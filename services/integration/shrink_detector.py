"""Flag high-risk inventory conditions such as near-expiry, low movement, and overstock."""
from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

from services.integration.odoo_service import OdooService

FlagRecord = Dict[str, Any]
InventoryRow = Mapping[str, Any]


def detect_flags(
    service: OdooService,
    *,
    inventory: Optional[Sequence[InventoryRow]] = None,
    near_expiry_days: Optional[int] = None,
    low_movement_window_days: Optional[int] = None,
    low_movement_min_units: Optional[float] = None,
    overstock_window_days: Optional[int] = None,
    overstock_target_days: Optional[float] = None,
    now: Optional[datetime] = None,
) -> List[FlagRecord]:
    """Evaluate all shrink conditions and return a combined list of flag records."""

    rows = list(inventory) if inventory is not None else list(service.fetch_inventory_snapshot())
    sales_cache: Dict[int, Mapping[str, float]] = {}

    def _sales(window: int) -> Mapping[str, float]:
        window = max(int(window or 0), 1)
        if window not in sales_cache:
            sales_cache[window] = service.fetch_sales(window_days=window)
        return sales_cache[window]

    flags: List[FlagRecord] = []
    if near_expiry_days is not None:
        flags.extend(flag_near_expiry(service, days=near_expiry_days, inventory=rows, now=now))

    if low_movement_window_days is not None and low_movement_min_units is not None:
        sales = _sales(low_movement_window_days)
        flags.extend(
            flag_low_movement(
                service,
                window_days=low_movement_window_days,
                min_units=low_movement_min_units,
                inventory=rows,
                sales=sales,
            )
        )

    if overstock_window_days is not None and overstock_target_days is not None:
        sales = _sales(overstock_window_days)
        flags.extend(
            flag_overstock(
                service,
                window_days=overstock_window_days,
                target_days=overstock_target_days,
                inventory=rows,
                sales=sales,
            )
        )

    flags.sort(key=lambda record: (record.get("reason") or "", record.get("product") or "", record.get("lot") or ""))
    return flags


def flag_near_expiry(
    service: OdooService,
    *,
    days: int,
    inventory: Optional[Sequence[InventoryRow]] = None,
    now: Optional[datetime] = None,
) -> List[FlagRecord]:
    """Flag lots that are within the provided day threshold of their expiry."""

    rows = list(inventory) if inventory is not None else list(service.fetch_inventory_snapshot())
    threshold = max(int(days or 0), 0)
    today = _coerce_datetime(now).date()

    flags: List[FlagRecord] = []
    for row in rows:
        life_date_raw = row.get("life_date")
        if not life_date_raw:
            continue
        life_date = _parse_date(life_date_raw)
        if life_date is None:
            continue
        delta_days = (life_date - today).days
        if delta_days <= threshold:
            flags.append(
                _build_flag_record(
                    row,
                    reason="near_expiry",
                    metrics={
                        "threshold_days": threshold,
                        "days_until_expiry": delta_days,
                        "life_date": life_date.isoformat(),
                    },
                )
            )
    return flags


def flag_low_movement(
    service: OdooService,
    *,
    window_days: int,
    min_units: float,
    inventory: Optional[Sequence[InventoryRow]] = None,
    sales: Optional[Mapping[str, float]] = None,
) -> List[FlagRecord]:
    """Flag products whose recent sales fall below the provided minimum units."""

    rows = list(inventory) if inventory is not None else list(service.fetch_inventory_snapshot())
    window_days = max(int(window_days or 0), 1)
    min_units = max(float(min_units), 0.0)
    sales = sales or service.fetch_sales(window_days=window_days)

    inventory_summary = _summarize_inventory(rows)
    flags: List[FlagRecord] = []

    for product, summary in inventory_summary.items():
        if summary.total_qty <= 0:
            continue
        velocity = float(sales.get(product, 0.0))
        units_sold = velocity * window_days
        if units_sold <= min_units:
            metrics = {
                "window_days": window_days,
                "units_sold": round(units_sold, 4),
                "threshold_units": min_units,
                "average_daily_sales": round(velocity, 4),
                "quantity_on_hand": round(summary.total_qty, 4),
            }
            flags.append(
                _build_flag_record(
                    summary.primary_row(),
                    reason="low_movement",
                    metrics=metrics,
                    lots=summary.lots,
                    quantity=summary.total_qty,
                )
            )
    return flags


