"""Point d'entree : `python -m mpp <commande>`.

Commandes
---------
run       lit les cartes, calcule les pronostics, les soumet (ou --dry-run)
gate      dit si une passe est utile, SANS lancer de navigateur (economie CI)
plan      calcule des pronostics depuis un fichier JSON de cartes (hors ligne)
discover  se connecte et vide le DOM + le trafic JSON, pour ecrire/reparer les
          selecteurs et verifier l'existence d'une API interne
report    resume lisible du journal
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
from .odds import OddsError
from .schedule import SITE_TZ, due_cards, next_wake_up, to_site_local
from .site import MppSite, SiteError, credentials
from .state import Store
from .strategy import build_predictions

EXIT_OK = 0
EXIT_PARTIAL = 1     # une partie des matchs a echoue
EXIT_FATAL = 2       # rien n'a pu etre fait


# ---------------------------------------------------------------------------
# rendu
# ---------------------------------------------------------------------------
def render_table(predictions: list[Prediction], kappa: float) -> str:
    if not predictions:
        return "(aucun pronostic)"
    head = (
        f"kappa = {kappa:.3f}   "
        f"deviations = {sum(p.deviation for p in predictions)}/{len(predictions)}\n\n"
        f"{'match':34} {'ko (Paris)':>11} {'marche 1/N/2':>16} "
        f"{'foule 1/N/2':>16} {'pick':>5} {'score':>6} {'edge':>7} {'P(ex)':>6}  note"
    )
    lines = [head, "-" * 140]
    for p in sorted(predictions, key=lambda x: x.kickoff):
        market = "/".join(f"{x:.0%}" for x in p.market)
        crowd = "/".join(f"{x:.0%}" for x in p.crowd)
        ko = to_site_local(p.kickoff).strftime("%d/%m %Hh%M")
        flag = "*" if p.deviation else " "
        lines.append(
            f"{p.home + ' - ' + p.away:34.34} {ko:>11} {market:>16} {crowd:>16} "
            f"{p.outcome:>5} {p.score_str():>6} {p.edge:>+7.1%} {p.p_exact:>6.1%} {flag} {p.reason}"
        )
    lines.append("")
    lines.append("* = ecart au favori du marche (pari de differenciation assume)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# commandes
# ---------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    store = Store(Path(args.state_dir or cfg.state_dir))
    now = datetime.now(timezone.utc)
    capture = Path(args.capture_dir) if args.capture_dir else None

    summary: dict = {
        "phase": args.phase,
        "dry_run": args.dry_run,
        "started_at": now.isoformat(),
        "submitted": 0,
        "unchanged": 0,
        "failed": 0,
        "read_errors": [],
    }

    try:
        with MppSite(cfg.site, capture_dir=capture) as site:
            with credentials() as (email, password):
                site.login(email, password)
            site.open_matches()
            cards, read_errors = site.read_cards()
    except Exception as exc:
        notify.send(f"[MPP] ECHEC TOTAL ({args.phase}) : {exc}")
        store.record_error("session", str(exc), traceback=traceback.format_exc())
        store.save_last_run({**summary, "fatal": str(exc)})
        return EXIT_FATAL

    summary["read_errors"] = read_errors
    for err in read_errors:
        store.record_error("parse", err)

    store.save_fixtures(cards)

    if cfg.competition_filter:
        cards = [
            c
            for c in cards
            if any(f.lower() in (c.competition or "").lower() for f in cfg.competition_filter)
            or any(f.lower() in (c.matchday or "").lower() for f in cfg.competition_filter)
        ]

    todo = due_cards(cards, now, cfg.windows, args.phase)[: cfg.max_matches_per_run]
    summary["cards_read"] = len(cards)
    summary["cards_due"] = len(todo)

    if not todo:
        store.save_last_run(summary)
        print(f"[MPP] {len(cards)} cartes lues, aucune dans la fenetre '{args.phase}'.")
        return EXIT_PARTIAL if read_errors else EXIT_OK

    try:
        predictions, kappa = build_predictions(todo, cfg.strategy)
    except OddsError as exc:
        notify.send(f"[MPP] cotes incoherentes, aucune soumission : {exc}")
        store.record_error("odds", str(exc))
        store.save_last_run({**summary, "fatal": str(exc)})
        return EXIT_FATAL

    summary["kappa"] = kappa
    print(render_table(predictions, kappa))

    if args.dry_run:
        for p in predictions:
            store.record_submission(p, "dry-run")
        store.save_last_run(summary)
        return EXIT_OK

    # Reouverture d'une session pour la phase d'ecriture : la lecture a pu
    # durer, et on repart d'une page fraiche.
    failures: list[str] = []
    try:
        with MppSite(cfg.site, capture_dir=capture) as site:
            with credentials() as (email, password):
                site.login(email, password)
            site.open_matches()

            for p in predictions:
                previous = store.last_submitted_score(p.match_id)
                if previous == (p.home_goals, p.away_goals):
                    store.record_submission(p, "skipped", "identique au dernier envoi")
                    summary["unchanged"] += 1
                    continue
                try:
                    site.submit_score(p.match_id, p.home_goals, p.away_goals)
                    ok = site.verify_score(p.match_id, p.home_goals, p.away_goals)
                    if not ok:
                        raise SiteError("relecture : le score enregistre ne correspond pas")
                    store.record_submission(p, "submitted")
                    summary["submitted"] += 1
                except Exception as exc:
                    failures.append(f"{p.home} - {p.away} : {exc}")
                    store.record_submission(p, "failed", str(exc))
                    summary["failed"] += 1
    except Exception as exc:
        notify.send(f"[MPP] echec pendant la soumission ({args.phase}) : {exc}")
        store.record_error("submit-session", str(exc), traceback=traceback.format_exc())
        store.save_last_run({**summary, "fatal": str(exc)})
        return EXIT_FATAL

    store.save_last_run(summary)

    if failures or read_errors:
        notify.send(
            f"[MPP] passe {args.phase} : {summary['submitted']} envoyes, "
            f"{summary['failed']} echecs, {len(read_errors)} cartes illisibles\n"
            + "\n".join(failures[:10] + read_errors[:10])
        )
        return EXIT_PARTIAL

    print(
        f"[MPP] passe {args.phase} : {summary['submitted']} envoyes, "
        f"{summary['unchanged']} inchanges."
    )
    return EXIT_OK


def cmd_gate(args: argparse.Namespace) -> int:
    """Decide s'il faut lancer la passe, a partir du calendrier en cache.

    Cout : quelques millisecondes, aucune dependance, aucun reseau. Le workflow
    n'installe Chromium que si cette commande dit oui.
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

    # Sans calendrier en cache, on ne peut rien decider : on laisse passer.
    due = True if not cards else bool(due_cards(cards, now, cfg.windows, args.phase))

    if due and cards:
        wake = next_wake_up(cards, now, cfg.windows)
        if wake:
            print(f"[gate] prochaine entree en fenetre : {to_site_local(wake):%d/%m %Hh%M} (Paris)")
    print(f"[gate] phase={args.phase} due={'true' if due else 'false'} ({len(cards)} matchs en cache)")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"due={'true' if due else 'false'}\n")
    return EXIT_OK


