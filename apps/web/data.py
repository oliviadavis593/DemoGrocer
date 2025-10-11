"""Data loading utilities for the reporting web app."""
from __future__ import annotations

from functools import lru_cache
import heapq
import itertools
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, List, Mapping, MutableMapping, Protocol, Sequence, Tuple

from services.simulator.inventory import InventorySnapshot, QuantRecord

DEFAULT_EVENTS_PATH = Path(os.getenv("FOODFLOW_EVENTS_PATH", "out/events.jsonl"))
DEFAULT_FLAGGED_PATH = Path(os.getenv("FOODFLOW_FLAGGED_PATH", "out/flagged.json"))
FALLBACK_CASE_UNITS = 12.0
MINIMUM_DISCOUNT_FACTOR = 0.0
MAXIMUM_DISCOUNT_FACTOR = 1.0


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


def load_flagged_decisions(path: Path | None = None) -> List[dict[str, object]]:
    """Load flagged decision payloads from disk."""

    flagged_path = path or DEFAULT_FLAGGED_PATH
    if not flagged_path.exists():
        return []
    try:
        text = flagged_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OSError(f"Failed to read flagged decisions from {flagged_path}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Flagged decisions file {flagged_path} contains invalid JSON") from exc
    if not isinstance(data, list):
        raise ValueError("Flagged decisions file must contain a list of records")
    records: List[dict[str, object]] = []
    for entry in data:
        if isinstance(entry, Mapping):
            records.append(dict(entry))
    return records


def calculate_impact_metrics(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Estimate waste diversion (value) and donations (weight) from decisions."""

    prices, uoms = _product_lookup()
    diverted_value = 0.0
    donated_weight = 0.0
    markdown_count = 0
    donation_count = 0

    for record in records:
        outcome = str(record.get("outcome") or "").upper()
        code = str(record.get("default_code") or "").strip()
        quantity, uom = _resolve_quantity_and_uom(record, code, uoms)
        if quantity is None or quantity <= 0:
            continue
        if outcome == "MARKDOWN":
            price = _resolve_unit_price(record, code, prices)
            if price is None or price <= 0:
                continue
            factor = _resolve_discount_factor(record)
            diverted_value += quantity * price * factor
            markdown_count += 1
        elif outcome == "DONATE":
            pounds = _convert_to_pounds(quantity, uom)
            if pounds <= 0:
                continue
            donated_weight += pounds
            donation_count += 1

    return {
        "diverted_value_usd": round(diverted_value, 2),
        "donated_weight_lbs": round(donated_weight, 2),
        "markdown_count": markdown_count,
        "donation_count": donation_count,
    }


def _resolve_discount_factor(record: Mapping[str, object]) -> float:
    discount = _coerce_positive_float(record.get("price_markdown_pct"), clamp=True)
    if discount is None:
        return 1.0
    return max(
        MINIMUM_DISCOUNT_FACTOR,
        min(MAXIMUM_DISCOUNT_FACTOR, 1.0 - discount),
    )


def _resolve_unit_price(
    record: Mapping[str, object],
    code: str,
    prices: Mapping[str, float],
) -> float | None:
    direct = _coerce_positive_float(record.get("list_price"), fallback=_coerce_positive_float(record.get("unit_price")))
    if direct is not None:
        return direct
    if code and code in prices:
        return prices[code]
    return None


def _resolve_uom(record: Mapping[str, object], code: str, uoms: Mapping[str, str]) -> str | None:
    if code and code in uoms:
        return uoms[code]
    raw = record.get("uom") or record.get("unit_of_measure") or record.get("unit")
    if isinstance(raw, str) and raw.strip():
        return raw.strip().upper()
    return None


def _resolve_quantity_and_uom(
    record: Mapping[str, object], code: str, uoms: Mapping[str, str]
) -> tuple[float | None, str | None]:
    quantity = _coerce_positive_float(
        record.get("suggested_qty"),
        fallback=_coerce_positive_float(record.get("quantity")),
    )
    if quantity is None or quantity <= 0:
        return None, None
    uom = _resolve_uom(record, code, uoms)
    return quantity, uom


def _convert_to_pounds(quantity: float, uom: str | None) -> float:
    if uom == "LB" or uom == "LBS":
        return quantity
    if uom == "OZ":
        return quantity / 16.0
    if uom == "CASE":
        return quantity * FALLBACK_CASE_UNITS
    # Treat EA and unknown units as approximate pounds to provide an estimate.
    return quantity


def _coerce_positive_float(value: object, *, fallback: float | None = None, clamp: bool = False) -> float | None:
    candidate: float | None
    try:
        if value is None:
            candidate = None
        else:
            candidate = float(value)
    except (TypeError, ValueError):
        candidate = None
    if candidate is None or candidate <= 0:
        candidate = fallback
    if candidate is None:
        return None
    if clamp:
        return max(0.0, min(1.0, candidate))
    return candidate


@lru_cache(maxsize=1)
def _product_lookup() -> tuple[dict[str, float], dict[str, str]]:
    prices: dict[str, float] = {}
    uoms: dict[str, str] = {}
    try:
        from scripts.seed_inventory import _product_catalog  # type: ignore
    except Exception:
        return prices, uoms
    try:
        catalog = _product_catalog()
    except Exception:
        return prices, uoms
    for entry in catalog:
        code = str(entry.get("default_code") or "").strip()
        if not code:
            continue
        price = _coerce_positive_float(entry.get("list_price"))
        if price is not None:
            prices[code] = price
        uom_raw = entry.get("uom")
        if isinstance(uom_raw, str) and uom_raw.strip():
            uoms[code] = uom_raw.strip().upper()
    return prices, uoms


def append_weight_metadata(records: Iterable[MutableMapping[str, object]]) -> None:
    """Populate per-record weight metadata for flagged decision payloads."""

    _, uoms = _product_lookup()
    for record in records:
        code = str(record.get("default_code") or "").strip()
        quantity, uom = _resolve_quantity_and_uom(record, code, uoms)
        if uom:
            record["unit"] = uom
            record["unit_of_measure"] = uom
        if quantity is not None:
            record["quantity"] = quantity
        pounds = _convert_to_pounds(quantity or 0.0, uom)
        record["estimated_weight_lbs"] = round(pounds, 2)


__all__ = [
    "AtRiskItem",
    "EventRecord",
    "calculate_at_risk",
    "calculate_impact_metrics",
    "load_flagged_decisions",
    "load_recent_events",
    "serialize_at_risk",
    "serialize_events",
    "serialize_inventory_events",
    "append_weight_metadata",
    "snapshot_from_quants",
]
