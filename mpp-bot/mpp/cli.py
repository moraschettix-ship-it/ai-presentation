"""Point d'entree : `python -m mpp <commande>`.

run      lit les cartes, calcule les pronostics, les soumet (ou --dry-run)
gate     dit si une passe est utile, SANS lancer de navigateur (economie CI)
doctor   se connecte, montre tout ce qu'il voit, n'ecrit rien, et depose des
         captures exploitables. C'est la commande a lancer quand quelque chose
         ne va pas - ou une premiere fois, pour verifier que tout marche.
plan     calcule des pronostics depuis un JSON de cartes (hors ligne)
report   resume lisible du journal
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as config_mod
from . import notify
from .models import Card, Prediction
from .odds import OddsError, crowd_probabilities, implied_probabilities
from .schedule import SITE_TZ, due_cards, next_wake_up, to_site_local
from .site import MppSite, credentials
from .state import Store
from .strategy import build_predictions

EXIT_OK = 0
EXIT_PARTIAL = 1     # une partie des matchs a echoue
EXIT_FATAL = 2       # rien n'a pu etre fait


def render_table(predictions: list[Prediction], kappa: float) -> str:
    if not predictions:
        return "(aucun pronostic)"
    lines = [
        f"kappa = {kappa:.3f}   "
        f"deviations = {sum(p.deviation for p in predictions)}/{len(predictions)}",
        "",
        f"{'match':34} {'ko (Paris)':>11} {'marche 1/N/2':>16} "
        f"{'foule 1/N/2':>16} {'pick':>5} {'score':>6} {'edge':>7} {'P(ex)':>6}  note",
        "-" * 140,
    ]
    for p in sorted(predictions, key=lambda x: x.kickoff):
        market = "/".join(f"{x:.0%}" for x in p.market)
        crowd = "/".join(f"{x:.0%}" for x in p.crowd)
        ko = to_site_local(p.kickoff).strftime("%d/%m %Hh%M")
        flag = "*" if p.deviation else " "
        lines.append(
            f"{p.home + ' - ' + p.away:34.34} {ko:>11} {market:>16} {crowd:>16} "
            f"{p.outcome:>5} {p.score_str():>6} {p.edge:>+7.1%} {p.p_exact:>6.1%} "
            f"{flag} {p.reason}"
        )
    lines += ["", "* = ecart au favori du marche (pari de differenciation assume)"]
    return "\n".join(lines)


def _capture_dir(args, cfg) -> Path:
    return Path(args.capture_dir or Path(args.state_dir or cfg.state_dir) / "captures")


def _filter_competition(cards: list[Card], wanted: list[str]) -> list[Card]:
    if not wanted:
        return cards
    return [
        c
        for c in cards
        if any(
            w.lower() in f"{c.competition or ''} {c.matchday or ''}".lower()
            for w in wanted
        )
    ]


def cmd_run(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    store = Store(Path(args.state_dir or cfg.state_dir))
    now = datetime.now(timezone.utc)
    capture = _capture_dir(args, cfg)

    summary: dict = {
        "phase": args.phase,
        "dry_run": args.dry_run,
        "started_at": now.isoformat(),
        "submitted": 0,
        "unchanged": 0,
        "failed": 0,
        "read_errors": [],
    }

    failures: list[str] = []

    try:
        # Une seule session pour tout le cycle : deux connexions successives
        # doublent l'exposition pour aucun benefice.
        with MppSite(cfg.site, capture_dir=capture) as site:
            try:
                with credentials() as (email, password):
                    site.login(email, password)
                site.open_matches()
                cards, read_errors = site.read_cards()
            except Exception:
                site.dump("echec")
                raise

            summary["read_errors"] = read_errors
            summary["cards_read"] = len(cards)
            for err in read_errors:
                store.record_error("parse", err)
            store.save_fixtures(cards)

            todo = due_cards(
                _filter_competition(cards, cfg.competition_filter),
                now,
                cfg.windows,
                args.phase,
            )[: cfg.max_matches_per_run]
            summary["cards_due"] = len(todo)

            if not todo:
                store.save_last_run(summary)
                print(
                    f"[MPP] {len(cards)} cartes lues, aucune dans la fenetre "
                    f"'{args.phase}'."
                )
                return EXIT_PARTIAL if read_errors else EXIT_OK

            predictions, kappa = build_predictions(todo, cfg.strategy)
            summary["kappa"] = kappa
            print(render_table(predictions, kappa))

            if args.dry_run:
                for p in predictions:
                    store.record_submission(p, "dry-run")
                store.save_last_run(summary)
                print("\n[MPP] --dry-run : rien n'a ete envoye.")
                return EXIT_PARTIAL if read_errors else EXIT_OK

            expected: dict[str, tuple[int, int]] = {}
            to_write: list[Prediction] = []

            for p in predictions:
                if store.last_submitted_score(p.match_id) == (p.home_goals, p.away_goals):
                    store.record_submission(p, "skipped", "identique au dernier envoi")
                    summary["unchanged"] += 1
                    continue
                to_write.append(p)

            for p in to_write:
                try:
                    site.fill_score(p.match_id, p.home_goals, p.away_goals)
                    expected[p.match_id] = (p.home_goals, p.away_goals)
                except Exception as exc:
                    failures.append(f"{p.home} - {p.away} : {exc}")
                    store.record_submission(p, "failed", str(exc))
                    summary["failed"] += 1

            # Un bouton global ("Enregistrer mes pronos") couvre toute la page :
            # inutile de le recliquer pour chaque match.
            for p in to_write:
                if p.match_id not in expected:
                    continue
                if site.click_validate(p.match_id) == "page":
                    break

            confirmed = site.verify_all(expected) if expected else {}
            for p in to_write:
                if p.match_id not in expected:
                    continue
                if confirmed.get(p.match_id):
                    store.record_submission(p, "submitted")
                    summary["submitted"] += 1
                else:
                    failures.append(
                        f"{p.home} - {p.away} : relecture, le score enregistre "
                        "ne correspond pas"
                    )
                    store.record_submission(p, "failed", "relecture negative")
                    summary["failed"] += 1

            if failures:
                site.dump("echec-partiel")

    except Exception as exc:
        notify.send(f"[MPP] ECHEC ({args.phase}) : {exc}")
        store.record_error("session", str(exc), traceback=traceback.format_exc())
        store.save_last_run({**summary, "fatal": str(exc)})
        print(f"\n[MPP] echec : {exc}", file=sys.stderr)
        print(
            "Lancer le workflow 'MPP - diagnostic' pour obtenir une capture "
            "exploitable.",
            file=sys.stderr,
        )
        return EXIT_FATAL

    store.save_last_run(summary)
    if failures or summary["read_errors"]:
        notify.send(
            f"[MPP] passe {args.phase} : {summary['submitted']} envoyes, "
            f"{summary['failed']} echecs, "
            f"{len(summary['read_errors'])} cartes illisibles\n"
            + "\n".join(failures[:10] + summary["read_errors"][:10])
        )
        return EXIT_PARTIAL

    print(
        f"\n[MPP] passe {args.phase} : {summary['submitted']} envoyes, "
        f"{summary['unchanged']} inchanges."
    )
    return EXIT_OK


def cmd_gate(args: argparse.Namespace) -> int:
    """Decide s'il faut lancer la passe, depuis le calendrier en cache.

    Cout : quelques millisecondes, aucun reseau, aucun navigateur.
    """
    cfg = config_mod.load(args.config)
    store = Store(Path(args.state_dir or cfg.state_dir))
    now = datetime.now(timezone.utc)

    cards = [
        Card(
            match_id=m["match_id"],
            home=m["home"],
            away=m["away"],
            kickoff=datetime.fromisoformat(m["kickoff"]),
            locked=bool(m.get("locked")),
        )
        for m in store.load_fixtures()
    ]

    # Sans calendrier en cache on ne peut rien decider : on laisse passer.
    due = True if not cards else bool(due_cards(cards, now, cfg.windows, args.phase))

    if cards:
        wake = next_wake_up(cards, now, cfg.windows)
        if wake:
            print(
                f"[gate] prochaine entree en fenetre : "
                f"{to_site_local(wake):%d/%m %Hh%M} (Paris)"
            )
    print(
        f"[gate] phase={args.phase} due={'true' if due else 'false'} "
        f"({len(cards)} matchs en cache)"
    )

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"due={'true' if due else 'false'}\n")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    """Verifie toute la chaine sans rien ecrire sur MPP."""
    cfg = config_mod.load(args.config)
    capture = _capture_dir(args, cfg)
    cfg.site.headless = not args.headed
    print(f"base_url    : {cfg.site.base_url}")
    print(f"login_url   : {cfg.site.login_url or '(auto-detection)'}")
    print(f"matches_url : {cfg.site.matches_url or '(auto-detection)'}")
    print()

    try:
        with MppSite(cfg.site, capture_dir=capture) as site:
            try:
                with credentials() as (email, password):
                    site.login(email, password)
                print(f"[1/3] connexion OK -> {site.page.url}")
                site.open_matches()
                print(f"[2/3] page de pronostics OK -> {site.page.url}")
                cards, errors = site.read_cards()
            finally:
                base = site.dump("doctor")
                if base:
                    print(f"\ncaptures : {base}.*")

            print(f"[3/3] {len(cards)} cartes lues, {len(errors)} illisibles\n")
            for c in cards:
                market = implied_probabilities(c.cotes)      # type: ignore[arg-type]
                crowd = crowd_probabilities(c.crowd_pct)     # type: ignore[arg-type]
                print(
                    f"  {to_site_local(c.kickoff):%d/%m %Hh%M}  "
                    f"{c.home + ' - ' + c.away:34.34} "
                    f"cotes {'/'.join(str(int(x)) for x in c.cotes):>12}  "
                    f"marche {'/'.join(f'{x:.0%}' for x in market.as_tuple()):>14}  "
                    f"foule {'/'.join(f'{x:.0%}' for x in crowd.as_tuple()):>14}  "
                    f"{'SAISIE FERMEE' if c.locked else 'saisissable'}"
                )
            for err in errors:
                print(f"  [illisible] {err}")

            json_hits = [n for n in site.network_log if n["status"] < 400]
            print(f"\nreponses JSON observees : {len(json_hits)}")
            for n in json_hits[:8]:
                print(f"  {n['method']:5} {n['status']} {n['url'][:110]}")
            if json_hits:
                print(
                    "\nSi le calendrier et les cotes apparaissent dans ces reponses,"
                    "\nune API interne existe : y basculer supprimerait Chromium."
                )
            return EXIT_OK if cards and not errors else EXIT_PARTIAL

    except Exception as exc:
        print(f"\nECHEC : {exc}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        return EXIT_FATAL


def cmd_plan(args: argparse.Namespace) -> int:
    """Calcul hors ligne depuis un JSON de cartes. Aucun reseau."""
    cfg = config_mod.load(args.config)
    raw = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    base = datetime.now(timezone.utc) + timedelta(hours=20)
    cards = [
        Card(
            match_id=m.get("match_id", f"{m['home']}-{m['away']}"),
            home=m["home"],
            away=m["away"],
            kickoff=datetime.fromisoformat(m["kickoff"]) if "kickoff" in m else base,
            cotes=tuple(m["cotes"]),
            crowd_pct=tuple(m["crowd_pct"]),
        )
        for m in raw["matches"]
    ]
    if args.target_deviations is not None:
        cfg.strategy.target_deviations = args.target_deviations
    try:
        predictions, kappa = build_predictions(cards, cfg.strategy)
    except OddsError as exc:
        print(f"cotes invalides : {exc}", file=sys.stderr)
        return EXIT_FATAL
    print(render_table(predictions, kappa))
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    store = Store(Path(args.state_dir or cfg.state_dir))
    events = list(store.events())
    subs = [e for e in events if e.get("type") == "submission"]
    errs = [e for e in events if e.get("type") == "error"]
    if not events:
        print("journal vide")
        return EXIT_OK

    print(f"{len(subs)} evenements de soumission, {len(errs)} erreurs\n")
    by_status: dict[str, int] = {}
    for e in subs:
        by_status[e.get("status", "?")] = by_status.get(e.get("status", "?"), 0) + 1
    for status, n in sorted(by_status.items()):
        print(f"  {status:12} {n}")

    print("\n20 derniers envois :")
    for e in [s for s in subs if s.get("status") == "submitted"][-20:]:
        ko = datetime.fromisoformat(e["kickoff"]).astimezone(SITE_TZ)
        print(
            f"  {ko:%d/%m %Hh%M}  {e['home']} - {e['away']:22.22} "
            f"{e['home_goals']}-{e['away_goals']}  edge {e['edge']:+.1%}"
            f"{'  [deviation]' if e.get('deviation') else ''}"
        )
    if errs:
        print("\n10 dernieres erreurs :")
        for e in errs[-10:]:
            print(f"  [{e.get('scope')}] {e.get('message')}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mpp", description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--state-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="lit, calcule et soumet")
    run.add_argument("--phase", choices=("early", "late", "any"), default="any")
    run.add_argument("--dry-run", action="store_true", help="calcule sans envoyer")
    run.add_argument("--capture-dir", default=None)
    run.set_defaults(func=cmd_run)

    gate = sub.add_parser("gate", help="y a-t-il un match dans la fenetre ?")
    gate.add_argument("--phase", choices=("early", "late", "any"), default="late")
    gate.set_defaults(func=cmd_gate)

    doc = sub.add_parser("doctor", help="verifie toute la chaine, sans rien ecrire")
    doc.add_argument("--capture-dir", default=None)
    doc.add_argument("--headed", action="store_true")
    doc.set_defaults(func=cmd_doctor)

    plan = sub.add_parser("plan", help="calcul hors ligne depuis un JSON")
    plan.add_argument("fixture")
    plan.add_argument("--target-deviations", type=int, default=None)
    plan.set_defaults(func=cmd_plan)

    rep = sub.add_parser("report", help="resume du journal")
    rep.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
