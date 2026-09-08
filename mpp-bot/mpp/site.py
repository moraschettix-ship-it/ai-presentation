"""Adaptateur Mon Petit Prono (Playwright).

MPP n'expose pas d'API publique documentee. Deux consequences :

1. TOUS les selecteurs CSS vivent dans `config.yaml`, jamais dans le code. Quand
   le front change - et il changera - la reparation est une edition de YAML,
   pas un patch Python. Le code, lui, ne change pas.

2. Le sous-commande `discover` enregistre le DOM ET tout le trafic XHR/fetch de
   la page. Si le front est une SPA, elle parle forcement a un backend JSON :
   `discover` le prouve ou l'infirme en une execution. Si une API interne
   existe, la basculer dessus supprime Chromium du pipeline et divise le temps
   d'execution par dix - c'est le premier chantier a instruire.

Hygiene volontaire, puisque l'acces automatise n'est pas prevu par les CGU :
un seul navigateur, une temporisation entre chaque action, aucune requete en
parallele, un User-Agent stable.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import Card
from .schedule import SITE_TZ, parse_site_time

_NUM_RE = re.compile(r"(\d+(?:[.,]\d+)?)")


class SiteError(RuntimeError):
    pass


class LoginError(SiteError):
    pass


@dataclass
class Selectors:
    """Carte des selecteurs. Toutes les valeurs viennent de config.yaml."""

    login_url: str = ""
    matches_url: str = ""
    email_input: str = ""
    password_input: str = ""
    login_submit: str = ""
    logged_in_marker: str = ""
    cookie_accept: str = ""
    card: str = ""
    card_id_attr: str = "data-match-id"
    home_team: str = ""
    away_team: str = ""
    kickoff_time: str = ""
    matchday: str = ""
    cote_cells: str = ""          # doit renvoyer exactement 3 elements par carte
    crowd_cells: str = ""         # idem
    home_score_input: str = ""
    away_score_input: str = ""
    submit_button: str = ""
    locked_marker: str = ""
    existing_home_score: str = ""
    existing_away_score: str = ""


@dataclass
class SiteConfig:
    selectors: Selectors = field(default_factory=Selectors)
    headless: bool = True
    timeout_ms: int = 20_000
    action_delay_s: tuple[float, float] = (1.2, 2.8)
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    )
    locale: str = "fr-FR"


def parse_number(text: str, what: str) -> float:
    """Extrait un nombre d'un libelle, en refusant tout ce qui est ambigu."""
    matches = _NUM_RE.findall(text or "")
    if len(matches) != 1:
        raise SiteError(f"{what} : attendu 1 nombre dans {text!r}, trouve {len(matches)}")
    return float(matches[0].replace(",", "."))


