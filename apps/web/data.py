"""Data loading utilities for the reporting web app."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, List, Mapping

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
    lot: str | None
    life_date: date
    days_until: int
    quantity: float


def load_recent_events(path: Path | None = None, limit: int = 20) -> List[EventRecord]:
    """Read simulator events from a JSON lines file ordered newest-first."""

    events_path = path or DEFAULT_EVENTS_PATH
    if limit <= 0:
        return []
    payloads: List[EventRecord] = []
    if not events_path.exists():
        return []
    with events_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, Mapping):
                continue
            record = EventRecord.from_mapping(payload)
            if record is None:
                continue
            payloads.append(record)
    payloads.sort(key=lambda record: record.ts, reverse=True)
    return payloads[:limit]


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


__all__ = [
    "AtRiskItem",
    "EventRecord",
    "calculate_at_risk",
    "load_recent_events",
    "snapshot_from_quants",
]
