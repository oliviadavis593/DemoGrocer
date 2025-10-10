from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Mapping

from services.integration.shrink_detector import (
    detect_flags,
    flag_low_movement,
    flag_near_expiry,
    flag_overstock,
)


class DummyService:
    def __init__(
        self,
        inventory: Iterable[Mapping[str, object]],
        sales_by_window: Dict[int, Mapping[str, float]],
    ) -> None:
        self._inventory = list(inventory)
        self._sales_by_window = {int(window): dict(values) for window, values in sales_by_window.items()}
        self.inventory_calls: int = 0
        self.sales_calls: List[int] = []

    def fetch_inventory_snapshot(self):
        self.inventory_calls += 1
        return list(self._inventory)

    def fetch_sales(self, window_days: int):
        window = int(window_days)
        self.sales_calls.append(window)
        return dict(self._sales_by_window.get(window, {}))


def build_inventory(now: datetime):
    return [
        {
            "product": "Milk",
            "lot": "MILK-001",
            "quantity": 10.0,
            "life_date": (now + timedelta(days=3)).date().isoformat(),
            "locations": ["Cooler"],
            "default_code": "D-MILK",
        },
        {
            "product": "Eggs",
            "lot": "EG-001",
            "quantity": 30.0,
            "life_date": (now + timedelta(days=15)).date().isoformat(),
            "locations": ["Cooler"],
            "default_code": "D-EGGS",
        },
        {
            "product": "Eggs",
            "lot": "EG-002",
            "quantity": 20.0,
            "life_date": None,
            "locations": ["Overflow"],
            "default_code": "D-EGGS",
        },
    ]


def test_flag_near_expiry_returns_lots_within_threshold() -> None:
    now = datetime(2024, 5, 1, tzinfo=timezone.utc)
    service = DummyService(build_inventory(now), sales_by_window={})

    flags = flag_near_expiry(service, days=5, now=now)

    assert len(flags) == 1
    record = flags[0]
    assert record["product"] == "Milk"
    assert record["reason"] == "near_expiry"
    assert record["metrics"]["days_until_expiry"] == 3
    assert record["metrics"]["threshold_days"] == 5
    assert record["quantity"] == 10.0


def test_flag_low_movement_uses_sales_window_and_threshold() -> None:
    now = datetime(2024, 5, 1, tzinfo=timezone.utc)
    inventory = build_inventory(now)
    service = DummyService(
        inventory,
        sales_by_window={7: {"Milk": 0.5, "Eggs": 5.0}},
    )

    flags = flag_low_movement(service, window_days=7, min_units=10.0)

    assert len(flags) == 1
    record = flags[0]
    assert record["product"] == "Milk"
    assert record["reason"] == "low_movement"
    assert record["metrics"]["units_sold"] == 3.5
    assert record["metrics"]["threshold_units"] == 10.0
    assert record["lots"] == ["MILK-001"]


def test_flag_overstock_defaults_low_velocity_to_flag() -> None:
    now = datetime(2024, 5, 1, tzinfo=timezone.utc)
    inventory = build_inventory(now)
    service = DummyService(
        inventory,
        sales_by_window={7: {"Milk": 2.0, "Eggs": 0.0}},
    )

    flags = flag_overstock(service, window_days=7, target_days=14.0)

    assert len(flags) == 1
    record = flags[0]
    assert record["product"] == "Eggs"
    assert record["reason"] == "overstock"
    assert record["metrics"]["target_days"] == 14.0
    assert record["metrics"]["average_daily_sales"] == 0.0
    assert record["metrics"]["quantity_on_hand"] == 50.0


def test_detect_flags_combines_results_and_reuses_inventory_snapshot() -> None:
    now = datetime(2024, 5, 1, tzinfo=timezone.utc)
    inventory = build_inventory(now)
    service = DummyService(
        inventory,
        sales_by_window={7: {"Milk": 0.5, "Eggs": 0.0}},
    )

    flags = detect_flags(
        service,
        near_expiry_days=5,
        low_movement_window_days=7,
        low_movement_min_units=10.0,
        overstock_window_days=7,
        overstock_target_days=14.0,
        now=now,
    )

    reasons = [record["reason"] for record in flags]
    assert reasons.count("near_expiry") == 1
    assert reasons.count("low_movement") == 2
    assert reasons.count("overstock") == 2
    milk_near_expiry = next(record for record in flags if record["product"] == "Milk" and record["reason"] == "near_expiry")
    assert milk_near_expiry["metrics"]["days_until_expiry"] == 3
    eggs_overstock = next(record for record in flags if record["product"] == "Eggs" and record["reason"] == "overstock")
    assert eggs_overstock["metrics"]["quantity_on_hand"] == 50.0
    assert service.inventory_calls == 1
    assert service.sales_calls == [7]
