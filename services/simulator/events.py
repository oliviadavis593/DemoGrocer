"""Event logging for simulator jobs."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from packages.db import EventStore, InventoryEvent

LOGGER = logging.getLogger(__name__)


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

    def to_inventory_event(self) -> InventoryEvent:
        return InventoryEvent(
            ts=self.ts,
            type=self.type,
            product=self.product,
            lot=self.lot,
            qty=self.qty,
            before=self.before,
            after=self.after,
            source="simulator",
        )


class EventWriter:
    """Append simulator events to a JSON lines file and optional database store."""

    def __init__(self, path: Path, store: EventStore | None = None) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.store = store or EventStore()

    def write(self, events: Iterable[SimulatorEvent]) -> None:
        buffered_events: List[SimulatorEvent] = list(events)
        if not buffered_events:
            return

        payloads: Sequence[Mapping[str, object]] = [event.to_payload() for event in buffered_events]
        with self.path.open("a", encoding="utf-8") as handle:
            for payload in payloads:
                handle.write(_json_line(payload))
                handle.write("\n")

        if self.store:
            inventory_events = [event.to_inventory_event() for event in buffered_events]
            try:
                self.store.add_events(inventory_events)
            except Exception:
                LOGGER.exception("Failed to persist events to database")


class EventHistory:
    """Utility to derive aggregates from the simulator event log."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def outstanding_returns(self) -> Dict[str, float]:
        """Return the remaining quantity that may be returned per product name."""

        totals: Dict[str, Dict[str, float]] = {}
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    record = _parse_json_line(line)
                    if not record or record.get("source") != "simulator":
                        continue
                    product = str(record.get("product"))
                    if not product:
                        continue
                    event_type = record.get("type")
                    qty = float(record.get("qty", 0.0) or 0.0)
                    entry = totals.setdefault(product, {"sold": 0.0, "returned": 0.0})
                    if event_type == "sell_down":
                        entry["sold"] += max(-qty, 0.0)
                    elif event_type == "returns":
                        entry["returned"] += max(qty, 0.0)
        except OSError:
            LOGGER.exception("Failed to read simulator event history from %s", self.path)
            return {}
        outstanding: Dict[str, float] = {}
        for product, values in totals.items():
            remaining = values["sold"] - values["returned"]
            if remaining > 0:
                outstanding[product] = remaining
        return outstanding


def _json_line(payload: Mapping[str, object]) -> str:
    # Avoid bringing in the json module for a single dump - implement minimal writer
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _parse_json_line(line: str) -> Optional[Mapping[str, object]]:
    line = line.strip()
    if not line:
        return None
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, Mapping):
        return parsed
    return None


__all__ = ["SimulatorEvent", "EventWriter", "EventHistory"]
