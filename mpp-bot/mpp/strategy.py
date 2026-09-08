"""Choix du pronostic.

Point de depart, demontre dans README : sur MPP, cote = K / p, donc

    esperance de points = p_i * cote_i = K,  IDENTIQUE pour les trois issues.

Il n'existe aucun arbitrage en esperance. Jouer le favori du marche ne
"rapporte" pas plus que jouer l'outsider. Le seul levier reel pour finir
premier d'une ligue est donc la DIFFERENCIATION vis-a-vis des autres joueurs.

Utilite retenue, un seul parametre :

    U_i = p_i + kappa * (p_i - foule_i)

- kappa = 0   -> favori du marche (le plus souvent = favori de la foule -> median)
- kappa -> inf -> ecart maximal a la foule (variance maximale)

Propriete utile : quand kappa croit a partir de 0, les deviations apparaissent
dans l'ordre exact de leur efficacite, c'est-a-dire du meilleur rapport
"differenciation gagnee / probabilite sacrifiee". Les premiers matchs a basculer
sont ceux ou le marche est proche d'un pile ou face alors que la foule est
massivement d'un cote : ces deviations coutent presque rien et rapportent
beaucoup de rang.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import OUTCOMES, Card, Prediction
from .odds import Probabilities, crowd_probabilities, edges, implied_probabilities
from .poisson import fit_lambdas_anchored, most_likely_score, score_matrix


@dataclass
class StrategyConfig:
    kappa: float = 0.35
    # Si defini, kappa est re-calibre a chaque journee pour produire exactement
    # ce nombre de deviations vis-a-vis du favori du marche. Prime sur `kappa`.
    target_deviations: int | None = 5
    kappa_max: float = 3.0
    # Bonus de retard : ajoute a kappa quand on court apres le leader.
    catchup_kappa: float = 0.0
    # Modele de buts
    rho: float = 0.0
    total_prior: float = 3.05
    total_shrink: float = 0.75
    # Bareme MPP (a confirmer, cf README) : points = cote si bonne issue,
    # + bonus si score exact. Sert uniquement a reporter une esperance, pas a
    # choisir l'issue (l'esperance d'issue etant plate par construction).
    exact_bonus: float = 0.0


def _utility(p: Probabilities, c: Probabilities, kappa: float) -> dict[str, float]:
    return {o: p.of(o) + kappa * (p.of(o) - c.of(o)) for o in OUTCOMES}


def _argmax(d: dict[str, float]) -> str:
    return max(d, key=lambda k: d[k])


def _pick_outcome(p: Probabilities, c: Probabilities, kappa: float) -> str:
    return _argmax(_utility(p, c, kappa))


def _deviations_at(rows: list[tuple[Probabilities, Probabilities]], kappa: float) -> int:
    """Nombre de matchs ou l'utilite s'ecarte du favori du marche."""
    n = 0
    for p, c in rows:
        if _pick_outcome(p, c, kappa) != _argmax({o: p.of(o) for o in OUTCOMES}):
            n += 1
    return n


def calibrate_kappa(
    rows: list[tuple[Probabilities, Probabilities]],
    target: int,
    kappa_max: float = 3.0,
) -> float:
    """Plus petit kappa produisant au moins `target` deviations.

    Le nombre de deviations est croissant (par paliers) en kappa : bissection.
    Si meme kappa_max ne suffit pas, on renvoie kappa_max.
    """
    if target <= 0 or not rows:
        return 0.0
    if _deviations_at(rows, kappa_max) < target:
        return kappa_max

    lo, hi = 0.0, kappa_max
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if _deviations_at(rows, mid) >= target:
            hi = mid
        else:
            lo = mid
    return hi


def build_predictions(
    cards: list[Card], cfg: StrategyConfig
) -> tuple[list[Prediction], float]:
    """Transforme des cartes en pronostics. Renvoie (pronostics, kappa utilise).

    Les cartes sans cotes ou sans repartition exploitables sont ignorees ici :
    l'appelant les traite comme des erreurs (cf cli.py), il ne faut jamais
    "deviner" un pronostic sur des donnees douteuses.
    """
    usable: list[tuple[Card, Probabilities, Probabilities]] = []
    for card in cards:
        if card.cotes is None or card.crowd_pct is None:
            continue
        market = implied_probabilities(card.cotes)
        crowd = crowd_probabilities(card.crowd_pct)
        usable.append((card, market, crowd))

    rows = [(m, c) for _, m, c in usable]
    if cfg.target_deviations is not None:
        kappa = calibrate_kappa(rows, cfg.target_deviations, cfg.kappa_max)
    else:
        kappa = cfg.kappa
    kappa += cfg.catchup_kappa

    predictions: list[Prediction] = []
    for card, market, crowd in usable:
        assert card.cotes is not None and card.crowd_pct is not None
        outcome = _pick_outcome(market, crowd, kappa)
        market_fav = _argmax({o: market.of(o) for o in OUTCOMES})
        crowd_fav = _argmax({o: crowd.of(o) for o in OUTCOMES})

        lambdas = fit_lambdas_anchored(
            *market.as_tuple(),
            rho=cfg.rho,
            total_prior=cfg.total_prior,
            shrink=cfg.total_shrink,
        )
        matrix = score_matrix(lambdas, cfg.rho)
        hg, ag = most_likely_score(matrix, outcome)

        p_out = market.of(outcome)
        cote = dict(zip(OUTCOMES, card.cotes))[outcome]
        p_exact = matrix[(hg, ag)]
        ed = edges(market, crowd)[outcome]

        predictions.append(
            Prediction(
                match_id=card.match_id,
                home=card.home,
                away=card.away,
                kickoff=card.kickoff,
                outcome=outcome,
                home_goals=hg,
                away_goals=ag,
                market=market.as_tuple(),
                crowd=crowd.as_tuple(),
                cotes=card.cotes,
                edge=ed,
                deviation=outcome != market_fav,
                contrarian=outcome != crowd_fav,
                p_outcome=p_out,
                p_exact=p_exact,
                expected_points=p_out * cote + p_exact * cfg.exact_bonus,
                lambdas=(lambdas.home, lambdas.away),
                reason=_explain(outcome, market_fav, crowd_fav, ed),
            )
        )
    return predictions, kappa


def _explain(outcome: str, market_fav: str, crowd_fav: str, edge: float) -> str:
    if outcome == market_fav and outcome == crowd_fav:
        return "consensus marche + foule"
    if outcome == market_fav:
        return f"favori du marche, la foule est ailleurs (edge {edge:+.1%})"
    if outcome != crowd_fav:
        return f"deviation contrarienne (edge {edge:+.1%})"
    return f"deviation vers le favori de la foule (edge {edge:+.1%})"
