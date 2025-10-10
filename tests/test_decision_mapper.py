from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from packages.decision.policy import DEFAULT_POLICY_PATH, DecisionMapper


def _build_mapper(path: Path | None = None) -> DecisionMapper:
    return DecisionMapper.from_path(path or DEFAULT_POLICY_PATH)


def _build_flag(
    *,
    reason: str,
    quantity: float,
    default_code: str = "SKU-001",
    lot: str | None = None,
    life_date: str | None = None,
    metrics: dict[str, object] | None = None,
    category: str | None = None,
) -> dict[str, object]:
    metrics_data = dict(metrics) if metrics else {}
    payload: dict[str, object] = {
        "reason": reason,
        "quantity": quantity,
        "default_code": default_code,
        "metrics": metrics_data,
    }
    if lot is not None:
        payload["lot"] = lot
    if life_date is not None:
        payload["life_date"] = life_date
        payload["metrics"].setdefault("life_date", life_date)
    if category is not None:
        payload["category"] = category
    return payload


def test_near_expiry_uses_markdown_policy() -> None:
    mapper = _build_mapper()
    life_date = datetime(2024, 6, 1, tzinfo=timezone.utc).date().isoformat()
    flag = _build_flag(
        reason="near_expiry",
        quantity=8.0,
        default_code="D-MILK",
        lot="MILK-LOT-01",
        life_date=life_date,
        metrics={"days_until_expiry": 2, "life_date": life_date},
    )

    decision = mapper.map_flag(flag)

    assert decision.outcome == "MARKDOWN"
    assert decision.price_markdown_pct == pytest.approx(0.35)
    assert decision.suggested_qty == pytest.approx(8.0)
    assert decision.default_code == "D-MILK"
    assert decision.lot == "MILK-LOT-01"


def test_low_movement_non_perishable_is_donation() -> None:
    mapper = _build_mapper()
    flag = _build_flag(
        reason="low_movement",
        quantity=15.0,
        default_code="SHELF-123",
        metrics={"window_days": 14, "units_sold": 3.0},
    )

    decision = mapper.map_flag(flag)

    assert decision.outcome == "DONATE"
    assert decision.price_markdown_pct is None
    assert decision.suggested_qty == pytest.approx(15.0)


def test_low_movement_perishable_marked_down() -> None:
    mapper = _build_mapper()
    flag = _build_flag(
        reason="low_movement",
        quantity=5.0,
        default_code="FRUIT-12",
        life_date="2024-07-01",
        metrics={"window_days": 14, "units_sold": 1.0},
        category="Produce",
    )

    decision = mapper.map_flag(flag)

    assert decision.outcome == "MARKDOWN"
    assert decision.price_markdown_pct == pytest.approx(0.15)
    assert decision.notes == "Discount slow movers with shelf life"


def test_recall_flags_quarantine_immediately() -> None:
    mapper = _build_mapper()
    flag = _build_flag(
        reason="recall",
        quantity=2.0,
        default_code="MEAT-42",
        lot="LOT-RECALL",
    )

    decision = mapper.map_flag(flag)

    assert decision.outcome == "RECALL_QUARANTINE"
    assert decision.notes == "Quarantine recalled inventory immediately"
    assert decision.price_markdown_pct is None


def test_map_flags_coerces_non_numeric_quantity_to_none() -> None:
    mapper = _build_mapper()
    flag = _build_flag(reason="unknown", quantity=0.0)
    flag["quantity"] = "N/A"

    decision = mapper.map_flag(flag)

    assert decision.outcome == "DIVERT"
    assert decision.suggested_qty is None
