"""Types de donnees partages."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any

OUTCOMES = ("1", "N", "2")


@dataclass
class Card:
    """Une carte de match lue sur MPP."""

    match_id: str
    home: str
    away: str
    kickoff: datetime           # timezone-aware, toujours en UTC en interne
    matchday: str | None = None
    competition: str | None = None
    cotes: tuple[float, float, float] | None = None       # 1, N, 2
    crowd_pct: tuple[float, float, float] | None = None    # 1, N, 2, en %
    existing_pick: tuple[int, int] | None = None           # score deja saisi
    locked: bool = False        # MPP a ferme la saisie
    raw: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return self.match_id


@dataclass
class Prediction:
    """Decision du bot pour un match."""

    match_id: str
    home: str
    away: str
    kickoff: datetime
    outcome: str                # "1", "N" ou "2"
    home_goals: int
    away_goals: int
    market: tuple[float, float, float]
    crowd: tuple[float, float, float]
    cotes: tuple[float, float, float]
    edge: float                 # p_marche - p_foule sur l'issue choisie
    deviation: bool             # differe du favori du marche
    contrarian: bool            # differe du favori de la foule
    p_outcome: float
    p_exact: float
    expected_points: float
    lambdas: tuple[float, float]
    reason: str = ""

    def score_str(self) -> str:
        return f"{self.home_goals}-{self.away_goals}"

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["kickoff"] = self.kickoff.isoformat()
        return d