def cmd_plan(args: argparse.Namespace) -> int:
    """Calcul hors ligne depuis un JSON de cartes. Sert aux tests et aux essais
    de reglage de kappa sans toucher au site."""
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
    predictions, kappa = build_predictions(cards, cfg.strategy)
    print(render_table(predictions, kappa))
    return EXIT_OK


def cmd_discover(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    capture = Path(args.capture_dir or "state/discovery")
    cfg.site.headless = not args.headed
    try:
        with MppSite(cfg.site, capture_dir=capture) as site:
            if cfg.site.selectors.login_url:
                with credentials() as (email, password):
                    site.login(email, password)
                site.dump("apres-login")
            if cfg.site.selectors.matches_url:
                site.open_matches()
                site.dump("page-matchs")
    except Exception as exc:
        print(f"[discover] interrompu : {exc}", file=sys.stderr)
        print("Les fichiers deja ecrits restent exploitables.", file=sys.stderr)
        return EXIT_PARTIAL
    print(f"[discover] captures ecrites dans {capture}/")
    print("Chercher dans *.network.json les reponses JSON : si le calendrier et")
    print("les cotes y figurent, une API interne existe et Playwright devient inutile.")
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    store = Store(Path(args.state_dir or cfg.state_dir))
    subs = [e for e in store.events() if e.get("type") == "submission"]
    errs = [e for e in store.events() if e.get("type") == "error"]
    if not subs and not errs:
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


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mpp", description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--state-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="lit, calcule et soumet")
    run.add_argument("--phase", choices=("early", "late", "any"), default="any")
    run.add_argument("--dry-run", action="store_true", help="calcule et affiche, n'envoie rien")
    run.add_argument("--capture-dir", default=None)
    run.set_defaults(func=cmd_run)

    gate = sub.add_parser("gate", help="y a-t-il quelque chose a faire ?")
    gate.add_argument("--phase", choices=("early", "late", "any"), default="late")
    gate.set_defaults(func=cmd_gate)

    plan = sub.add_parser("plan", help="calcul hors ligne depuis un JSON")
    plan.add_argument("fixture")
    plan.add_argument("--target-deviations", type=int, default=None)
    plan.set_defaults(func=cmd_plan)

    disc = sub.add_parser("discover", help="capture DOM + trafic JSON")
    disc.add_argument("--capture-dir", default=None)
    disc.add_argument("--headed", action="store_true")
    disc.set_defaults(func=cmd_discover)

    rep = sub.add_parser("report", help="resume du journal")
    rep.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
