from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from apps.web import create_app
from apps.web.data import calculate_at_risk, load_recent_events, snapshot_from_quants
from packages.db import EventStore, InventoryEvent
from scripts.db_migrate import run as run_migration
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
                default_code="FF-101",
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
                default_code="FF-102",
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
                default_code="FF-103",
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
    assert results[0].default_code == "FF-101"
    assert results[0].days_until == 2


def test_app_endpoints_return_json(tmp_path: Path) -> None:
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
                default_code="FF-101",
                category="Produce",
                quantity=5.0,
                lot_id=201,
                lot_name="LOT-1",
                life_date=date(2024, 1, 12),
            )
        ]
    )

    class FakeRepository:
        def __init__(self) -> None:
            self.expiry_field = None

        def set_lot_expiry_field(self, field):
            self.expiry_field = field

        def load_snapshot(self):
            return snapshot

    class FakeOdooClient:
        def search_read(self, model, domain, fields=None, limit=None, order=None):
            if model == "ir.model" and domain == [["model", "=", "stock.lot"]]:
                return [{"id": 1}]
            if model == "ir.model.fields" and ["name", "=", "life_date"] in domain:
                return [{"id": 1}]
            raise AssertionError(f"Unexpected call: {model}, {domain}")

    app = create_app(
        events_path_provider=lambda: events_path,
        repository_factory=lambda: FakeRepository(),
        odoo_client_provider=lambda: FakeOdooClient(),
    )
    client = TestClient(app)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    events_resp = client.get("/events/recent")
    assert events_resp.status_code == 200
    events_payload = events_resp.json()
    assert events_payload["meta"]["exists"] is True
    assert events_payload["events"][0]["product"] == "Whole Milk"

    risk_resp = client.get("/at-risk", params={"days": 3})
    assert risk_resp.status_code == 200
    risk_payload = risk_resp.json()
    assert risk_payload["meta"]["days"] == 3
    assert risk_payload["meta"]["lot_expiry_field"] == "life_date"
    assert risk_payload["items"][0]["product"] == "Gala Apples"
    assert risk_payload["items"][0]["default_code"] == "FF-101"


def test_recent_events_handles_missing_file(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.jsonl"

    app = create_app(
        events_path_provider=lambda: missing_path,
        repository_factory=lambda: None,
        odoo_client_provider=lambda: None,
    )
    client = TestClient(app)

    resp = client.get("/events/recent", params={"limit": "bogus"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["events"] == []
    assert payload["meta"]["exists"] is False
    assert payload["meta"]["clamped"] is True
    assert payload["meta"]["limit"] == 100


def test_at_risk_reports_missing_field() -> None:
    class MissingLifeDateClient:
        def search_read(self, model, domain, fields=None, limit=None, order=None):
            if model == "ir.model" and domain == [["model", "=", "stock.lot"]]:
                return [{"id": 1}]
            if model == "ir.model.fields":
                return []
            raise AssertionError(f"Unexpected call: {model}, {domain}")

    app = create_app(
        repository_factory=lambda: None,
        odoo_client_provider=lambda: MissingLifeDateClient(),
    )
    client = TestClient(app)

    resp = client.get("/at-risk", params={"days": -5})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["items"] == []
    assert payload["meta"]["reason"] == "no_expiry_field"
    assert payload["meta"]["clamped"] is True
    assert payload["meta"]["days"] == 1


def test_at_risk_falls_back_to_expiration_date() -> None:
    class ExpirationDateClient:
        def __init__(self) -> None:
            self.calls = []

        def search_read(self, model, domain, fields=None, limit=None, order=None):
            if model == "ir.model":
                return [{"id": 1}]
            if model == "ir.model.fields":
                # Simulate only expiration_date existing
                if ["name", "=", "life_date"] in domain:
                    return []
                if ["name", "=", "expiration_date"] in domain:
                    return [{"id": 2}]
            raise AssertionError(f"Unexpected call: {model}, {domain}")

    class FakeRepository:
        def __init__(self) -> None:
            self.expiry_field = None

        def set_lot_expiry_field(self, field):
            self.expiry_field = field

        def load_snapshot(self):
            return snapshot_from_quants(
                [
                    QuantRecord(
                        id=1,
                        product_id=101,
                        product_name="Bananas",
                        default_code="BAN-1",
                        category="Produce",
                        quantity=10.0,
                        lot_id=5,
                        lot_name="LOT-BAN",
                        life_date=date(2024, 1, 15),
                    )
                ]
            )

    app = create_app(
        repository_factory=lambda: FakeRepository(),
        odoo_client_provider=lambda: ExpirationDateClient(),
    )
    client = TestClient(app)

    resp = client.get("/at-risk")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["meta"]["lot_expiry_field"] == "expiration_date"
    assert payload["items"][0]["default_code"] == "BAN-1"


def test_events_endpoint_filters_by_type_and_since(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    run_migration(db_path)
    store = EventStore(db_path)
    now = datetime.now(timezone.utc)
    store.add_events(
        [
            InventoryEvent(
                ts=now - timedelta(days=1),
                type="receiving",
                product="Gala Apples",
                lot="LOT-1",
                qty=5.0,
                before=10.0,
                after=15.0,
            ),
            InventoryEvent(
                ts=now - timedelta(days=5),
                type="receiving",
                product="Gala Apples",
                lot="LOT-2",
                qty=5.0,
                before=15.0,
                after=20.0,
            ),
            InventoryEvent(
                ts=now - timedelta(days=2),
                type="sell_down",
                product="Whole Milk",
                lot="LOT-3",
                qty=-2.0,
                before=8.0,
                after=6.0,
            ),
        ]
    )

    app = create_app(
        events_path_provider=lambda: tmp_path / "unused.jsonl",
        repository_factory=lambda: None,
        odoo_client_provider=lambda: None,
        event_store_provider=lambda: EventStore(db_path),
    )
    client = TestClient(app)

    resp = client.get("/events", params={"type": "receiving", "since": "3d", "limit": 5})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["meta"]["count"] == 1
    assert payload["meta"]["type"] == "receiving"
    assert payload["events"][0]["product"] == "Gala Apples"
    assert payload["events"][0]["lot"] == "LOT-1"


def test_metrics_summary_reports_counts(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    run_migration(db_path)
    store = EventStore(db_path)
    now = datetime.now(timezone.utc)
    store.add_events(
        [
            InventoryEvent(
                ts=now - timedelta(hours=1),
                type="receiving",
                product="Gala Apples",
                lot=None,
                qty=5.0,
                before=10.0,
                after=15.0,
            ),
            InventoryEvent(
                ts=now - timedelta(hours=2),
                type="sell_down",
                product="Whole Milk",
                lot="LOT-3",
                qty=-2.0,
                before=8.0,
                after=6.0,
            ),
        ]
    )

    app = create_app(
        events_path_provider=lambda: tmp_path / "unused.jsonl",
        repository_factory=lambda: None,
        odoo_client_provider=lambda: None,
        event_store_provider=lambda: EventStore(db_path),
    )
    client = TestClient(app)

    resp = client.get("/metrics/summary")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["events"]["total"] == 2
    assert payload["events"]["by_type"]["receiving"] == 1
    assert payload["events"]["by_type"]["sell_down"] == 1
    assert payload["meta"]["source"] == "database"