class MppSite:
    """Session navigateur MPP. A utiliser comme context manager."""

    def __init__(self, cfg: SiteConfig, capture_dir: Path | None = None) -> None:
        self.cfg = cfg
        self.capture_dir = capture_dir
        self._pw = None
        self._browser = None
        self._page = None
        self.network_log: list[dict[str, Any]] = []

    # --- cycle de vie ------------------------------------------------------
    def __enter__(self) -> "MppSite":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.cfg.headless)
        context = self._browser.new_context(
            user_agent=self.cfg.user_agent,
            locale=self.cfg.locale,
            timezone_id="Europe/Paris",
        )
        context.set_default_timeout(self.cfg.timeout_ms)
        self._page = context.new_page()
        if self.capture_dir is not None:
            self._page.on("response", self._log_response)
        return self

    def __exit__(self, *exc: Any) -> None:
        # La fermeture ne doit jamais masquer l'erreur qui a fait sortir du bloc.
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._pw is not None:
            try:
                self._pw.stop()
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
        """Enregistre les reponses JSON : c'est la que se trouve l'API, si elle existe."""
        try:
            ctype = (response.headers or {}).get("content-type", "")
            if "json" not in ctype:
                return
            body = response.text()[:20_000]
            self.network_log.append(
                {
                    "url": response.url,
                    "status": response.status,
                    "method": response.request.method,
                    "body_preview": body,
                }
            )
        except Exception:
            pass

    # --- navigation --------------------------------------------------------
    def login(self, email: str, password: str) -> None:
        s = self.cfg.selectors
        self.page.goto(s.login_url, wait_until="domcontentloaded")
        if s.cookie_accept:
            try:
                self.page.click(s.cookie_accept, timeout=4000)
            except Exception:
                pass   # banniere absente : cas normal
        self._pause()
        self.page.fill(s.email_input, email)
        self.page.fill(s.password_input, password)
        self._pause()
        self.page.click(s.login_submit)
        try:
            self.page.wait_for_selector(s.logged_in_marker, timeout=self.cfg.timeout_ms)
        except Exception as exc:
            raise LoginError(
                "connexion echouee : le marqueur de session "
                f"{s.logged_in_marker!r} n'est jamais apparu. "
                "Identifiants invalides, captcha, ou selecteur obsolete."
            ) from exc

    def open_matches(self) -> None:
        s = self.cfg.selectors
        self.page.goto(s.matches_url, wait_until="networkidle")
        self.page.wait_for_selector(s.card, timeout=self.cfg.timeout_ms)
        self._pause()

    # --- lecture -----------------------------------------------------------
    def read_cards(self, day: date | None = None) -> tuple[list[Card], list[str]]:
        """Renvoie (cartes lues, erreurs par carte).

        Une carte illisible n'interrompt jamais la lecture des autres : on
        collecte l'erreur et on continue. Un pronostic manquant sur un match
        vaut mieux qu'aucun pronostic sur dix-huit.
        """
        s = self.cfg.selectors
        day = day or datetime.now(SITE_TZ).date()
        cards: list[Card] = []
        errors: list[str] = []

        elements = self.page.query_selector_all(s.card)
        if not elements:
            raise SiteError(f"aucune carte trouvee avec le selecteur {s.card!r}")

        for idx, el in enumerate(elements):
            try:
                cards.append(self._parse_card(el, idx, day))
            except Exception as exc:
                errors.append(f"carte #{idx} : {exc}")
        return cards, errors

    def _text(self, el: Any, selector: str, what: str) -> str:
        node = el.query_selector(selector)
        if node is None:
            raise SiteError(f"{what} introuvable (selecteur {selector!r})")
        return (node.inner_text() or "").strip()

    def _triplet(self, el: Any, selector: str, what: str) -> tuple[float, float, float]:
        nodes = el.query_selector_all(selector)
        if len(nodes) != 3:
            raise SiteError(f"{what} : attendu 3 valeurs, trouve {len(nodes)}")
        values = [parse_number(n.inner_text(), what) for n in nodes]
        return (values[0], values[1], values[2])

    def _parse_card(self, el: Any, idx: int, day: date) -> Card:
        s = self.cfg.selectors
        match_id = el.get_attribute(s.card_id_attr) or ""
        home = self._text(el, s.home_team, "equipe domicile")
        away = self._text(el, s.away_team, "equipe exterieur")
        if not match_id:
            # Repli stable tant que MPP n'expose pas d'identifiant : la paire
            # d'equipes + la date suffit a identifier un match d'une journee.
            match_id = f"{day.isoformat()}:{home}-{away}".lower().replace(" ", "_")

        kickoff = parse_site_time(day, self._text(el, s.kickoff_time, "heure"))
        cotes = self._triplet(el, s.cote_cells, "cotes")
        crowd = self._triplet(el, s.crowd_cells, "repartition foule")

        locked = bool(s.locked_marker and el.query_selector(s.locked_marker))
        existing = None
        if s.existing_home_score and s.existing_away_score:
            hn = el.query_selector(s.existing_home_score)
            an = el.query_selector(s.existing_away_score)
            if hn is not None and an is not None:
                ht = (hn.inner_text() or hn.get_attribute("value") or "").strip()
                at = (an.inner_text() or an.get_attribute("value") or "").strip()
                if ht.isdigit() and at.isdigit():
                    existing = (int(ht), int(at))

        matchday = None
        if s.matchday:
            node = el.query_selector(s.matchday)
            if node is not None:
                matchday = (node.inner_text() or "").strip()

        return Card(
            match_id=match_id,
            home=home,
            away=away,
            kickoff=kickoff,
            matchday=matchday,
            cotes=cotes,
            crowd_pct=crowd,
            existing_pick=existing,
            locked=locked,
        )

    # --- ecriture ----------------------------------------------------------
    def submit_score(self, match_id: str, home_goals: int, away_goals: int) -> None:
        """Saisit un score et valide. Ecrase toute saisie existante."""
        s = self.cfg.selectors
        card = self._find_card(match_id)
        if s.locked_marker and card.query_selector(s.locked_marker):
            raise SiteError(f"{match_id} : saisie fermee cote MPP")

        home_input = card.query_selector(s.home_score_input)
        away_input = card.query_selector(s.away_score_input)
        if home_input is None or away_input is None:
            raise SiteError(f"{match_id} : champs de score introuvables")

        home_input.fill(str(home_goals))
        self._pause()
        away_input.fill(str(away_goals))
        self._pause()

        if s.submit_button:
            button = card.query_selector(s.submit_button) or self.page.query_selector(
                s.submit_button
            )
            if button is None:
                raise SiteError(f"{match_id} : bouton de validation introuvable")
            button.click()
        self._pause()

    def verify_score(self, match_id: str, home_goals: int, away_goals: int) -> bool:
        """Relit la carte pour confirmer l'enregistrement.

        Un HTTP 200 ne prouve rien : on verifie la valeur affichee. Sans cette
        etape, un changement de front peut faire echouer toutes les saisies en
        silence pendant des semaines.
        """
        s = self.cfg.selectors
        if not (s.existing_home_score and s.existing_away_score):
            return True   # verification non configuree : on ne peut rien affirmer
        self.page.reload(wait_until="networkidle")
        card = self._find_card(match_id)
        hn = card.query_selector(s.existing_home_score)
        an = card.query_selector(s.existing_away_score)
        if hn is None or an is None:
            return False
        ht = (hn.inner_text() or hn.get_attribute("value") or "").strip()
        at = (an.inner_text() or an.get_attribute("value") or "").strip()
        return ht == str(home_goals) and at == str(away_goals)

    def _find_card(self, match_id: str) -> Any:
        s = self.cfg.selectors
        for el in self.page.query_selector_all(s.card):
            if (el.get_attribute(s.card_id_attr) or "") == match_id:
                return el
        # Repli sur l'identifiant synthetique (noms d'equipes).
        for el in self.page.query_selector_all(s.card):
            try:
                home = self._text(el, s.home_team, "equipe domicile")
                away = self._text(el, s.away_team, "equipe exterieur")
            except SiteError:
                continue
            if f"{home}-{away}".lower().replace(" ", "_") in match_id:
                return el
        raise SiteError(f"carte {match_id} introuvable sur la page")

    # --- diagnostic --------------------------------------------------------
    def dump(self, name: str) -> None:
        if self.capture_dir is None:
            return
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (self.capture_dir / f"{name}-{stamp}.html").write_text(
            self.page.content(), encoding="utf-8"
        )
        try:
            self.page.screenshot(
                path=str(self.capture_dir / f"{name}-{stamp}.png"), full_page=True
            )
        except Exception:
            pass
        (self.capture_dir / f"{name}-{stamp}.network.json").write_text(
            json.dumps(self.network_log, ensure_ascii=False, indent=2), encoding="utf-8"
        )


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
