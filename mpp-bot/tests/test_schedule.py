import unittest
from datetime import date, datetime, timedelta, timezone

from mpp.models import Card
from mpp.schedule import (
    ScheduleError,
    Windows,
    due_cards,
    is_due,
    next_wake_up,
    parse_site_time,
)

UTC = timezone.utc


def card(minutes_ahead: float, locked: bool = False) -> Card:
    now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    return Card("m", "H", "A", now + timedelta(minutes=minutes_ahead), locked=locked)


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


class TestSiteTime(unittest.TestCase):
    def test_winter_and_summer_offsets(self):
        # CET (UTC+1) en janvier
        self.assertEqual(
            parse_site_time(date(2026, 1, 20), "21h00"),
            datetime(2026, 1, 20, 20, 0, tzinfo=UTC),
        )
        # CEST (UTC+2) en septembre
        self.assertEqual(
            parse_site_time(date(2026, 9, 15), "18h45"),
            datetime(2026, 9, 15, 16, 45, tzinfo=UTC),
        )

    def test_dst_switch_days(self):
        """Les jours de bascule sont la ou un decalage code en dur casse."""
        self.assertEqual(   # dimanche du passage a l'heure d'ete
            parse_site_time(date(2026, 3, 29), "21h00"),
            datetime(2026, 3, 29, 19, 0, tzinfo=UTC),
        )
        self.assertEqual(   # dimanche du retour a l'heure d'hiver
            parse_site_time(date(2026, 10, 25), "21h00"),
            datetime(2026, 10, 25, 20, 0, tzinfo=UTC),
        )

    def test_accepted_formats(self):
        for text in ("18h45", "18H45", "18:45", " 18h45 "):
            self.assertEqual(
                parse_site_time(date(2026, 9, 15), text),
                datetime(2026, 9, 15, 16, 45, tzinfo=UTC),
            )
        self.assertEqual(
            parse_site_time(date(2026, 9, 15), "21h"),
            datetime(2026, 9, 15, 19, 0, tzinfo=UTC),
        )

    def test_rejects_nonsense(self):
        for bad in ("", "ce soir", "25h00", "18h99", "18-45"):
            with self.assertRaises(ScheduleError, msg=bad):
                parse_site_time(date(2026, 9, 15), bad)


class TestWindows(unittest.TestCase):
    W = Windows()

    def test_early_window(self):
        self.assertTrue(is_due(card(24 * 60), NOW, self.W, "early"))
        self.assertFalse(is_due(card(60), NOW, self.W, "early"))       # trop proche
        self.assertFalse(is_due(card(72 * 60), NOW, self.W, "early"))  # trop loin

    def test_late_window(self):
        self.assertTrue(is_due(card(30), NOW, self.W, "late"))
        self.assertTrue(is_due(card(85), NOW, self.W, "late"))
        self.assertFalse(is_due(card(120), NOW, self.W, "late"))

    def test_deadline_margin_is_absolute(self):
        """Aucune passe ne doit tirer trop pres du coup d'envoi."""
        for phase in ("early", "late", "any"):
            self.assertFalse(is_due(card(3), NOW, self.W, phase), phase)
            self.assertFalse(is_due(card(-10), NOW, self.W, phase), phase)  # deja commence

    def test_locked_cards_are_never_due(self):
        for phase in ("early", "late", "any"):
            self.assertFalse(is_due(card(30, locked=True), NOW, self.W, phase))

    def test_late_cron_delay_is_absorbed(self):
        """Un cron retarde de 15 min doit encore trouver le match eligible.

        Fenetre T-90 -> T-15 : une passe prevue a T-60 et executee a T-45,
        T-30 ou T-20 reste dans la fenetre. C'est la garantie qui remplace la
        ponctualite, que GitHub Actions ne fournit pas.
        """
        for actual in (60, 45, 30, 20, 16):
            self.assertTrue(is_due(card(actual), NOW, self.W, "late"), actual)

    def test_due_cards_are_sorted_by_kickoff(self):
        cards = [card(80), card(20), card(50)]
        got = due_cards(cards, NOW, self.W, "late")
        self.assertEqual([c.kickoff for c in got], sorted(c.kickoff for c in got))

    def test_next_wake_up(self):
        cards = [card(300), card(600)]
        wake = next_wake_up(cards, NOW, self.W)
        self.assertEqual(wake, cards[0].kickoff - timedelta(minutes=self.W.late_max))
        self.assertIsNone(next_wake_up([card(-100)], NOW, self.W))


if __name__ == "__main__":
    unittest.main()


class TestResolveKickoff(unittest.TestCase):
    """Datation d'une carte : c'est la que se joue le cas 'page consultee le
    soir qui affiche deja les matchs du lendemain'."""

    def test_explicit_date_wins(self):
        from mpp.schedule import resolve_kickoff

        now = datetime(2026, 9, 15, 21, 30, tzinfo=UTC)      # 23h30 a Paris
        self.assertEqual(
            resolve_kickoff("18h45", "16/09", now),
            datetime(2026, 9, 16, 16, 45, tzinfo=UTC),
        )

    def test_explicit_date_picks_the_nearest_year(self):
        """Un match du 31/12 lu le 1er janvier appartient a l'annee ecoulee.

        Sans ce choix, le bot daterait le match du 31 decembre PROCHAIN et
        l'ignorerait pendant un an.
        """
        from mpp.schedule import resolve_kickoff

        self.assertEqual(
            resolve_kickoff("21h00", "31/12", datetime(2027, 1, 1, 10, 0, tzinfo=UTC)),
            datetime(2026, 12, 31, 20, 0, tzinfo=UTC),
        )
        self.assertEqual(
            resolve_kickoff("21h00", "02/01", datetime(2026, 12, 31, 10, 0, tzinfo=UTC)),
            datetime(2027, 1, 2, 20, 0, tzinfo=UTC),
        )

    def test_without_date_recent_past_stays_today(self):
        """Un match commence il y a deux heures reste date d'aujourd'hui, pour
        etre correctement ecarte comme passe plutot que reporte a demain."""
        from mpp.schedule import resolve_kickoff

        now = datetime(2026, 9, 15, 21, 30, tzinfo=UTC)      # 23h30 a Paris
        self.assertEqual(
            resolve_kickoff("21h00", None, now),
            datetime(2026, 9, 15, 19, 0, tzinfo=UTC),
        )

    def test_without_date_distant_past_rolls_to_tomorrow(self):
        from mpp.schedule import resolve_kickoff

        now = datetime(2026, 9, 15, 22, 0, tzinfo=UTC)       # minuit a Paris
        self.assertEqual(
            resolve_kickoff("12h00", None, now),
            datetime(2026, 9, 16, 10, 0, tzinfo=UTC),
        )

    def test_without_date_future_is_today(self):
        from mpp.schedule import resolve_kickoff

        now = datetime(2026, 9, 15, 8, 0, tzinfo=UTC)        # 10h00 a Paris
        self.assertEqual(
            resolve_kickoff("21h00", None, now),
            datetime(2026, 9, 15, 19, 0, tzinfo=UTC),
        )

    def test_malformed_date_falls_back_instead_of_crashing(self):
        from mpp.schedule import resolve_kickoff

        now = datetime(2026, 9, 15, 8, 0, tzinfo=UTC)
        for bad in ("32/13", "demain", "15-09", ""):
            self.assertEqual(
                resolve_kickoff("21h00", bad, now),
                datetime(2026, 9, 15, 19, 0, tzinfo=UTC),
                bad,
            )
