"""Adaptateur Mon Petit Prono (Playwright), sans selecteur code en dur.

Toute l'identification passe par mpp/detect.py : une carte est reconnue a sa
signature numerique (3 cotes + 3 pourcentages + une heure), pas a ses classes
CSS. Voir l'en-tete de detect.py pour le raisonnement.

Ce module ne fait que la mecanique navigateur : connexion, recherche de la page
de pronostics, saisie, relecture de controle, et production d'un diagnostic
exploitable quand quelque chose ne colle pas.

Hygiene volontaire, l'acces automatise n'etant vraisemblablement pas prevu par
les CGU : un seul navigateur, temporisation aleatoire entre chaque action,
aucune requete en parallele, User-Agent stable.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .detect import (
    CARD_SCAN_JS,
    CONSENT_JS,
    LOGIN_SCAN_JS,
    SUBMIT_SCAN_JS,
    DetectionError,
    parse_raw_cards,
)
from .models import Card
from .schedule import resolve_kickoff

# Liens a suivre pour trouver la page de pronostics quand l'atterrissage
# post-connexion n'en est pas une.
NAV_HINTS = re.compile(
    r"prono|pronostic|grille|mes matchs|matchs|journ[ée]e|calendrier|jouer",
    re.IGNORECASE,
)


# Cles dont la valeur ne doit jamais sortir dans une capture : les artefacts
# GitHub Actions d'un depot PUBLIC sont telechargeables par n'importe qui.
_SECRET_KEY_RE = re.compile(
    r"token|auth|password|passwd|secret|session|cookie|jwt|bearer|api[-_]?key|"
    r"refresh|credential",
    re.IGNORECASE,
)
_REDACTED = "[REDACTED]"


def _redact_value(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            k: (_REDACTED if _SECRET_KEY_RE.search(str(k)) else _redact_value(v))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_redact_value(v) for v in node]
    return node


def redact(text: str) -> str:
    """Neutralise les valeurs sensibles d'un corps JSON avant ecriture.

    On travaille sur la structure decodee plutot qu'a coups d'expressions
    regulieres : une substitution textuelle sur du JSON produit du JSON casse
    des que la valeur contient une quote echappee, et un fichier casse n'est
    plus exploitable pour diagnostiquer quoi que ce soit.

    Corps non decodable (HTML, texte libre) : on masque toute suite assez
    longue de caracteres de jeton, ce qui couvre les JWT et les cles opaques
    sans toucher au texte normal.
    """
    try:
        return json.dumps(_redact_value(json.loads(text)), ensure_ascii=False)
    except (json.JSONDecodeError, TypeError, ValueError):
        return re.sub(r"[A-Za-z0-9_\-]{40,}\.?[A-Za-z0-9_\-.]*", _REDACTED, text)


class SiteError(RuntimeError):
    pass


class LoginError(SiteError):
    pass


@dataclass
class SiteConfig:
    base_url: str = "https://www.monpetitprono.com"
    # Laisser vide pour laisser le bot trouver seul. Renseigner uniquement si
    # l'auto-detection echoue (le diagnostic dit alors quoi mettre).
    login_url: str = ""
    matches_url: str = ""
    headless: bool = True
    executable_path: str = ""       # utile en local, jamais en CI
    timeout_ms: int = 25_000
    action_delay_s: tuple[float, float] = (1.2, 2.8)
    nav_attempts: int = 6           # liens explores pour trouver la page de pronos
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    )
    locale: str = "fr-FR"


@dataclass
class Diagnostic:
    """Ce que le bot a vu, quand il n'a pas vu ce qu'il fallait."""

    step: str
    url: str = ""
    message: str = ""
    candidates: int = 0
    details: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"[{self.step}] {self.message}", f"  url : {self.url}"]
        if self.candidates:
            lines.append(f"  blocs candidats : {self.candidates}")
        lines += [f"  - {d}" for d in self.details[:10]]
        return "\n".join(lines)


class MppSite:
    """Session navigateur MPP. A utiliser comme context manager."""

    def __init__(self, cfg: SiteConfig, capture_dir: Path | None = None) -> None:
        self.cfg = cfg
        self.capture_dir = capture_dir
        self._pw = None
        self._browser = None
        self._page = None
        self._index: dict[str, int] = {}      # match_id -> data-mpp-card
        self.network_log: list[dict[str, Any]] = []
        self.diagnostics: list[Diagnostic] = []

    # --- cycle de vie ------------------------------------------------------
    def __enter__(self) -> "MppSite":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        launch: dict[str, Any] = {"headless": self.cfg.headless}
        if self.cfg.executable_path:
            launch["executable_path"] = self.cfg.executable_path
        self._browser = self._pw.chromium.launch(**launch)
        context = self._browser.new_context(
            user_agent=self.cfg.user_agent,
            locale=self.cfg.locale,
            timezone_id="Europe/Paris",
        )
        context.set_default_timeout(self.cfg.timeout_ms)
        self._page = context.new_page()
        self._page.on("response", self._log_response)
        return self

    def __exit__(self, *exc: Any) -> None:
        # La fermeture ne doit jamais masquer l'erreur qui a fait sortir du bloc.
        for closer in (self._browser, self._pw):
            if closer is None:
                continue
            try:
                closer.close() if closer is self._browser else closer.stop()
            except Exception:
                pass

    @property
    def page(self):
        if self._page is None:
            raise SiteError("session non demarree (utiliser `with MppSite(...)`)")
        return self._page

    def _pause(self) -> None:
        lo, hi = self.cfg.action_delay_s
        time.sleep(random.uniform(lo, hi))

    def _log_response(self, response: Any) -> None:
        """Trace les reponses JSON : c'est la qu'on verra une API interne."""
        try:
            if "json" not in (response.headers or {}).get("content-type", ""):
                return
            url = response.url
            if _SECRET_KEY_RE.search(url.split("?", 1)[1] if "?" in url else ""):
                url = url.split("?", 1)[0] + "?[REDACTED]"
            self.network_log.append(
                {
                    "url": url,
                    "status": response.status,
                    "method": response.request.method,
                    "body_preview": redact(response.text()[:20_000]),
                }
            )
        except Exception:
            pass

    def _note(self, diag: Diagnostic) -> None:
        diag.url = self.page.url
        self.diagnostics.append(diag)

    # --- connexion ---------------------------------------------------------
    def _dismiss_consent(self) -> None:
        try:
            if self.page.evaluate(CONSENT_JS):
                self._pause()
        except Exception:
            pass    # une banniere absente ou recalcitrante n'est pas bloquante

    def _goto_login_form(self) -> None:
        """Amene la page sur un formulaire contenant un champ mot de passe."""
        start = self.cfg.login_url or self.cfg.base_url
        self.page.goto(start, wait_until="domcontentloaded")
        self._dismiss_consent()
        if self.page.query_selector('input[type="password"]'):
            return

        # Pas de formulaire sur la page d'accueil : suivre un lien de connexion.
        # `e.href` (et non getAttribute) renvoie l'URL absolue resolue : un
        # href relatif ne peut pas etre passe tel quel a page.goto().
        links = self.page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href], button'))
                 .map(e => ({ text: (e.innerText || '').replace(/\s+/g,' ').trim(), href: e.href || '' }))
                 .filter(l => /connexion|se connecter|login|sign in|s'identifier/i.test(l.text + ' ' + l.href))
                 .slice(0, 5)"""
        )
        for link in links:
            try:
                if link["href"] and "#" not in link["href"].rsplit("/", 1)[-1]:
                    self.page.goto(link["href"], wait_until="domcontentloaded")
                elif link["text"]:
                    self.page.get_by_text(link["text"], exact=False).first.click()
                else:
                    continue
                self._dismiss_consent()
                self.page.wait_for_selector('input[type="password"]', timeout=6000)
                return
            except Exception:
                continue

        self._note(
            Diagnostic(
                step="login",
                message="aucun champ mot de passe trouve, et aucun lien de "
                "connexion exploitable depuis la page de depart",
                details=[f"lien essaye : {l['text']!r} -> {l['href']!r}" for l in links],
            )
        )
        raise LoginError(
            "formulaire de connexion introuvable. Renseigner `site.login_url` "
            "dans config.yaml (le diagnostic joint indique ce qui a ete vu)."
        )

    def login(self, email: str, password: str) -> None:
        self._goto_login_form()
        found = self.page.evaluate(LOGIN_SCAN_JS)
        if not found.get("found"):
            raise LoginError("champ mot de passe disparu entre-temps")
        if not found.get("has_user"):
            raise LoginError("champ identifiant introuvable a cote du mot de passe")

        self.page.fill("[data-mpp-user]", email)
        self.page.fill("[data-mpp-pass]", password)
        self._pause()

        if found.get("has_submit"):
            self.page.click("[data-mpp-submit]")
        else:
            self.page.press("[data-mpp-pass]", "Enter")

        # Critere de succes : le champ mot de passe a disparu. Il ne depend
        # d'aucun libelle, d'aucune classe, et d'aucune URL.
        try:
            self.page.wait_for_selector(
                'input[type="password"]', state="detached", timeout=self.cfg.timeout_ms
            )
        except Exception as exc:
            self._note(
                Diagnostic(
                    step="login",
                    message="le champ mot de passe est toujours la apres envoi : "
                    "identifiants refuses, captcha, ou double authentification",
                )
            )
            raise LoginError(
                "connexion echouee : le formulaire est toujours affiche apres envoi"
            ) from exc
        self._pause()

    # --- recherche de la page de pronostics --------------------------------
    def _scan(self) -> list[dict[str, Any]]:
        try:
            return self.page.evaluate(CARD_SCAN_JS) or []
        except Exception:
            return []

    def open_matches(self) -> None:
        """Trouve la page de pronostics, en explorant si necessaire."""
        if self.cfg.matches_url:
            self.page.goto(self.cfg.matches_url, wait_until="networkidle")
            self._dismiss_consent()
            if self._scan():
                return
            self._note(
                Diagnostic(
                    step="matches",
                    message="`site.matches_url` ne contient aucune carte detectable",
                )
            )

        if self._scan():
            return   # la page d'atterrissage est deja la bonne

        visited = {self.page.url}
        links = self.page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href]'))
                 .map(a => ({ text: (a.innerText || '').replace(/\\s+/g,' ').trim(), href: a.href }))
                 .filter(l => l.href && !l.href.startsWith('javascript'))"""
        )
        candidates = [l for l in links if NAV_HINTS.search(l["text"] + " " + l["href"])]

        tried: list[str] = []
        for link in candidates[: self.cfg.nav_attempts]:
            if link["href"] in visited:
                continue
            visited.add(link["href"])
            tried.append(f"{link['text'][:30]!r} -> {link['href']}")
            try:
                self.page.goto(link["href"], wait_until="networkidle")
                self._dismiss_consent()
                self._pause()
                if self._scan():
                    return
            except Exception:
                continue

        self._note(
            Diagnostic(
                step="matches",
                message="page de pronostics introuvable : aucune page visitee ne "
                "contient de bloc combinant 3 cotes et 3 pourcentages",
                details=tried,
            )
        )
        raise SiteError(
            "aucune carte de match detectee. Renseigner `site.matches_url` dans "
            "config.yaml, ou consulter le diagnostic joint."
        )

    # --- lecture -----------------------------------------------------------
    def read_cards(self) -> tuple[list[Card], list[str]]:
        """Renvoie (cartes lues, erreurs par carte).

        Une carte illisible n'interrompt jamais les autres : mieux vaut 17
        pronostics sur 18 que zero.
        """
        now = datetime.now(timezone.utc)
        payload = self._scan()
        try:
            raw_cards, errors = parse_raw_cards(payload)
        except DetectionError as exc:
            self._note(
                Diagnostic(
                    step="parse",
                    message=str(exc),
                    candidates=len(payload),
                    details=[str(p.get("text", ""))[:80] for p in payload[:5]],
                )
            )
            raise SiteError(str(exc)) from exc

        self._index = {}
        cards: list[Card] = []
        for raw in raw_cards:
            try:
                kickoff = resolve_kickoff(raw.time, raw.date, now)
            except Exception as exc:
                errors.append(f"{raw.home} - {raw.away} : date/heure illisible ({exc})")
                continue

            match_id = raw.match_id or (
                f"{kickoff.date().isoformat()}:{raw.home}-{raw.away}"
                .lower()
                .replace(" ", "_")
            )
            self._index[match_id] = raw.idx

            existing = None
            digits = [v for v in raw.input_values if v.strip().isdigit()]
            if len(digits) == 2:
                existing = (int(digits[0]), int(digits[1]))

            cards.append(
                Card(
                    match_id=match_id,
                    home=raw.home,
                    away=raw.away,
                    kickoff=kickoff,
                    cotes=raw.cotes,
                    crowd_pct=raw.crowd_pct,
                    existing_pick=existing,
                    locked=raw.input_count < 2,
                    raw={"idx": raw.idx, "inputs": raw.input_count, "text": raw.text},
                )
            )
        return cards, errors

    # --- ecriture ----------------------------------------------------------
    def _inputs(self, match_id: str):
        idx = self._index.get(match_id)
        if idx is None:
            raise SiteError(f"{match_id} : carte absente du dernier scan")
        card = self.page.query_selector(f'[data-mpp-card="{idx}"]')
        if card is None:
            raise SiteError(f"{match_id} : carte disparue de la page")
        inputs = [
            el
            for el in card.query_selector_all("input")
            if (el.get_attribute("type") or "text").lower()
            in ("text", "number", "tel", "")
        ]
        if len(inputs) != 2:
            raise SiteError(
                f"{match_id} : {len(inputs)} champs de saisie au lieu de 2 "
                "(saisie fermee, ou interface a boutons +/- non geree)"
            )
        return inputs

    def fill_score(self, match_id: str, home_goals: int, away_goals: int) -> None:
        """Saisit un score. Ecrase toute valeur presente."""
        home_input, away_input = self._inputs(match_id)
        for element, value in ((home_input, home_goals), (away_input, away_goals)):
            element.fill("")
            element.fill(str(value))
            element.dispatch_event("change")   # frameworks reactifs
            self._pause()

    def click_validate(self, match_id: str) -> str | None:
        """Clique le bouton de validation. Renvoie 'card', 'page' ou None."""
        idx = self._index.get(match_id)
        if idx is None:
            return None
        scope = self.page.evaluate(SUBMIT_SCAN_JS, idx)
        if scope is None:
            return None
        self.page.click(f'[data-mpp-validate="{idx}"]')
        self._pause()
        return scope

    def verify_all(
        self, expected: dict[str, tuple[int, int]]
    ) -> dict[str, bool]:
        """Recharge la page et confirme chaque score enregistre.

        Un HTTP 200 ne prouve rien : sans cette relecture, un changement
        d'interface peut faire echouer toutes les saisies en silence pendant des
        semaines. Une seule rechargement pour tous les matchs, pas un par match.
        """
        self.page.reload(wait_until="networkidle")
        self._dismiss_consent()
        cards, _ = self.read_cards()
        actual = {c.match_id: c.existing_pick for c in cards}
        return {mid: actual.get(mid) == score for mid, score in expected.items()}

    # --- diagnostic --------------------------------------------------------
    def dump(self, name: str) -> Path | None:
        """Ecrit HTML + capture d'ecran + trafic JSON + diagnostics."""
        if self.capture_dir is None:
            return None
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base = self.capture_dir / f"{name}-{stamp}"
        try:
            base.with_suffix(".html").write_text(self.page.content(), encoding="utf-8")
            self.page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        except Exception:
            pass
        base.with_suffix(".network.json").write_text(
            json.dumps(
                _redact_value(self.network_log), ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        if self.diagnostics:
            base.with_suffix(".diagnostic.txt").write_text(
                "\n\n".join(d.render() for d in self.diagnostics), encoding="utf-8"
            )
        return base


@contextmanager
def credentials() -> Iterator[tuple[str, str]]:
    email = os.environ.get("MPP_EMAIL", "")
    password = os.environ.get("MPP_PASSWORD", "")
    if not email or not password:
        raise LoginError(
            "MPP_EMAIL et MPP_PASSWORD doivent etre definis "
            "(secrets GitHub en CI, variables d'environnement en local)"
        )
    yield email, password
