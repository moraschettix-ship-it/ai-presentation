import unittest

from mpp.odds import implied_probabilities
from mpp.poisson import (
    FitError,
    fit_lambdas,
    fit_lambdas_anchored,
    most_likely_score,
    outcome_probabilities,
    score_matrix,
)

TRIPLETS = [(65, 124, 129), (100, 117, 93), (62, 124, 136), (127, 124, 67)]


class TestPoisson(unittest.TestCase):
    def test_fit_reproduces_the_triplet_exactly(self):
        for cotes in TRIPLETS:
            p = implied_probabilities(cotes)
            lambdas = fit_lambdas(*p.as_tuple())
            back = outcome_probabilities(score_matrix(lambdas))
            for got, want in zip(back, p.as_tuple()):
                self.assertAlmostEqual(got, want, places=5, msg=f"{cotes}")

    def test_matrix_is_a_distribution(self):
        lambdas = fit_lambdas(*implied_probabilities((62, 124, 136)).as_tuple())
        matrix = score_matrix(lambdas)
        self.assertAlmostEqual(sum(matrix.values()), 1.0, places=10)
        self.assertTrue(all(v >= 0 for v in matrix.values()))

    def test_favourite_gets_higher_lambda(self):
        p = implied_probabilities((62, 124, 136))      # domicile favori
        lambdas = fit_lambdas(*p.as_tuple())
        self.assertGreater(lambdas.home, lambdas.away)

    def test_most_likely_score_respects_the_outcome(self):
        lambdas = fit_lambdas(*implied_probabilities((62, 124, 136)).as_tuple())
        matrix = score_matrix(lambdas)
        h, a = most_likely_score(matrix, "1")
        self.assertGreater(h, a)
        h, a = most_likely_score(matrix, "N")
        self.assertEqual(h, a)
        h, a = most_likely_score(matrix, "2")
        self.assertLess(h, a)

    def test_anchoring_raises_the_total_without_flipping_the_outcome(self):
        p = implied_probabilities((62, 124, 136))
        low = fit_lambdas_anchored(*p.as_tuple(), shrink=0.0)
        high = fit_lambdas_anchored(*p.as_tuple(), total_prior=3.05, shrink=1.0)
        self.assertLess(low.home + low.away, high.home + high.away)
        self.assertAlmostEqual(high.home + high.away, 3.05, places=6)
        # L'ecart d'issues P1 - P2 est preserve : l'ancrage ne change jamais
        # le choix 1/N/2, uniquement le score.
        for lambdas in (low, high):
            p1, _, p2 = outcome_probabilities(score_matrix(lambdas))
            self.assertAlmostEqual(p1 - p2, p.home - p.away, places=4)

    def test_dixon_coles_keeps_a_valid_distribution(self):
        p = implied_probabilities((100, 117, 93))
        for rho in (-0.15, -0.05, 0.0, 0.05, 0.15):
            lambdas = fit_lambdas(*p.as_tuple(), rho=rho)
            matrix = score_matrix(lambdas, rho)
            self.assertAlmostEqual(sum(matrix.values()), 1.0, places=10)
            self.assertTrue(all(v >= 0 for v in matrix.values()))

    def test_rejects_degenerate_input(self):
        with self.assertRaises(FitError):
            fit_lambdas(0.0, 0.5, 0.5)
        with self.assertRaises(FitError):
            fit_lambdas(0.5, 0.5, 0.5)      # ne somme pas a 1
        with self.assertRaises(FitError):
            fit_lambdas_anchored(0.5, 0.25, 0.25, shrink=1.5)


if __name__ == "__main__":
    unittest.main()
