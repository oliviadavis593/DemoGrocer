"""Persistence for simulator execution state."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional


class StateTracker:
    """Persist last execution timestamps for simulator jobs."""

    def __init__(self, path: Path, minimum_interval: timedelta) -> None:
        self.path = path
        self.minimum_interval = minimum_interval
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state: Dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._state = {str(k): str(v) for k, v in data.items()}
        except (json.JSONDecodeError, OSError):
            self._state = {}

    def should_run(self, job_name: str, now: datetime, force: bool = False) -> bool:
        if force:
            return True
        if job_name not in self._state:
            return True
        try:
            last_run = datetime.fromisoformat(self._state[job_name])
        except ValueError:
            return True
        if last_run.tzinfo is None:
            last_run = last_run.replace(tzinfo=timezone.utc)
        return now - last_run >= self.minimum_interval

    def record(self, job_name: str, now: datetime) -> None:
        self._state[job_name] = now.astimezone(timezone.utc).isoformat()
        self._save()

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._state, sort_keys=True), encoding="utf-8")
        except OSError:
            pass


__all__ = ["StateTracker"]
