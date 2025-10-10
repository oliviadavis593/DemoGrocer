"""Synthetic movement generator for demo inventory fixtures."""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, List, Sequence

from .fixtures import InventoryFixture


@dataclass(frozen=True)
class MovementEvent:
    """Synthetic inventory movement generated for offline demos."""

    ts: datetime
    type: str
    default_code: str
    product: str
    category: str
    supplier: str
    qty: float
    balance: float

    def to_dict(self) -> dict[str, object]:
        return {
            "ts": self.ts.astimezone(timezone.utc).isoformat(),
            "type": self.type,
            "default_code": self.default_code,
            "product": self.product,
            "category": self.category,
            "supplier": self.supplier,
            "qty": round(self.qty, 4),
            "balance": round(self.balance, 4),
        }


def generate_fake_movements(
    fixtures: Sequence[InventoryFixture],
    *,
    days: int = 14,
    start_date: date | None = None,
    seed: int = 4862,
) -> List[MovementEvent]:
    """Generate deterministic movement events for perishables and low-demand items."""

    if not fixtures:
        return []

    horizon_days = max(days, 1)
    earliest_life_date = min(fixture.life_date for fixture in fixtures)
    base_start = earliest_life_date - timedelta(days=horizon_days)
    anchor_date = start_date or base_start
    start_dt = datetime.combine(anchor_date, time(hour=8), tzinfo=timezone.utc)

    events: List[MovementEvent] = []
    for index, fixture in enumerate(fixtures):
        if not fixture.perishable and fixture.demand_profile != "low":
            continue
        rng = random.Random(seed + index)
        on_hand = fixture.stock_on_hand
        base_fraction = _daily_sales_fraction(fixture)

        for day in range(horizon_days):
            if on_hand <= 0.01:
                break
            day_start = start_dt + timedelta(days=day)
            sale_fraction = base_fraction * rng.uniform(0.65, 1.25)
            sale_qty = round(min(on_hand, on_hand * sale_fraction), 2)
            if sale_qty > 0:
                on_hand = max(on_hand - sale_qty, 0.0)
                events.append(
                    MovementEvent(
                        ts=day_start + timedelta(hours=9 + rng.random() * 6),
                        type="sale",
                        default_code=fixture.default_code,
                        product=fixture.product,
                        category=fixture.category,
                        supplier=fixture.supplier,
                        qty=-sale_qty,
                        balance=max(on_hand, 0.0),
                    )
                )

            # Spoilage/expiry adjustment for perishables approaching end of life
            days_until_expiry = (fixture.life_date - day_start.date()).days
            if fixture.perishable and days_until_expiry <= 1 and on_hand > 0.2:
                spoilage_fraction = rng.uniform(0.15, 0.35)
                spoilage_qty = round(min(on_hand, on_hand * spoilage_fraction), 2)
                if spoilage_qty > 0:
                    on_hand = max(on_hand - spoilage_qty, 0.0)
                    events.append(
                        MovementEvent(
                            ts=day_start + timedelta(hours=17 + rng.random() * 2),
                            type="expiry_adjustment",
                            default_code=fixture.default_code,
                            product=fixture.product,
                            category=fixture.category,
                            supplier=fixture.supplier,
                            qty=-spoilage_qty,
                            balance=max(on_hand, 0.0),
                        )
                    )

            # Occasional markdowns for low demand items still overstocked
            if fixture.demand_profile == "low" and day % 4 == 0 and on_hand > fixture.stock_on_hand * 0.8:
                markdown_qty = round(min(on_hand * 0.05, on_hand), 2)
                if markdown_qty > 0:
                    on_hand = max(on_hand - markdown_qty, 0.0)
                    events.append(
                        MovementEvent(
                            ts=day_start + timedelta(hours=12 + rng.random() * 3),
                            type="markdown_clearance",
                            default_code=fixture.default_code,
                            product=fixture.product,
                            category=fixture.category,
                            supplier=fixture.supplier,
                            qty=-markdown_qty,
                            balance=max(on_hand, 0.0),
                        )
                    )

        # Replenish when stock has been depleted significantly
        replenish_threshold = fixture.stock_on_hand * 0.3
        if on_hand < replenish_threshold:
            replenishment_qty = round(max(fixture.stock_on_hand - on_hand, 0.0) * rng.uniform(0.6, 0.9), 2)
            if replenishment_qty > 0:
                on_hand = max(on_hand + replenishment_qty, 0.0)
                events.append(
                    MovementEvent(
                        ts=start_dt + timedelta(days=horizon_days, hours=6 + rng.random() * 4),
                        type="receiving",
                        default_code=fixture.default_code,
                        product=fixture.product,
                        category=fixture.category,
                        supplier=fixture.supplier,
                        qty=replenishment_qty,
                        balance=max(on_hand, 0.0),
                    )
                )

    events.sort(key=lambda event: event.ts)
    return events


def movements_as_dicts(events: Iterable[MovementEvent]) -> List[dict[str, object]]:
    """Serialize movement events to dictionaries."""

    return [event.to_dict() for event in events]


def _daily_sales_fraction(fixture: InventoryFixture) -> float:
    base_fraction = 0.18 if fixture.perishable else 0.08
    if fixture.demand_profile == "high":
        base_fraction *= 1.3
    elif fixture.demand_profile == "low":
        base_fraction *= 0.45
    return max(0.02, min(base_fraction, 0.35))


__all__ = ["MovementEvent", "generate_fake_movements", "movements_as_dicts"]
