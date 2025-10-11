from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from apps.web import create_app
from apps.web.data import calculate_at_risk, load_recent_events, snapshot_from_quants
from packages.db import EventStore, InventoryEvent
from scripts.db_migrate import run as run_migration
from services.recall import QuarantinedItem, RecallResult
from services.simulator.inventory import QuantRecord


def _media_type(response):
    media_type = getattr(response, "media_type", None)
    if media_type is None and hasattr(response, "headers"):
        return response.headers.get("content-type")
    return media_type


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


def test_root_endpoint_lists_links() -> None:
    app = create_app(
        repository_factory=lambda: None,
        odoo_client_provider=lambda: None,
        recall_service_factory=lambda: None,
    )
    client = TestClient(app)

    response = client.get("/")
    assert response.status_code == 200
    payload = response.json()
    assert payload["app"] == "FoodFlow reporting API"
    assert payload["status"] == "ok"
    assert "/health" in payload["links"].values()
    assert payload["links"]["events"] == "/events"
    assert payload["links"]["metrics_impact"] == "/metrics/impact"
    assert payload["links"]["recall_trigger"] == "/recall/trigger"


def test_recall_trigger_invokes_service() -> None:
    class FakeRecallService:
        def __init__(self) -> None:
            self.calls = []

        def recall(self, *, default_codes=None, categories=None):
            self.calls.append((default_codes, categories))
            return [
                RecallResult(
                    product="Gala Apples",
                    default_code="FF101",
                    lot="LOT-1",
                    quantity=5.0,
                    source_location="Sales Floor",
                    destination_location="Quarantine",
                )
            ]

        def list_quarantined(self):
            return []

    fake_service = FakeRecallService()
    app = create_app(
        repository_factory=lambda: None,
        odoo_client_provider=lambda: None,
        recall_service_factory=lambda: fake_service,
    )
    client = TestClient(app)

    response = client.post("/recall/trigger", json={"codes": ["FF101"], "categories": []})
    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["count"] == 1
    assert fake_service.calls == [(["FF101"], [])]
    assert payload["items"][0]["product"] == "Gala Apples"
    assert payload["items"][0]["destination_location"] == "Quarantine"


def test_recall_quarantined_lists_items() -> None:
    class FakeRecallService:
        def recall(self, *, default_codes=None, categories=None):
            return []

        def list_quarantined(self):
            return [
                QuarantinedItem(
                    product="Whole Milk",
                    default_code="FF102",
                    lot="LOT-2",
                    quantity=8.0,
                )
            ]

    app = create_app(
        repository_factory=lambda: None,
        odoo_client_provider=lambda: None,
        recall_service_factory=lambda: FakeRecallService(),
    )
    client = TestClient(app)

    response = client.get("/recall/quarantined")
    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["count"] == 1
    assert payload["items"][0]["default_code"] == "FF102"


def test_recall_trigger_returns_service_unavailable() -> None:
    app = create_app(
        repository_factory=lambda: None,
        odoo_client_provider=lambda: None,
        recall_service_factory=lambda: None,
    )
    client = TestClient(app)

    response = client.post("/recall/trigger", json={"codes": ["FF101"]})
    assert response.status_code == 503


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


