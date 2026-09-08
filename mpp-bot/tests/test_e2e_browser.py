"""Bout en bout dans un vrai Chromium, sur deux faux sites MPP.

Ce que ce test prouve
---------------------
Les deux pages `pronos.html` et `grille.html` contiennent EXACTEMENT les memes
donnees (celles de la capture d'ecran de reference) dans des DOM totalement
differents :

  - pronos.html : `<article>` imbriquees, classes obfusquees (`x7f2a-*`), noms
    d'equipes en double (texte + attribut alt), rangs suffixes ("27e"),
    identifiant de match en attribut, bouton de validation par carte ;
  - grille.html : un `<table>`, aucun identifiant, aucun alt, rangs NUS ("27"),
    donc indiscernables d'une cote par leur seule forme, et un unique bouton
    global.

Aucun selecteur CSS n'est fourni au bot. S'il lit les six matchs correctement
sur les deux pages, c'est que la detection repose bien sur la structure
numerique et non sur l'habillage.

Le test est ignore si Playwright ou un binaire Chromium ne sont pas
disponibles, pour que la suite unitaire reste executable sans navigateur.
"""

from __future__ import annotations

import os
import unittest
from datetime import timezone
from pathlib import Path

from mpp.schedule import to_site_local

PAGES = Path(__file__).parent / "pages"

ATTENDU = {
    ("AEK Athens", "LASK"): ((65, 124, 129), (70, 21, 8), "18h45"),
    ("Bruges", "Aston Villa"): ((100, 117, 93), (15, 23, 62), "18h45"),
    ("Real Madrid", "Inter"): ((62, 124, 136), (85, 11, 4), "21h00"),
    ("LOSC", "Betis"): ((88, 121, 103), (63, 25, 13), "21h00"),
    ("Dortmund", "Villarreal"): ((73, 119, 122), (81, 15, 4), "21h00"),
    ("Porto", "Man City"): ((127, 124, 67), (7, 12, 81), "21h00"),
}


def _chromium_path() -> str | None:
    explicit = os.environ.get("MPP_CHROMIUM")
    if explicit and Path(explicit).exists():
        return explicit
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    for pattern in ("chromium_headless_shell-*/chrome-linux/headless_shell",
                    "chromium-*/chrome-linux/chrome"):
        found = sorted(root.glob(pattern))
        if found:
            return str(found[-1])
    return None


try:
    import playwright  # noqa: F401  # detecte la presence du paquet

    HAS_PW = True
except ImportError:
    HAS_PW = False

CHROMIUM = _chromium_path() if HAS_PW else None


