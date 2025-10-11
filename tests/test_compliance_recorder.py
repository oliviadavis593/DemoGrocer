from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from packages.db import ComplianceEvent, EventStore, compliance_session
from services.compliance import CSV_HEADERS, to_compliance_event, validate_and_persist


def test_to_compliance_event_computes_extended_value() -> None:
    timestamp = datetime(2024, 1, 5, 9, 0, tzinfo=timezone.utc)
    decision = {
        "default_code": "FF300",
        "reason": "near_expiry",
        "outcome": "DONATE",
        "suggested_qty": 4,
        "notes": "Donate this pallet",
    }
    enrichment = {
        "product_name": "Community Soup",
        "category": "Center Store",
        "store": "Midtown",
        "qty": 4,
        "unit_cost": 2.5,
        "list_price": 4.25,
    }
    staff = {"username": "coordinator", "staff_id": "EMP-12"}

    payload = to_compliance_event(decision, enrichment, staff, None, timestamp=timestamp)

    assert payload["event_type"] == "donation"
    assert payload["quantity_units"] == 4.0
    assert payload["unit_cost"] == 2.5
    assert payload["fair_market_value"] == 4.25
    assert payload["extended_value"] == 17.0
    assert payload["irs_170e3_flags"]["qualified_org"] is True
    assert payload["captured_by"] == "coordinator"
    assert payload["notes"] == "Donate this pallet"


def test_validate_and_persist_creates_db_csv_and_audit(tmp_path: Path) -> None:
    db_path = tmp_path / "foodflow.db"
    csv_path = tmp_path / "compliance.csv"
    timestamp = datetime(2024, 1, 6, 14, 15, tzinfo=timezone.utc)
    decision = {
        "default_code": "FF301",
        "reason": "near_expiry",
        "outcome": "DONATE",
        "suggested_qty": 3,
    }
    enrichment = {
        "product_name": "Bakery Loaf",
        "category": "Bakery",
        "store": "Downtown",
        "qty": 3,
        "unit_cost": 1.2,
        "list_price": 2.0,
    }
    staff = {"username": "driver", "staff_id": "EMP-7"}

    event_payload = to_compliance_event(decision, enrichment, staff, None, timestamp=timestamp)
    stored = validate_and_persist(event_payload, db_path=db_path, csv_path=csv_path)

    assert stored.id == event_payload["event_id"]
    assert csv_path.exists()

    with csv_path.open("r", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == list(CSV_HEADERS)
    assert rows[1][0] == stored.id

    with compliance_session(db_path) as session:
        db_events = session.execute(select(ComplianceEvent)).scalars().all()
    assert len(db_events) == 1
    assert db_events[0].product_code == "FF301"
    assert db_events[0].fair_market_value == pytest.approx(2.0)
    assert db_events[0].extended_value == pytest.approx(6.0)

    store = EventStore(db_path)
    audit_events = store.list_events(event_type="compliance_donation", limit=5)
    assert len(audit_events) == 1
    audit = audit_events[0]
    assert audit.source.endswith(stored.id)
    assert audit.product == "FF301"
    assert audit.after == pytest.approx(3.0)


def test_validate_and_persist_rejects_invalid_payload(tmp_path: Path) -> None:
    db_path = tmp_path / "foodflow.db"
    csv_path = tmp_path / "compliance.csv"
    decision = {"reason": "low_movement", "outcome": "MARKDOWN", "suggested_qty": 2}
    enrichment = {"product_name": "Frozen Meal", "category": "Frozen", "store": "Uptown", "qty": 2}
    staff = {"username": "auditor"}
    timestamp = datetime(2024, 1, 7, 8, 0, tzinfo=timezone.utc)

    event_payload = to_compliance_event(decision, enrichment, staff, None, timestamp=timestamp)
    event_payload.pop("product_code", None)

    with pytest.raises(ValueError):
        validate_and_persist(event_payload, db_path=db_path, csv_path=csv_path)
