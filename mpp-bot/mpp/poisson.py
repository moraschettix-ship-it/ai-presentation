"""Distribution de scores a partir des probabilites d'issue.

Les cotes MPP donnent 3 nombres (P1, PN, P2). Pour choisir un SCORE il faut
une distribution jointe sur les buts. On ajuste un modele de Poisson double
(avec correction Dixon-Coles optionnelle sur les petits scores) dont les
probabilites d'issue reproduisent exactement le triplet observe.

Deux parametres (lambda_domicile, lambda_exterieur) pour deux contraintes
independantes (le triplet somme a 1) : le systeme est exactement determine,
donc l'ajustement est exact, pas approche.

Implementation en Python pur : pas de numpy/scipy, resolution par bissection
imbriquee sur (total de buts, superiorite).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MAX_GOALS = 10  # 0..10 par equipe couvre >99.99% de la masse en football
_TOL = 1e-10
_MAX_ITER = 200


class FitError(RuntimeError):
    """L'ajustement Poisson n'a pas converge."""


@dataclass(frozen=True)
class Lambdas:
    home: float
    away: float


def _poisson_pmf(k: int, lam: float) -> float:
    return math.exp(-lam) * lam**k / math.factorial(k)


def _dc_tau(i: int, j: int, lh: float, la: float, rho: float) -> float:
    """Correction Dixon-Coles : depend des 4 scores a 0 ou 1 but."""
    if rho == 0.0:
        return 1.0
    if i == 0 and j == 0:
        return 1.0 - lh * la * rho
    if i == 0 and j == 1:
        return 1.0 + lh * rho
    if i == 1 and j == 0:
        return 1.0 + la * rho
    if i == 1 and j == 1:
        return 1.0 - rho
    return 1.0


def score_matrix(
    lambdas: Lambdas, rho: float = 0.0, max_goals: int = MAX_GOALS
) -> dict[tuple[int, int], float]:
    """P(score = i-j) pour i, j dans 0..max_goals, renormalise a 1."""
    lh, la = lambdas.home, lambdas.away
    home_pmf = [_poisson_pmf(i, lh) for i in range(max_goals + 1)]
    away_pmf = [_poisson_pmf(j, la) for j in range(max_goals + 1)]

    matrix: dict[tuple[int, int], float] = {}
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            tau = _dc_tau(i, j, lh, la, rho)
            if tau <= 0.0:
                # rho trop agressif pour ces lambdas : la correction rendrait la
                # probabilite negative. On clippe plutot que de produire un
                # modele invalide.
                tau = 1e-9
            matrix[(i, j)] = home_pmf[i] * away_pmf[j] * tau

    total = sum(matrix.values())
    return {k: v / total for k, v in matrix.items()}


def outcome_probabilities(
    matrix: dict[tuple[int, int], float],
) -> tuple[float, float, float]:
    """Agrege une matrice de scores en (P1, PN, P2)."""
    p1 = pn = p2 = 0.0
    for (i, j), p in matrix.items():
        if i > j:
            p1 += p
        elif i == j:
            pn += p
        else:
            p2 += p
    return p1, pn, p2


def _outcomes_for(total: float, supremacy: float, rho: float):
    lh = (total + supremacy) / 2.0
    la = (total - supremacy) / 2.0
    lh = max(lh, 1e-6)
    la = max(la, 1e-6)
    return outcome_probabilities(score_matrix(Lambdas(lh, la), rho)), Lambdas(lh, la)


def _solve_supremacy(total: float, target_diff: float, rho: float) -> float:
    """Trouve s tel que P1 - P2 = target_diff, a total de buts fixe.

    P1 - P2 est strictement croissant en s : bissection sure.
    """
    lo, hi = -total * 0.999, total * 0.999
    for _ in range(_MAX_ITER):
        mid = (lo + hi) / 2.0
        (p1, _pn, p2), _ = _outcomes_for(total, mid, rho)
        if (p1 - p2) < target_diff:
            lo = mid
        else:
            hi = mid
        if hi - lo < _TOL:
            break
    return (lo + hi) / 2.0


def fit_lambdas(
    p_home: float, p_draw: float, p_away: float, rho: float = 0.0
) -> Lambdas:
    """Ajuste (lambda_dom, lambda_ext) pour reproduire le triplet d'issues.

    P(nul) decroit quand le total de buts augmente (a ecart d'issues constant) :
    bissection sure sur le total.
    """
    if min(p_home, p_draw, p_away) <= 0.0:
        raise FitError(f"probabilite nulle ou negative : {(p_home, p_draw, p_away)}")
    if abs(p_home + p_draw + p_away - 1.0) > 1e-6:
        raise FitError(f"triplet non normalise : {(p_home, p_draw, p_away)}")

    target_diff = p_home - p_away
    lo, hi = 0.30, 9.0  # totaux de buts attendus plausibles, tres large

    for _ in range(_MAX_ITER):
        total = (lo + hi) / 2.0
        s = _solve_supremacy(total, target_diff, rho)
        (_p1, pn, _p2), lambdas = _outcomes_for(total, s, rho)
        if pn > p_draw:
            lo = total  # trop de nuls -> il faut plus de buts
        else:
            hi = total
        if hi - lo < 1e-9:
            break

    total = (lo + hi) / 2.0
    s = _solve_supremacy(total, target_diff, rho)
    (p1, pn, p2), lambdas = _outcomes_for(total, s, rho)

    err = max(abs(p1 - p_home), abs(pn - p_draw), abs(p2 - p_away))
    if err > 1e-3:
        raise FitError(
            f"convergence insuffisante (err={err:.2e}) pour "
            f"{(p_home, p_draw, p_away)} avec rho={rho}"
        )
    return lambdas


def most_likely_score(
    matrix: dict[tuple[int, int], float], outcome: str
) -> tuple[int, int]:
    """Score le plus probable parmi ceux compatibles avec l'issue demandee."""
    def matches(i: int, j: int) -> bool:
        return {"1": i > j, "N": i == j, "2": i < j}[outcome]

    best = max(
        (kv for kv in matrix.items() if matches(*kv[0])),
        key=lambda kv: (kv[1], -(kv[0][0] + kv[0][1])),
    )
    return best[0]


def fit_lambdas_anchored(
    p_home: float,
    p_draw: float,
    p_away: float,
    rho: float = 0.0,
    total_prior: float = 3.05,
    shrink: float = 0.0,
) -> Lambdas:
    """Ajustement avec ancrage optionnel du total de buts.

    Pourquoi : un triplet 1/N/2 determine le total de buts uniquement via la
    probabilite de nul. Sur les cartes MPP, cela donne des totaux ajustes
    autour de 2.2-2.5, alors qu'un match de C1 tourne plutot vers 3.0-3.2.
    Consequence : le score exact le plus probable est systematiquement tire
    vers le bas (1-0, 1-1, 0-1), ce qui coute des bonus "score exact".

    `shrink` (0..1) tire le total ajuste vers `total_prior`. La superiorite est
    ensuite re-resolue pour preserver exactement P1 - P2 (donc l'ordre des
    issues et le choix 1/N/2 restent intacts) ; seule P(nul) devie du triplet.

    shrink = 0.0 -> ajustement exact (defaut, aucune hypothese ajoutee)
    shrink = 1.0 -> total force au prior
    """
    if not (0.0 <= shrink <= 1.0):
        raise FitError(f"shrink doit etre dans [0, 1], recu {shrink}")

    exact = fit_lambdas(p_home, p_draw, p_away, rho)
    if shrink == 0.0:
        return exact

    fitted_total = exact.home + exact.away
    total = (1.0 - shrink) * fitted_total + shrink * total_prior
    s = _solve_supremacy(total, p_home - p_away, rho)
    _, lambdas = _outcomes_for(total, s, rho)
    return lambdas