@unittest.skipUnless(HAS_PW and CHROMIUM, "Playwright ou Chromium indisponible")
class TestBrowserEndToEnd(unittest.TestCase):
    def _site(self, capture=None):
        from mpp.site import MppSite, SiteConfig

        return MppSite(
            SiteConfig(
                base_url=(PAGES / "index.html").as_uri(),
                executable_path=CHROMIUM or "",
                action_delay_s=(0.0, 0.0),
                timeout_ms=10_000,
            ),
            capture_dir=capture,
        )

    # --- lecture -----------------------------------------------------------
    def _read(self, page_name: str):
        from mpp.site import SiteConfig

        with self._site() as site:
            site.cfg.matches_url = (PAGES / page_name).as_uri()
            site.login("moi@example.com", "secret")
            site.open_matches()
            return site.read_cards()

    def test_variant_a_divs_and_obfuscated_classes(self):
        cards, errors = self._read("pronos.html")
        self._assert_matches_reference(cards, errors)

    def test_variant_b_table_no_ids_and_bare_ranks(self):
        """Meme donnees, DOM sans rapport, et des rangs nus qui ressemblent a
        des cotes. C'est le depart par K median qui doit trancher."""
        cards, errors = self._read("grille.html")
        self._assert_matches_reference(cards, errors)

    def _assert_matches_reference(self, cards, errors):
        self.assertEqual(errors, [], f"erreurs de lecture : {errors}")
        self.assertEqual(len(cards), 6, [c.home for c in cards])
        got = {(c.home, c.away): c for c in cards}
        self.assertEqual(set(got), set(ATTENDU), "equipes mal identifiees")
        for key, (cotes, pcts, hhmm) in ATTENDU.items():
            card = got[key]
            self.assertEqual(tuple(card.cotes), tuple(float(c) for c in cotes), key)
            self.assertEqual(tuple(card.crowd_pct), tuple(float(p) for p in pcts), key)
            # Comparaison en heure de Paris, pas en UTC : un test qui code en
            # dur "18h45 -> 16h UTC" passe en ete et casse en novembre.
            self.assertEqual(f"{to_site_local(card.kickoff):%Hh%M}", hhmm, key)
            self.assertEqual(card.kickoff.tzinfo, timezone.utc)
            self.assertFalse(card.locked, key)

    # --- navigation --------------------------------------------------------
    def test_finds_the_predictions_page_on_its_own(self):
        """Aucune URL fournie : le bot part de l'accueil, ferme la banniere
        cookies, trouve le lien de connexion, se connecte, puis trouve la page
        de pronostics en suivant les liens du tableau de bord."""
        with self._site() as site:
            site.login("moi@example.com", "secret")
            site.open_matches()
            cards, errors = site.read_cards()
        self.assertEqual(len(cards), 6)
        self.assertEqual(errors, [])

    # --- ecriture ----------------------------------------------------------
    def test_full_cycle_read_decide_write_verify(self):

        from mpp.strategy import StrategyConfig, build_predictions

        with self._site() as site:
            site.cfg.matches_url = (PAGES / "pronos.html").as_uri()
            site.login("moi@example.com", "secret")
            site.open_matches()
            cards, _ = site.read_cards()

            predictions, kappa = build_predictions(
                cards, StrategyConfig(target_deviations=2)
            )
            self.assertEqual(len(predictions), 6)
            self.assertEqual(sum(p.deviation for p in predictions), 2)

            expected = {}
            for prediction in predictions:
                site.fill_score(
                    prediction.match_id, prediction.home_goals, prediction.away_goals
                )
                site.click_validate(prediction.match_id)
                expected[prediction.match_id] = (
                    prediction.home_goals,
                    prediction.away_goals,
                )

            results = site.verify_all(expected)

        self.assertEqual(len(results), 6)
        self.assertTrue(all(results.values()), f"scores non confirmes : {results}")

    def test_overwrites_an_existing_prediction(self):
        with self._site() as site:
            site.cfg.matches_url = (PAGES / "pronos.html").as_uri()
            site.login("moi@example.com", "secret")
            site.open_matches()
            cards, _ = site.read_cards()
            target = cards[0].match_id

            site.fill_score(target, 4, 4)
            self.assertTrue(site.verify_all({target: (4, 4)})[target])
            site.fill_score(target, 1, 0)
            self.assertTrue(site.verify_all({target: (1, 0)})[target])

    # --- echecs -----------------------------------------------------------
    def test_a_page_without_cards_fails_loudly_with_a_diagnostic(self):
        from mpp.site import SiteError

        with self._site() as site:
            site.cfg.matches_url = (PAGES / "reglement.html").as_uri()
            site.login("moi@example.com", "secret")
            with self.assertRaises(SiteError):
                site.open_matches()
            self.assertTrue(site.diagnostics, "aucun diagnostic produit")
            self.assertIn("introuvable", site.diagnostics[-1].message)

    def test_wrong_credentials_are_reported_not_ignored(self):
        from mpp.site import LoginError

        with self._site() as site:
            with self.assertRaises(LoginError):
                site.login("moi@example.com", "")   # le faux site refuse de soumettre


if __name__ == "__main__":
    unittest.main()
