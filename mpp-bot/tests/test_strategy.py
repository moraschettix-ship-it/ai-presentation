import unittest
from datetime import datetime, timezone

from mpp.models import Card
from mpp.odds import crowd_probabilities, implied_probabilities
from mpp.strategy import StrategyConfig, build_predictions, calibrate_kappa

RAW = [
    ("AEK Athens", "LASK", (65, 124, 129), (70, 21, 8)),
    ("Bruges", "Aston Villa", (100, 117, 93), (15, 23, 62)),
    ("Real Madrid", "Inter", (62, 124, 136), (85, 11, 4)),
    ("LOSC", "Betis", (88, 121, 103), (63, 25, 13)),
    ("Dortmund", "Villarreal", (73, 119, 122), (81, 15, 4)),
    ("Porto", "Man City", (127, 124, 67), (7, 12, 81)),
]
KO = datetime(2026, 9, 15, 19, 0, tzinfo=timezone.utc)


def cards():
    return [
        Card(f"m{i}", h, a, KO, cotes=c, crowd_pct=p)
        for i, (h, a, c, p) in enumerate(RAW)
    ]


def rows():
    return [
        (implied_probabilities(c), crowd_probabilities(p)) for _, _, c, p in RAW
    ]


class TestStrategy(unittest.TestCase):
    def test_kappa_zero_follows_the_market(self):
        preds, kappa = build_predictions(cards(), StrategyConfig(target_deviations=0))
        self.assertEqual(kappa, 0.0)
        self.assertFalse(any(p.deviation for p in preds))

    def test_deviation_count_is_monotone_in_kappa(self):
        counts = []
        for kappa in (0.0, 0.1, 0.2, 0.3, 0.5, 0.8, 1.5, 3.0):
            preds, _ = build_predictions(
                cards(), StrategyConfig(target_deviations=None, kappa=kappa)
            )
            counts.append(sum(p.deviation for p in preds))
        self.assertEqual(counts, sorted(counts))
        self.assertEqual(counts[0], 0)
        self.assertEqual(counts[-1], len(RAW))

    def test_calibration_hits_the_target(self):
        for target in range(0, len(RAW) + 1):
            kappa = calibrate_kappa(rows(), target)
            preds, _ = build_predictions(
                cards(), StrategyConfig(target_deviations=None, kappa=kappa)
            )
            self.assertGreaterEqual(sum(p.deviation for p in preds), target)

    def test_cheapest_deviations_come_first(self):
        """Bruges et LOSC doivent basculer avant Real et Dortmund.

        Sur ces deux matchs le marche est presque a pile ou face alors que la
        foule est massivement d'un cote : la deviation coute quelques points de
        probabilite et rapporte 40 a 50 points de differenciation. C'est la
        propriete qui rend le budget de variance utile plutot qu'arbitraire.
        """
        preds, _ = build_predictions(cards(), StrategyConfig(target_deviations=2))
        deviants = {p.home for p in preds if p.deviation}
        self.assertEqual(deviants, {"Bruges", "LOSC"})

    def test_every_deviation_has_a_positive_edge(self):
        for target in (1, 3, 5):
            preds, _ = build_predictions(cards(), StrategyConfig(target_deviations=target))
            for p in preds:
                if p.deviation:
                    self.assertGreater(p.edge, 0.0, msg=f"{p.home} - {p.away}")

    def test_score_is_consistent_with_the_chosen_outcome(self):
        preds, _ = build_predictions(cards(), StrategyConfig(target_deviations=3))
        for p in preds:
            got = "1" if p.home_goals > p.away_goals else (
                "N" if p.home_goals == p.away_goals else "2"
            )
            self.assertEqual(got, p.outcome, msg=f"{p.home} - {p.away}")

    def test_expected_points_stay_flat_whatever_the_pick(self):
        """Corollaire de cote = K / p : le budget de variance est GRATUIT en
        esperance de points. Il ne coute que de la variance."""
        flat, _ = build_predictions(cards(), StrategyConfig(target_deviations=0))
        wild, _ = build_predictions(cards(), StrategyConfig(target_deviations=6))
        for a, b in zip(flat, wild):
            self.assertAlmostEqual(a.expected_points, b.expected_points, places=6)

    def test_catchup_increases_aggression(self):
        calm, _ = build_predictions(
            cards(), StrategyConfig(target_deviations=1, catchup_kappa=0.0)
        )
        angry, _ = build_predictions(
            cards(), StrategyConfig(target_deviations=1, catchup_kappa=1.0)
        )
        self.assertGreater(
            sum(p.deviation for p in angry), sum(p.deviation for p in calm)
        )

    def test_cards_without_data_are_skipped_not_guessed(self):
        broken = cards()
        broken[0].cotes = None
        broken[1].crowd_pct = None
        preds, _ = build_predictions(broken, StrategyConfig(target_deviations=1))
        self.assertEqual(len(preds), len(RAW) - 2)


if __name__ == "__main__":
    unittest.main()
