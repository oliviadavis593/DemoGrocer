"""Data loading utilities for the reporting web app."""
from __future__ import annotations

import heapq
import itertools
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, List, Mapping, Protocol, Sequence, Tuple

from services.simulator.inventory import InventorySnapshot, QuantRecord

DEFAULT_EVENTS_PATH = Path(os.getenv("FOODFLOW_EVENTS_PATH", "out/events.jsonl"))


@dataclass
class EventRecord:
    """Representation of a simulator event for display."""

    ts: datetime
    type: str
    product: str
    lot: str | None
    qty: float
    before: float
    after: float

    def to_dict(self) -> dict[str, object]:
        return {
            "ts": self.ts.isoformat(),
            "type": self.type,
            "product": self.product,
            "lot": self.lot,
            "qty": self.qty,
            "before": self.before,
            "after": self.after,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "EventRecord | None":
        ts_raw = payload.get("ts")
        if not isinstance(ts_raw, str):
            return None
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return cls(
            ts=ts,
            type=str(payload.get("type", "")),
            product=str(payload.get("product", "")),
            lot=(str(payload["lot"]) if payload.get("lot") not in (None, "") else None),
            qty=float(payload.get("qty", 0.0) or 0.0),
            before=float(payload.get("before", 0.0) or 0.0),
            after=float(payload.get("after", 0.0) or 0.0),
        )


@dataclass
class AtRiskItem:
    """Inventory item approaching or past expiry."""

    product: str
    default_code: str | None
    lot: str | None
    life_date: date
    days_until: int
    quantity: float

    def to_dict(self) -> dict[str, object]:
        return {
            "default_code": self.default_code,
            "product": self.product,
            "lot": self.lot,
            "life_date": self.life_date.isoformat(),
            "days_left": self.days_until,
            "quantity": self.quantity,
        }


def load_recent_events(path: Path | None = None, limit: int = 20) -> List[EventRecord]:
    """Read simulator events from a JSON lines file ordered newest-first."""

    events_path = path or DEFAULT_EVENTS_PATH
    if limit <= 0:
        return []
    if not events_path.exists():
        return []

    heap: List[Tuple[datetime, int, EventRecord]] = []
    counter = itertools.count()

    try:
        with events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Failed to parse JSON in {events_path}") from exc
                if not isinstance(payload, Mapping):
                    continue
                record = EventRecord.from_mapping(payload)
                if record is None:
                    continue
                entry = (record.ts, next(counter), record)
                if len(heap) < limit:
                    heapq.heappush(heap, entry)
                else:
                    heapq.heappushpop(heap, entry)
    except OSError as exc:
        raise OSError(f"Failed to read events file {events_path}") from exc

    ordered = sorted(heap, key=lambda item: item[0], reverse=True)
    return [item[2] for item in ordered]


def calculate_at_risk(
    snapshot: InventorySnapshot,
    *,
    today: date | None = None,
    threshold_days: int = 3,
) -> List[AtRiskItem]:
    """Compute items that are within ``threshold_days`` of their expiry."""

    current_day = today or date.today()
    items: List[AtRiskItem] = []
    for quant in snapshot.quants():
        if quant.life_date is None:
            continue
        days_until = (quant.life_date - current_day).days
        if days_until > threshold_days:
            continue
        if quant.quantity <= 0:
            continue
        items.append(
            AtRiskItem(
                product=quant.product_name,
                default_code=quant.default_code,
                lot=quant.lot_name,
                life_date=quant.life_date,
                days_until=days_until,
                quantity=quant.quantity,
            )
        )
    items.sort(key=lambda item: (item.days_until, item.life_date, item.product))
    return items


def snapshot_from_quants(quants: Iterable[QuantRecord]) -> InventorySnapshot:
    """Helper for building snapshots in tests and utilities."""

    return InventorySnapshot(quants)


def serialize_events(records: Sequence[EventRecord]) -> List[dict[str, object]]:
    return [record.to_dict() for record in records]


def serialize_at_risk(items: Sequence[AtRiskItem]) -> List[dict[str, object]]:
    return [item.to_dict() for item in items]


class InventoryEventProtocol(Protocol):
    """Structural protocol for events returned by the database layer."""

    ts: datetime
    type: str
    product: str
    lot: str | None
    qty: float
    before: float
    after: float
    source: str


def serialize_inventory_events(events: Sequence[InventoryEventProtocol]) -> List[dict[str, object]]:
    """Serialize inventory events loaded from the database."""

    payload: List[dict[str, object]] = []
    for event in events:
        ts = event.ts.astimezone(timezone.utc).isoformat()
        payload.append(
            {
                "ts": ts,
                "type": event.type,
                "product": event.product,
                "lot": event.lot,
                "qty": round(event.qty, 4),
                "before": round(event.before, 4),
                "after": round(event.after, 4),
                "source": getattr(event, "source", "simulator"),
            }
        )
    return payload


__all__ = [
    "AtRiskItem",
    "EventRecord",
    "calculate_at_risk",
    "load_recent_events",
    "serialize_at_risk",
    "serialize_events",
    "serialize_inventory_events",
    "snapshot_from_quants",
]
