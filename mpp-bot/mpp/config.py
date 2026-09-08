"""Chargement de config.yaml vers des dataclasses typees."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from .schedule import Windows
from .site import SiteConfig
from .strategy import StrategyConfig


@dataclass
class AppConfig:
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    windows: Windows = field(default_factory=Windows)
    site: SiteConfig = field(default_factory=SiteConfig)
    state_dir: str = "state"
    competition_filter: list[str] = field(default_factory=list)
    max_matches_per_run: int = 40


def _fill(cls, data: dict[str, Any]):
    """Instancie une dataclass en refusant les cles inconnues.

    Une cle inconnue est presque toujours une faute de frappe dans config.yaml,
    qui ferait tourner le bot avec la valeur par defaut sans rien dire.
    """
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(
            f"cles inconnues dans la section {cls.__name__} : {sorted(unknown)} "
            f"(connues : {sorted(known)})"
        )
    return cls(**data)


def load(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    site_raw = dict(raw.get("site", {}) or {})
    if "action_delay_s" in site_raw:
        site_raw["action_delay_s"] = tuple(site_raw["action_delay_s"])

    return AppConfig(
        strategy=_fill(StrategyConfig, raw.get("strategy", {}) or {}),
        windows=_fill(Windows, raw.get("windows", {}) or {}),
        site=_fill(SiteConfig, site_raw),
        state_dir=raw.get("state_dir", "state"),
        competition_filter=list(raw.get("competition_filter", []) or []),
        max_matches_per_run=int(raw.get("max_matches_per_run", 40)),
    )
