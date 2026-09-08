import unittest

from mpp.odds import (
    OddsError,
    crowd_probabilities,
    edges,
    implied_probabilities,
    scale_constant,
)

# Les six cartes de la capture d'ecran de reference.
CARDS = [
    ((65, 124, 129), (70, 21, 8)),
    ((100, 117, 93), (15, 23, 62)),
    ((62, 124, 136), (85, 11, 4)),
    ((88, 121, 103), (63, 25, 13)),
    ((73, 119, 122), (81, 15, 4)),
    ((127, 124, 67), (7, 12, 81)),
]


class TestOdds(unittest.TestCase):
    def test_probabilities_sum_to_one(self):
        for cotes, _ in CARDS:
            p = implied_probabilities(cotes)
            self.assertAlmostEqual(sum(p.as_tuple()), 1.0, places=12)

    def test_expected_points_are_flat(self):
        """Propriete centrale : cote = K / p, donc p * cote = K sur les 3 issues.

        C'est ce qui prouve qu'aucune issue n'est meilleure qu'une autre en
        esperance, et donc que toute la strategie doit porter sur la
        differenciation, pas sur la selection de valeur.
        """
        for cotes, _ in CARDS:
            p = implied_probabilities(cotes)
            evs = [p.as_tuple()[i] * cotes[i] for i in range(3)]
            self.assertAlmostEqual(max(evs), min(evs), places=9, msg=f"{cotes}")
            self.assertAlmostEqual(evs[0], scale_constant(cotes), places=9)

    def test_probabilities_are_football_plausible(self):
        """Garde-fou de non-regression sur le decodage lui-meme."""
        p = implied_probabilities((62, 124, 136))     # Real Madrid - Inter
        self.assertAlmostEqual(p.home, 0.511, places=3)
        self.assertAlmostEqual(p.draw, 0.256, places=3)
        self.assertAlmostEqual(p.away, 0.233, places=3)
        for cotes, _ in CARDS:
            self.assertTrue(0.18 <= implied_probabilities(cotes).draw <= 0.32)

    def test_rejects_garbage(self):
        for bad in [(0, 124, 136), (62, -1, 136), (5, 5, 5), (62, 124, 1e9)]:
            with self.assertRaises(OddsError, msg=f"{bad}"):
                implied_probabilities(bad)

    def test_crowd_normalisation_and_rejection(self):
        c = crowd_probabilities((70, 21, 8))     # somme 99, arrondis MPP
        self.assertAlmostEqual(sum(c.as_tuple()), 1.0, places=12)
        with self.assertRaises(OddsError):
            crowd_probabilities((70, 21, 40))    # somme 131 : parsing rate

    def test_edge_is_market_minus_crowd(self):
        p = implied_probabilities((62, 124, 136))
        c = crowd_probabilities((85, 11, 4))
        e = edges(p, c)
        self.assertAlmostEqual(e["1"], p.home - c.home, places=12)
        self.assertLess(e["1"], 0)      # la foule surestime largement le Real
        self.assertGreater(e["2"], 0)   # et sous-estime l'Inter
        self.assertAlmostEqual(sum(e.values()), 0.0, places=12)


if __name__ == "__main__":
    unittest.main()
