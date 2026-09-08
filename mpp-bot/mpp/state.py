"""Journal persistant : tracabilite, idempotence, et cache pour le mode gate.

Trois fichiers, tous commites par le workflow :

- `journal.jsonl` : append-only, une ligne par evenement. C'est la piste d'audit
  qui permet de relire a la main ce que le bot a fait, et la source de donnees
  du recalibrage (cf `calibrate`).
- `fixtures.json` : dernier calendrier connu. Permet au workflow de decider
  s'il y a quelque chose a faire AVANT d'installer Chromium.
- `last_run.json` : resume du dernier passage, pour l'alerting.

Note : le fait meme de commiter ces fichiers a un effet de bord utile sur
GitHub Actions, qui desactive les workflows planifies apres 60 jours sans
activite sur le depot. Les commits du bot maintiennent le cron en vie.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import Card, Prediction


@dataclass
class Store:
    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    # --- chemins -----------------------------------------------------------
    @property
    def journal_path(self) -> Path:
        return self.root / "journal.jsonl"

    @property
    def fixtures_path(self) -> Path:
        return self.root / "fixtures.json"

    @property
    def last_run_path(self) -> Path:
        return self.root / "last_run.json"

    # --- ecriture ----------------------------------------------------------
    def append(self, event: dict[str, Any]) -> None:
        event.setdefault("ts", datetime.now(timezone.utc).isoformat())
        with self.journal_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    def record_submission(
        self, prediction: Prediction, status: str, detail: str = ""
    ) -> None:
        self.append(
            {
                "type": "submission",
                "status": status,     # "submitted" | "skipped" | "failed" | "dry-run"
                "detail": detail,
                **prediction.to_json(),
            }
        )

    def record_error(self, scope: str, message: str, **extra: Any) -> None:
        self.append({"type": "error", "scope": scope, "message": message, **extra})

    def _write_atomic(self, path: Path, payload: Any) -> None:
        """Ecriture atomique : un job interrompu ne laisse pas un JSON tronque."""
        fd, tmp = tempfile.mkstemp(dir=str(self.root), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def save_fixtures(self, cards: list[Card]) -> None:
        self._write_atomic(
            self.fixtures_path,
            {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "matches": [
                    {
                        "match_id": c.match_id,
                        "home": c.home,
                        "away": c.away,
                        "kickoff": c.kickoff.isoformat(),
                        "competition": c.competition,
                        "matchday": c.matchday,
                        "locked": c.locked,
                    }
                    for c in cards
                ],
            },
        )

    def save_last_run(self, payload: dict[str, Any]) -> None:
        payload.setdefault("finished_at", datetime.now(timezone.utc).isoformat())
        self._write_atomic(self.last_run_path, payload)

    # --- lecture -----------------------------------------------------------
    def events(self) -> Iterator[dict[str, Any]]:
        if not self.journal_path.exists():
            return
        with self.journal_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    # Une ligne corrompue (job tue en plein write) ne doit pas
                    # faire tomber tout le pipeline.
                    continue

    def load_fixtures(self) -> list[dict[str, Any]]:
        if not self.fixtures_path.exists():
            return []
        try:
            return json.loads(self.fixtures_path.read_text(encoding="utf-8"))["matches"]
        except (json.JSONDecodeError, KeyError):
            return []

    def last_submitted_score(self, match_id: str) -> tuple[int, int] | None:
        """Dernier score effectivement soumis pour ce match, s'il existe.

        Le bot ecrase toujours cote MPP, mais il evite de re-soumettre a
        l'identique : moins de requetes, moins d'exposition, et un journal
        lisible.
        """
        found: tuple[int, int] | None = None
        for ev in self.events():
            if (
                ev.get("type") == "submission"
                and ev.get("status") == "submitted"
                and ev.get("match_id") == match_id
            ):
                found = (int(ev["home_goals"]), int(ev["away_goals"]))
        return found
