from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from apps.web import create_app
from apps.web.data import calculate_at_risk, load_recent_events, snapshot_from_quants
from services.simulator.inventory import QuantRecord


def test_load_recent_events_orders_and_limits(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    entries = [
        {
            "ts": "2024-01-10T12:00:00+00:00",
            "type": "receiving",
            "product": "Gala Apples",
            "lot": "LOT-1",
            "qty": 5.0,
            "before": 10.0,
            "after": 15.0,
        },
        {
            "ts": "2024-01-11T08:30:00+00:00",
            "type": "sell_down",
            "product": "Whole Milk",
            "lot": "LOT-2",
            "qty": -2.0,
            "before": 8.0,
            "after": 6.0,
        },
        {
            "ts": "2024-01-09T09:15:00+00:00",
            "type": "daily_expiry",
            "product": "Cheddar",
            "lot": "LOT-3",
            "qty": -3.0,
            "before": 3.0,
            "after": 0.0,
        },
    ]
    with events_path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry))
            handle.write("\n")

    records = load_recent_events(events_path, limit=2)
    assert [record.product for record in records] == ["Whole Milk", "Gala Apples"]


def test_calculate_at_risk_filters_by_threshold() -> None:
    snapshot = snapshot_from_quants(
        [
            QuantRecord(
                id=1,
                product_id=101,
                product_name="Gala Apples",
                category="Produce",
                quantity=12.5,
                lot_id=201,
                lot_name="LOT-1",
                life_date=date(2024, 1, 12),
            ),
            QuantRecord(
                id=2,
                product_id=102,
                product_name="Whole Milk",
                category="Dairy",
                quantity=0.0,
                lot_id=202,
                lot_name="LOT-2",
                life_date=date(2024, 1, 11),
            ),
            QuantRecord(
                id=3,
                product_id=103,
                product_name="Cheddar",
                category="Dairy",
                quantity=4.0,
                lot_id=203,
                lot_name="LOT-3",
                life_date=date(2024, 1, 20),
            ),
        ]
    )

    results = calculate_at_risk(snapshot, today=date(2024, 1, 10), threshold_days=3)
    assert len(results) == 1
    assert results[0].product == "Gala Apples"
    assert results[0].days_until == 2


def test_app_endpoints_render_html(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    entries = [
        {
            "ts": "2024-01-11T08:30:00+00:00",
            "type": "sell_down",
            "product": "Whole Milk",
            "lot": "LOT-2",
            "qty": -2.0,
            "before": 8.0,
            "after": 6.0,
        }
    ]
    with events_path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry))
            handle.write("\n")

    snapshot = snapshot_from_quants(
        [
            QuantRecord(
                id=1,
                product_id=101,
                product_name="Gala Apples",
                category="Produce",
                quantity=5.0,
                lot_id=201,
                lot_name="LOT-1",
                life_date=date(2024, 1, 12),
            )
        ]
    )

    class FakeRepository:
        def load_snapshot(self):
            return snapshot

    app = create_app(
        events_path_provider=lambda: events_path,
        repository_factory=lambda: FakeRepository(),
    )
    client = TestClient(app)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    events_resp = client.get("/events/recent")
    assert events_resp.status_code == 200
    assert "Whole Milk" in events_resp.text

    risk_resp = client.get("/at-risk", params={"threshold_days": 3})
    assert risk_resp.status_code == 200
    assert "Gala Apples" in risk_resp.text
    assert "At-Risk Inventory" in risk_resp.text