def flag_overstock(
    service: OdooService,
    *,
    window_days: int,
    target_days: float,
    inventory: Optional[Sequence[InventoryRow]] = None,
    sales: Optional[Mapping[str, float]] = None,
) -> List[FlagRecord]:
    """Flag products whose inventory exceeds the target days of supply."""

    rows = list(inventory) if inventory is not None else list(service.fetch_inventory_snapshot())
    window_days = max(int(window_days or 0), 1)
    target_days = max(float(target_days), 0.0)
    sales = sales or service.fetch_sales(window_days=window_days)

    inventory_summary = _summarize_inventory(rows)
    flags: List[FlagRecord] = []

    for product, summary in inventory_summary.items():
        total_qty = summary.total_qty
        if total_qty <= 0:
            continue
        velocity = float(sales.get(product, 0.0))
        avg_daily_sales = max(velocity, 0.0)
        effective_velocity = avg_daily_sales if avg_daily_sales > 0 else 1e-6
        days_of_supply = total_qty / effective_velocity
        if avg_daily_sales <= 0 or days_of_supply > target_days:
            metrics = {
                "window_days": window_days,
                "target_days": target_days,
                "average_daily_sales": round(avg_daily_sales, 4),
                "days_of_supply": round(days_of_supply, 4),
                "quantity_on_hand": round(total_qty, 4),
            }
            flags.append(
                _build_flag_record(
                    summary.primary_row(),
                    reason="overstock",
                    metrics=metrics,
                    lots=summary.lots,
                    quantity=total_qty,
                )
            )
    return flags


def _build_flag_record(
    row: Optional[InventoryRow],
    *,
    reason: str,
    metrics: MutableMapping[str, Any],
    lots: Optional[Iterable[str]] = None,
    quantity: Optional[float] = None,
) -> FlagRecord:
    product = str(row.get("product")) if row else ""
    lot = row.get("lot") if row else None
    record: FlagRecord = {
        "product": product,
        "lot": lot,
        "reason": reason,
        "metrics": dict(metrics),
    }
    if row:
        life_date = row.get("life_date")
        if life_date not in (None, "", "null", "None"):
            record["life_date"] = str(life_date)
        category = row.get("category")
        if category:
            record["category"] = str(category)
    if quantity is not None:
        record["quantity"] = round(float(quantity), 4)
    else:
        qty = row.get("quantity") if row else None
        if isinstance(qty, (int, float)):
            record["quantity"] = round(float(qty), 4)
    locations = row.get("locations") if row else None
    if locations:
        record["locations"] = list(locations)
    default_code = row.get("default_code") if row else None
    if default_code:
        record["default_code"] = default_code
    if lots is not None:
        record["lots"] = sorted({lot for lot in lots if lot})
    elif lot:
        record["lots"] = [lot]
    return record


def _coerce_datetime(value: Optional[datetime]) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str):
        return None
    # Attempt ISO 8601 parsing; fall back to date-only format.
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.date()
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d")
            return parsed.date()
        except ValueError:
            return None


class _InventorySummary:
    """Aggregate inventory context for a single product."""

    def __init__(self, first_row: InventoryRow) -> None:
        self.rows: List[InventoryRow] = [first_row]
        self.total_qty: float = _coerce_quantity(first_row.get("quantity"))
        self.lots: List[str] = [str(first_row.get("lot"))] if first_row.get("lot") else []

    def add(self, row: InventoryRow) -> None:
        self.rows.append(row)
        qty = _coerce_quantity(row.get("quantity"))
        self.total_qty += qty
        lot = row.get("lot")
        if lot:
            self.lots.append(str(lot))

    def primary_row(self) -> InventoryRow:
        return self.rows[0]


def _summarize_inventory(rows: Sequence[InventoryRow]) -> Dict[str, _InventorySummary]:
    summary: Dict[str, _InventorySummary] = {}
    for row in rows:
        product = row.get("product")
        if not product:
            continue
        product_name = str(product)
        record = summary.get(product_name)
        if record is None:
            record = _InventorySummary(row)
            summary[product_name] = record
        else:
            record.add(row)
    return summary


def _coerce_quantity(value: Any) -> float:
    try:
        quantity = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(quantity) or math.isinf(quantity):
        return 0.0
    return max(quantity, 0.0)


__all__ = [
    "detect_flags",
    "flag_near_expiry",
    "flag_low_movement",
    "flag_overstock",
]