def test_markdown_labels_generate_pdfs(tmp_path: Path) -> None:
    output_dir = tmp_path / "labels"

    class FakeClient:
        def search_read(self, model, domain, fields=None, limit=None, order=None):
            assert model == "product.product"
            return [
                {
                    "id": 10,
                    "name": "Gala Apples",
                    "default_code": "FF101",
                    "barcode": "1234567890123",
                    "categ_id": [1, "Produce"],
                    "description": "Sweet and crisp apples.",
                },
                {
                    "id": 11,
                    "name": "Whole Milk",
                    "default_code": "FF102",
                    "categ_id": [2, "Dairy"],
                },
            ]

    app = create_app(
        events_path_provider=lambda: tmp_path / "events.jsonl",
        repository_factory=lambda: None,
        odoo_client_provider=lambda: FakeClient(),
        labels_path_provider=lambda: output_dir,
    )
    client = TestClient(app)

    response = client.post("/labels/markdown", json={"default_codes": ["FF101", "FF102"]})
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    generated = {entry["code"]: entry for entry in payload["generated"]}
    assert generated.keys() == {"FF101", "FF102"}
    for code, entry in generated.items():
        assert entry["path"].endswith(f"{code}.pdf")
        assert entry["url"] == f"/static/labels/{code}.pdf"
    assert (output_dir / "FF101.pdf").exists()
    assert (output_dir / "FF102.pdf").exists()
    for entry in payload["generated"]:
        pdf_path = Path(entry["path"])
        assert pdf_path.exists()
        assert pdf_path.read_bytes().startswith(b"%PDF")

    index_resp = client.get("/out/labels/")
    assert index_resp.status_code == 200
    index_payload = index_resp.json()
    assert index_payload["meta"]["count"] == 2
    filenames = {item["filename"] for item in index_payload["labels"]}
    assert filenames == {"FF101.pdf", "FF102.pdf"}
    urls = {item["url"] for item in index_payload["labels"]}
    assert urls == {"/static/labels/FF101.pdf", "/static/labels/FF102.pdf"}

    combined = client.post("/labels/markdown?combined=true", json={"default_codes": ["FF101", "FF102"]})
    assert combined.status_code == 200
    assert combined.headers["content-type"].startswith("application/pdf")
    assert combined.content.startswith(b"%PDF")


def test_markdown_labels_combined_cached(tmp_path: Path) -> None:
    output_dir = tmp_path / "labels"

    class FakeClient:
        def search_read(self, model, domain, fields=None, limit=None, order=None):
            assert model == "product.product"
            return [
                {
                    "id": 10,
                    "name": "Gala Apples",
                    "default_code": "FF101",
                    "barcode": "1234567890123",
                    "categ_id": [1, "Produce"],
                    "description": "Sweet and crisp apples.",
                },
                {
                    "id": 11,
                    "name": "Whole Milk",
                    "default_code": "FF102",
                    "categ_id": [2, "Dairy"],
                },
            ]

    app = create_app(
        events_path_provider=lambda: tmp_path / "events.jsonl",
        repository_factory=lambda: None,
        odoo_client_provider=lambda: FakeClient(),
        labels_path_provider=lambda: output_dir,
    )
    client = TestClient(app)

    first = client.post("/labels/markdown?combined=true", json={"default_codes": ["FF101", "FF102"]})
    assert first.status_code == 200
    combined_files = sorted(output_dir.glob("labels-combined-*.pdf"))
    assert len(combined_files) == 1
    combined_path = combined_files[0]
    first_mtime = combined_path.stat().st_mtime
    first_bytes = combined_path.read_bytes()

    second = client.post("/labels/markdown?combined=true", json={"default_codes": ["FF101", "FF102"]})
    assert second.status_code == 200
    assert second.content.startswith(b"%PDF")
    assert combined_path.read_bytes() == first_bytes
    assert combined_path.stat().st_mtime == first_mtime

    listing = client.get("/out/labels/")
    assert listing.status_code == 200
    payload = listing.json()
    filenames = {item["filename"] for item in payload["labels"]}
    assert combined_path.name in filenames


def test_markdown_labels_validates_payload() -> None:
    app = create_app(
        repository_factory=lambda: None,
        odoo_client_provider=lambda: None,
    )
    client = TestClient(app)

    resp = client.post("/labels/markdown", json={"default_codes": []})
    assert resp.status_code == 400
    error = resp.json()
    assert error["detail"]["default_codes"]


def test_markdown_labels_requires_body() -> None:
    app = create_app(
        repository_factory=lambda: None,
        odoo_client_provider=lambda: None,
    )
    client = TestClient(app)

    resp = client.post("/labels/markdown")
    assert resp.status_code == 400
    error = resp.json()
    assert error["detail"]["default_codes"]


