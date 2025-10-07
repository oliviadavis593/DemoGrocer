"""Event logging for simulator jobs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional


@dataclass
class SimulatorEvent:
    """Representation of a single simulator event."""

    ts: datetime
    type: str
    product: str
    lot: Optional[str]
    qty: float
    before: float
    after: float

    def to_payload(self) -> Dict[str, object]:
        return {
            "ts": self.ts.astimezone(timezone.utc).isoformat(),
            "source": "simulator",
            "type": self.type,
            "product": self.product,
            "lot": self.lot,
            "qty": round(self.qty, 4),
            "before": round(self.before, 4),
            "after": round(self.after, 4),
        }


class EventWriter:
    """Append simulator events to a JSON lines file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, events: Iterable[SimulatorEvent]) -> None:
        payloads: List[Mapping[str, object]] = [event.to_payload() for event in events]
        if not payloads:
            return
        with self.path.open("a", encoding="utf-8") as handle:
            for payload in payloads:
                handle.write(_json_line(payload))
                handle.write("\n")


def _json_line(payload: Mapping[str, object]) -> str:
    # Avoid bringing in the json module for a single dump - implement minimal writer
    import json

    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


__all__ = ["SimulatorEvent", "EventWriter"]
