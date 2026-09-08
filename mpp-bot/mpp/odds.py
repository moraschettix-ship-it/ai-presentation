"""Decodage des cotes MPP.

Constat etabli empiriquement sur un echantillon de cartes MPP (cf README,
section "Decodage des cotes") : les trois nombres affiches sous une carte
verifient

    cote_i = K / p_i

ou p_i est la probabilite de l'issue i (1 / N / 2) et K une constante propre
au match. Consequence directe : la somme des inverses des cotes vaut 1/K, donc

    p_i = (1 / cote_i) / somme_j (1 / cote_j)

Cette normalisation est invariante d'echelle : elle elimine K, donc on n'a
jamais besoin de connaitre sa valeur. Les probabilites obtenues sont deja
sans marge bookmaker (elles somment a 1 par construction).

Corollaire strategique majeur : si les points gagnes valent la cote, alors
l'esperance de points vaut p_i * K / p_i = K, IDENTIQUE pour les trois issues.
Suivre la cote ne rapporte rien. Voir strategy.py.
"""

from __future__ import annotations

from dataclasses import dataclass

# Bornes de plausibilite. Tout ce qui sort de la est traite comme une anomalie
# de parsing (DOM change, valeur tronquee, pourcentage lu comme une cote...).
COTE_MIN = 20.0
COTE_MAX = 2000.0
# K observe entre 31.7 et 34.1 sur l'echantillon de reference. On laisse large :
# un K aberrant signale surtout un triplet incoherent (ex : deux cotes du meme
# match melangees avec celles d'un autre).
K_MIN = 10.0
K_MAX = 200.0


class OddsError(ValueError):
    """Triplet de cotes invalide ou implausible."""


@dataclass(frozen=True)
class Probabilities:
    """Probabilites d'issue, normalisees a 1."""

    home: float
    draw: float
    away: float

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.home, self.draw, self.away)

    def of(self, outcome: str) -> float:
        return {"1": self.home, "N": self.draw, "2": self.away}[outcome]


def implied_probabilities(cotes: tuple[float, float, float]) -> Probabilities:
    """Convertit un triplet de cotes MPP (1, N, 2) en probabilites."""
    for c in cotes:
        if not isinstance(c, (int, float)) or c != c:  # NaN-safe
            raise OddsError(f"cote non numerique : {cotes!r}")
        if not (COTE_MIN <= c <= COTE_MAX):
            raise OddsError(f"cote hors bornes [{COTE_MIN}, {COTE_MAX}] : {cotes!r}")

    inverses = [1.0 / c for c in cotes]
    total = sum(inverses)
    k = 1.0 / total
    if not (K_MIN <= k <= K_MAX):
        raise OddsError(f"K={k:.2f} implausible pour {cotes!r} (triplet incoherent ?)")

    return Probabilities(*[i / total for i in inverses])


def scale_constant(cotes: tuple[float, float, float]) -> float:
    """Renvoie K = 1 / somme(1/cote). Utile pour le suivi de calibration."""
    return 1.0 / sum(1.0 / c for c in cotes)


def crowd_probabilities(percentages: tuple[float, float, float]) -> Probabilities:
    """Normalise les pourcentages de repartition des joueurs (foule).

    MPP arrondit a l'entier, donc la somme vaut rarement exactement 100.
    On renormalise, et on refuse un total trop eloigne de 100 (parsing rate).
    """
    total = sum(percentages)
    if not (90.0 <= total <= 110.0):
        raise OddsError(f"repartition foule invalide, somme={total} : {percentages!r}")
    if any(p < 0 for p in percentages):
        raise OddsError(f"pourcentage negatif : {percentages!r}")
    return Probabilities(*[p / total for p in percentages])


def edges(market: Probabilities, crowd: Probabilities) -> dict[str, float]:
    """Ecart marche - foule par issue.

    C'est l'esperance de gain de rang : si vous jouez l'issue i, vous battez la
    part (1 - foule_i) du terrain quand elle sort, et vous etes battu par la
    part foule_j quand c'est j qui sort. L'algebre se simplifie exactement en
    (p_i - foule_i). Positif = le terrain sous-estime cette issue.
    """
    return {
        "1": market.home - crowd.home,
        "N": market.draw - crowd.draw,
        "2": market.away - crowd.away,
    }