def test_flagged_endpoint_applies_filters(tmp_path: Path) -> None:
    flagged_path = tmp_path / "flagged.json"
    data = [
        {
            "default_code": "FF101",
            "product": "Gala Apples",
            "reason": "near_expiry",
            "store": "Downtown",
            "stores": ["Downtown"],
            "category": "Produce",
            "quantity": 4.5,
            "outcome": "MARKDOWN",
        },
        {
            "default_code": "FF202",
            "product": "Whole Milk",
            "reason": "low_movement",
            "store": "Uptown",
            "stores": ["Uptown"],
            "category": "Dairy",
            "quantity": 3.0,
            "outcome": "DONATE",
        },
    ]
    flagged_path.write_text(json.dumps(data), encoding="utf-8")

    app = create_app(
        repository_factory=lambda: None,
        odoo_client_provider=lambda: None,
        flagged_path_provider=lambda: flagged_path,
    )
    client = TestClient(app)

    response = client.get("/flagged", params={"store": "Downtown"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["total"] == 2
    assert payload["meta"]["count"] == 1
    assert payload["meta"]["estimated_weight_lbs"] == 4.5
    assert payload["items"][0]["default_code"] == "FF101"
    assert "Downtown" in payload["meta"]["filters"]["stores"]
    assert "Produce" in payload["meta"]["filters"]["categories"]
    assert payload["meta"]["active_filters"]["store"] == "Downtown"
    assert payload["items"][0]["product_name"] == "Gala Apples"
    assert payload["items"][0]["qty"] == 4.5
    assert payload["items"][0]["estimated_weight_lbs"] == 4.5
    assert payload["items"][0]["unit"] == "LB"


def test_export_flagged_csv_includes_headers(tmp_path: Path) -> None:
    flagged_path = tmp_path / "flagged.json"
    data = [
        {
            "default_code": "FF101",
            "product": "Gala Apples",
            "lot": "LOT-1",
            "reason": "low_movement",
            "outcome": "MARKDOWN",
            "suggested_qty": 12.5,
            "quantity": 10,
            "unit": "EA",
            "price_markdown_pct": 0.15,
            "store": "Downtown",
            "stores": ["Downtown", "Warehouse"],
            "category": "Produce",
            "notes": "Discount gently",
        }
    ]
    flagged_path.write_text(json.dumps(data), encoding="utf-8")

    app = create_app(
        repository_factory=lambda: None,
        odoo_client_provider=lambda: None,
        flagged_path_provider=lambda: flagged_path,
    )
    client = TestClient(app)

    response = client.get("/export/flagged.csv")
    assert response.status_code == 200
    assert "text/csv" in (_media_type(response) or "")
    text = response.text
    assert text.startswith("\ufeff")
    reader = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    assert reader[0] == [
        "default_code",
        "product",
        "lot",
        "reason",
        "outcome",
        "suggested_qty",
        "quantity",
        "unit",
        "estimated_weight_lbs",
        "price_markdown_pct",
        "store",
        "stores",
        "category",
        "notes",
    ]
    assert reader[1] == [
        "FF101",
        "Gala Apples",
        "LOT-1",
        "low_movement",
        "MARKDOWN",
        "12.5",
        "10",
        "LB",
        "12.5",
        "0.15",
        "Downtown",
        "Downtown; Warehouse",
        "Produce",
        "Discount gently",
    ]


def test_export_flagged_csv_requires_api_key(tmp_path: Path, monkeypatch) -> None:
    flagged_path = tmp_path / "flagged.json"
    flagged_path.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("FOODFLOW_WEB_API_KEY", "secret")

    app = create_app(
        repository_factory=lambda: None,
        odoo_client_provider=lambda: None,
        flagged_path_provider=lambda: flagged_path,
    )
    client = TestClient(app)

    unauthorized = client.get("/export/flagged.csv")
    assert unauthorized.status_code == 401

    authorized = client.get("/export/flagged.csv", params={"api_key": "secret"})
    assert authorized.status_code == 200
    assert "text/csv" in (_media_type(authorized) or "")


def test_metrics_impact_summarizes_decisions(tmp_path: Path) -> None:
    flagged_path = tmp_path / "flagged.json"
    data = [
        {
            "default_code": "FF101",
            "outcome": "MARKDOWN",
            "price_markdown_pct": 0.2,
            "suggested_qty": 5.0,
        },
        {
            "default_code": "FF102",
            "outcome": "MARKDOWN",
            "price_markdown_pct": 0.15,
            "suggested_qty": 2.0,
        },
        {
            "default_code": "FF150",
            "outcome": "DONATE",
            "suggested_qty": 4.0,
        },
    ]
    flagged_path.write_text(json.dumps(data), encoding="utf-8")

    app = create_app(
        repository_factory=lambda: None,
        odoo_client_provider=lambda: None,
        flagged_path_provider=lambda: flagged_path,
    )
    client = TestClient(app)

    response = client.get("/metrics/impact")
    assert response.status_code == 200
    payload = response.json()
    impact = payload["impact"]
    assert impact["diverted_value_usd"] == 17.89
    assert impact["donated_weight_lbs"] == 4.0
    assert payload["meta"]["count"] == 3
    assert payload["meta"]["markdown_count"] == 2
    assert payload["meta"]["donation_count"] == 1


def test_dashboard_flagged_page_renders(tmp_path: Path) -> None:
    flagged_path = tmp_path / "flagged.json"
    flagged_path.write_text(
        json.dumps(
            [
                {
                    "default_code": "FF101",
                    "product": "Gala Apples",
                    "reason": "near_expiry",
                    "store": "Downtown",
                    "stores": ["Downtown"],
                    "category": "Produce",
                }
            ]
        ),
        encoding="utf-8",
    )

    app = create_app(
        repository_factory=lambda: None,
        odoo_client_provider=lambda: None,
        flagged_path_provider=lambda: flagged_path,
    )
    client = TestClient(app)

    response = client.get("/dashboard/flagged")
    assert response.status_code == 200
    content = response.text
    assert "Flagged Decisions Dashboard" in content
    assert "/flagged" in content
    assert "impact-diverted" in content


def test_labels_index_handles_missing_directory(tmp_path: Path) -> None:
    app = create_app(
        repository_factory=lambda: None,
        odoo_client_provider=lambda: None,
        labels_path_provider=lambda: tmp_path / "labels",
    )
    client = TestClient(app)

    resp = client.get("/out/labels/")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["meta"]["exists"] is False
    assert payload["meta"]["count"] == 0
    assert payload["labels"] == []



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


def test_export_events_csv_filters_results(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    run_migration(db_path)
    store = EventStore(db_path)
    timestamp = datetime(2024, 1, 12, 15, 30, tzinfo=timezone.utc)
    store.add_events(
        [
            InventoryEvent(
                ts=timestamp,
                type="receiving",
                product="Gala Apples",
                lot="LOT-1",
                qty=5.0,
                before=10.0,
                after=15.0,
            ),
            InventoryEvent(
                ts=timestamp - timedelta(days=1),
                type="sell_down",
                product="Whole Milk",
                lot="LOT-2",
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

    response = client.get("/export/events.csv", params={"type": "receiving"})
    assert response.status_code == 200
    assert "text/csv" in (_media_type(response) or "")
    rows = list(csv.reader(io.StringIO(response.text.lstrip("\ufeff"))))
    assert rows[0] == [
        "timestamp",
        "type",
        "product",
        "lot",
        "quantity",
        "before_quantity",
        "after_quantity",
        "source",
    ]
    assert len(rows) == 2
    entry = rows[1]
    assert entry[0] == timestamp.isoformat()
    assert entry[1] == "receiving"
    assert entry[2] == "Gala Apples"
    assert entry[3] == "LOT-1"
    assert entry[4] == "5"
    assert entry[5] == "10"
    assert entry[6] == "15"
    assert entry[7] == "simulator"


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


def test_metrics_last_sync_reports_timestamp(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    run_migration(db_path)
    store = EventStore(db_path)
    recorded = datetime(2024, 1, 12, 15, 45, tzinfo=timezone.utc)
    store.record_integration_sync(recorded)

    app = create_app(
        events_path_provider=lambda: tmp_path / "unused.jsonl",
        repository_factory=lambda: None,
        odoo_client_provider=lambda: None,
        event_store_provider=lambda: EventStore(db_path),
    )
    client = TestClient(app)

    resp = client.get("/metrics/last_sync")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["last_sync"] == recorded.isoformat()
    assert payload["meta"]["source"] == "database"


def test_metrics_last_sync_handles_missing_record(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    run_migration(db_path)

    app = create_app(
        events_path_provider=lambda: tmp_path / "unused.jsonl",
        repository_factory=lambda: None,
        odoo_client_provider=lambda: None,
        event_store_provider=lambda: EventStore(db_path),
    )
    client = TestClient(app)

    resp = client.get("/metrics/last_sync")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["last_sync"] is None
    assert payload["meta"]["source"] == "database"
    assert payload["meta"]["status"] == "not_recorded"
