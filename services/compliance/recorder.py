"""Compliance data recorder for IRS 170(e)(3) donation and markdown events."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError as JsonSchemaValidationError

from packages.db import ComplianceEvent, EventStore, InventoryEvent, compliance_session, create_all

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "contracts" / "schemas" / "compliance.schema.json"
DEFAULT_CSV_PATH = Path(os.getenv("FOODFLOW_COMPLIANCE_CSV_PATH", "out/compliance/compliance_events.csv"))
DEFAULT_IRS_FLAGS: dict[str, bool] = {
    "qualified_org": True,
    "charitable_purpose": True,
    "wholesome_food": True,
    "no_compensation": True,
    "proper_handling": True,
}
CSV_HEADERS: Sequence[str] = (
    "event_id",
    "timestamp",
    "event_type",
    "outcome",
    "reason",
    "product_code",
    "product_name",
    "category",
    "lot_code",
    "life_date",
    "store",
    "location_id",
    "quantity_units",
    "uom",
    "weight_lbs",
    "unit_cost",
    "fair_market_value",
    "extended_value",
    "captured_by",
    "staff_id",
    "photo_url",
    "irs_qualified_org",
    "irs_charitable_purpose",
    "irs_wholesome_food",
    "irs_no_compensation",
    "irs_proper_handling",
    "meta_json",
)


with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
    _SCHEMA = json.load(handle)
_VALIDATOR = Draft202012Validator(_SCHEMA)
_VALID_REASONS = {"near_expiry", "low_movement", "overstock", "recall", "manual", "other"}
_OUTCOME_EVENT_TYPE = {
    "DONATE": "donation",
    "MARKDOWN": "markdown",
    "RECALL_QUARANTINE": "recall_quarantine",
    "DIVERT": "divert",
}
LOGGER = logging.getLogger("foodflow.compliance.recorder")


def resolve_csv_path(path: Path | None = None) -> Path:
    """Resolve the CSV output path, honoring overrides and environment variables."""

    if path is not None:
        candidate = Path(path)
    else:
        override = os.getenv("FOODFLOW_COMPLIANCE_CSV_PATH")
        candidate = Path(override) if override else DEFAULT_CSV_PATH
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate


def to_compliance_event(
    decision: Mapping[str, Any] | Any,
    enrichment: Mapping[str, Any] | None,
    staff: Mapping[str, Any] | Any | None,
    evidence: Mapping[str, Any] | None = None,
    *,
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    """Build a JSON object conforming to the compliance schema."""

    outcome = _coerce_str(_access(decision, "outcome"), default="DIVERT").upper()
    event_type = _OUTCOME_EVENT_TYPE.get(outcome, "divert")

    product_code = _coerce_str(
        _first_not_none(
            _access(decision, "default_code"),
            _access(enrichment, "default_code"),
            _access(enrichment, "product_code"),
        ),
        default="UNKNOWN",
    )
    product_name = _coerce_str(
        _first_not_none(
            _access(enrichment, "product_name"),
            _access(decision, "product_name"),
            _access(decision, "product"),
        ),
        default="Unknown Product",
    )
    category = _coerce_str(
        _first_not_none(
            _access(enrichment, "category"),
            _access(decision, "category"),
        ),
        default="Uncategorized",
    )
    lot_code = _coerce_optional_str(
        _first_not_none(
            _access(decision, "lot"),
            _access(enrichment, "lot"),
            _access(enrichment, "lot_code"),
            _access(decision, "lot_code"),
        )
    )
    life_date = _coerce_iso_datetime(
        _first_not_none(
            _access(enrichment, "life_date"),
            _access(decision, "life_date"),
            _access(decision, "expiry_date"),
        )
    )

    stores = _as_sequence(_access(enrichment, "stores"))
    store = _coerce_str(
        _first_not_none(
            _access(enrichment, "store"),
            stores[0] if stores else None,
            _access(decision, "store"),
        ),
        default="Unassigned",
    )
    location_id = _coerce_optional_int(
        _first_not_none(
            _access(enrichment, "location_id"),
            _access(decision, "location_id"),
        )
    )

    reason_raw = _coerce_str(_access(decision, "reason"), default="other").lower()
    reason = reason_raw if reason_raw in _VALID_REASONS else "other"

    notes = _coerce_optional_str(_access(decision, "notes"))
    if notes is None and enrichment is not None:
        notes = _coerce_optional_str(_access(enrichment, "notes"))
    notes = _truncate(notes or "", max_length=2000)

    quantity_units = _coerce_float(
        _first_not_none(
            _access(decision, "quantity_units"),
            _access(decision, "quantity"),
            _access(decision, "suggested_qty"),
            _access(enrichment, "quantity_units"),
            _access(enrichment, "quantity"),
            _access(enrichment, "qty"),
        ),
        default=0.0,
    )
    if quantity_units < 0:
        quantity_units = 0.0

    uom = _coerce_str(
        _first_not_none(
            _access(decision, "uom"),
            _access(decision, "unit"),
            _access(enrichment, "uom"),
            _access(enrichment, "unit"),
        ),
        default="EA",
    ).upper()

    weight_lbs = _coerce_optional_float(
        _first_not_none(
            _access(decision, "weight_lbs"),
            _access(enrichment, "weight_lbs"),
            _access(decision, "estimated_weight_lbs"),
            _access(enrichment, "estimated_weight_lbs"),
        )
    )

    unit_cost = _coerce_float(
        _first_not_none(
            _access(enrichment, "unit_cost"),
            _access(enrichment, "standard_price"),
            _access(decision, "unit_cost"),
            _access(decision, "cost"),
        ),
        default=0.0,
    )
    fair_market_value = _coerce_float(
        _first_not_none(
            _access(enrichment, "fair_market_value"),
            _access(enrichment, "list_price"),
            _access(decision, "fair_market_value"),
            _access(decision, "list_price"),
            _access(decision, "unit_price"),
        ),
        default=unit_cost,
    )
    extended_value = round(fair_market_value * quantity_units, 2)

    captured_by = _resolve_captured_by(staff)
    staff_id = _coerce_optional_str(
        _first_not_none(
            _access(staff, "staff_id"),
            _access(staff, "id"),
            _access(staff, "staffId"),
            _access(staff, "external_id"),
        )
    )

    photo_url = _coerce_optional_str(
        _first_not_none(
            _access(evidence, "photo_url"),
            _access(decision, "photo_url"),
        )
    )

    irs_flags = DEFAULT_IRS_FLAGS.copy()
    irs_flags.update(_normalize_bool_map(_access(decision, "irs_170e3_flags")))
    irs_flags.update(_normalize_bool_map(_access(evidence, "irs_170e3_flags")))
    irs_flags.setdefault("qualified_org", True)
    irs_flags.setdefault("charitable_purpose", True)
    irs_flags.setdefault("wholesome_food", True)

    meta = _build_meta(decision, enrichment, evidence)

    event_timestamp = timestamp or datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "timestamp": event_timestamp.astimezone(timezone.utc).isoformat(),
        "product_code": product_code,
        "product_name": product_name,
        "category": category,
        "lot_code": lot_code,
        "life_date": life_date,
        "store": store,
        "location_id": location_id,
        "outcome": outcome,
        "reason": reason,
        "notes": notes,
        "quantity_units": round(quantity_units, 4),
        "uom": uom,
        "weight_lbs": weight_lbs,
        "unit_cost": round(unit_cost, 4),
        "fair_market_value": round(fair_market_value, 4),
        "extended_value": round(extended_value, 2),
        "captured_by": captured_by,
        "staff_id": staff_id,
        "photo_url": photo_url,
        "irs_170e3_flags": irs_flags,
        "meta": meta,
    }
    if payload["photo_url"] is None:
        payload["photo_url"] = None
    return payload


def validate_and_persist(
    event: Mapping[str, Any],
    *,
    db_path: Path | None = None,
    csv_path: Path | None = None,
) -> ComplianceEvent:
    """Validate a compliance event, persist it, and append it to the CSV export."""

    try:
        _VALIDATOR.validate(event)
    except JsonSchemaValidationError as exc:
        raise ValueError(f"Compliance event failed schema validation: {exc.message}") from exc

    create_all(db_path)
    payload = dict(event)
    model = _payload_to_model(payload)
    with compliance_session(db_path) as session:
        session.add(model)
        session.flush()
        session.refresh(model)

    csv_target = resolve_csv_path(csv_path)
    _append_csv(payload, csv_target)
    _emit_audit_event(model, db_path)
    return model


def record_donation(
    decision: Mapping[str, Any] | Any,
    enrichment: Mapping[str, Any] | None,
    staff: Mapping[str, Any] | Any | None,
    evidence: Mapping[str, Any] | None = None,
    *,
    db_path: Path | None = None,
    csv_path: Path | None = None,
) -> ComplianceEvent:
    """Record a donation outcome."""

    event = to_compliance_event(decision, enrichment, staff, evidence)
    event["event_type"] = "donation"
    event["outcome"] = "DONATE"
    return validate_and_persist(event, db_path=db_path, csv_path=csv_path)


def record_markdown(
    decision: Mapping[str, Any] | Any,
    enrichment: Mapping[str, Any] | None,
    staff: Mapping[str, Any] | Any | None,
    evidence: Mapping[str, Any] | None = None,
    *,
    db_path: Path | None = None,
    csv_path: Path | None = None,
) -> ComplianceEvent:
    """Record a markdown outcome."""

    event = to_compliance_event(decision, enrichment, staff, evidence)
    event["event_type"] = "markdown"
    event["outcome"] = "MARKDOWN"
    return validate_and_persist(event, db_path=db_path, csv_path=csv_path)


def serialize_event(model: ComplianceEvent) -> dict[str, Any]:
    """Convert a ComplianceEvent ORM instance into a schema-compliant dict."""

    life_date_value = model.life_date.isoformat() if model.life_date else None
    payload: dict[str, Any] = {
        "event_id": model.id,
        "event_type": model.event_type,
        "timestamp": model.timestamp.astimezone(timezone.utc).isoformat(),
        "product_code": model.product_code,
        "product_name": model.product_name,
        "category": model.category,
        "lot_code": model.lot_code,
        "life_date": life_date_value,
        "store": model.store,
        "location_id": model.location_id,
        "outcome": model.outcome,
        "reason": model.reason,
        "notes": model.notes or "",
        "quantity_units": round(float(model.quantity_units), 4),
        "uom": model.uom or "EA",
        "weight_lbs": model.weight_lbs,
        "unit_cost": round(float(model.unit_cost), 4),
        "fair_market_value": round(float(model.fair_market_value), 4),
        "extended_value": round(float(model.extended_value or 0.0), 2),
        "captured_by": model.captured_by,
        "staff_id": model.staff_id,
        "photo_url": model.photo_url,
        "irs_170e3_flags": {
            "qualified_org": bool(model.irs_qualified_org),
            "charitable_purpose": bool(model.irs_charitable_purpose),
            "wholesome_food": bool(model.irs_wholesome_food),
            "no_compensation": bool(model.irs_no_compensation) if model.irs_no_compensation is not None else True,
            "proper_handling": bool(model.irs_proper_handling) if model.irs_proper_handling is not None else True,
        },
        "meta": json.loads(model.meta_json) if model.meta_json else {},
    }
    return payload


def _payload_to_model(payload: Mapping[str, Any]) -> ComplianceEvent:
    timestamp = _parse_datetime(payload["timestamp"])
    life_date_value = payload.get("life_date")
    life_date = _parse_datetime(life_date_value) if life_date_value else None
    flags = payload.get("irs_170e3_flags") or {}
    meta = payload.get("meta") or {}
    return ComplianceEvent(
        id=str(payload["event_id"]),
        event_type=str(payload["event_type"]),
        timestamp=timestamp,
        product_code=str(payload["product_code"]),
        product_name=str(payload["product_name"]),
        category=str(payload["category"]),
        lot_code=_coerce_optional_str(payload.get("lot_code")),
        life_date=life_date,
        store=str(payload["store"]),
        location_id=_coerce_optional_int(payload.get("location_id")),
        outcome=str(payload["outcome"]),
        reason=str(payload["reason"]),
        notes=_coerce_optional_str(payload.get("notes")),
        quantity_units=float(payload["quantity_units"]),
        uom=str(payload.get("uom") or "EA"),
        weight_lbs=_coerce_optional_float(payload.get("weight_lbs")),
        unit_cost=float(payload["unit_cost"]),
        fair_market_value=float(payload["fair_market_value"]),
        extended_value=float(payload.get("extended_value") or 0.0),
        captured_by=str(payload["captured_by"]),
        staff_id=_coerce_optional_str(payload.get("staff_id")),
        photo_url=_coerce_optional_str(payload.get("photo_url")),
        irs_qualified_org=bool(flags.get("qualified_org", True)),
        irs_charitable_purpose=bool(flags.get("charitable_purpose", True)),
        irs_wholesome_food=bool(flags.get("wholesome_food", True)),
        irs_no_compensation=_coerce_optional_bool(flags.get("no_compensation")),
        irs_proper_handling=_coerce_optional_bool(flags.get("proper_handling")),
        meta_json=json.dumps(meta, sort_keys=True) if meta else None,
    )


def _append_csv(payload: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0
    row = [
        payload.get("event_id"),
        payload.get("timestamp"),
        payload.get("event_type"),
        payload.get("outcome"),
        payload.get("reason"),
        payload.get("product_code"),
        payload.get("product_name"),
        payload.get("category"),
        payload.get("lot_code") or "",
        payload.get("life_date") or "",
        payload.get("store"),
        payload.get("location_id") if payload.get("location_id") is not None else "",
        _format_number(payload.get("quantity_units")),
        payload.get("uom"),
        _format_number(payload.get("weight_lbs")),
        _format_number(payload.get("unit_cost")),
        _format_number(payload.get("fair_market_value")),
        _format_number(payload.get("extended_value")),
        payload.get("captured_by"),
        payload.get("staff_id") or "",
        payload.get("photo_url") or "",
        _format_bool(payload.get("irs_170e3_flags", {}).get("qualified_org", True)),
        _format_bool(payload.get("irs_170e3_flags", {}).get("charitable_purpose", True)),
        _format_bool(payload.get("irs_170e3_flags", {}).get("wholesome_food", True)),
        _format_bool(payload.get("irs_170e3_flags", {}).get("no_compensation", True)),
        _format_bool(payload.get("irs_170e3_flags", {}).get("proper_handling", True)),
        json.dumps(payload.get("meta") or {}, sort_keys=True),
    ]
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if needs_header:
            writer.writerow(CSV_HEADERS)
        writer.writerow(row)


def _emit_audit_event(record: ComplianceEvent, db_path: Path | None) -> None:
    audit_event = InventoryEvent(
        ts=record.timestamp,
        type=f"compliance_{record.event_type}",
        product=record.product_code or record.product_name,
        lot=record.lot_code,
        qty=float(record.quantity_units),
        before=0.0,
        after=float(record.quantity_units),
        source=f"compliance:{record.id}",
    )
    try:
        try:
            from scripts.db_migrate import run as run_migration

            run_migration(db_path)
        except Exception:
            LOGGER.exception("Failed to ensure inventory_events table before recording audit event")
        store = EventStore(db_path)
        store.add_events([audit_event])
    except Exception:
        LOGGER.exception("Failed to emit compliance audit event for %s", record.id)


def _resolve_captured_by(staff: Mapping[str, Any] | Any | None) -> str:
    username = _coerce_optional_str(
        _first_not_none(
            _access(staff, "username"),
            _access(staff, "login"),
            _access(staff, "email"),
            _access(staff, "name"),
        )
    )
    if username:
        return username
    env_user = os.getenv("STAFF_USER") or os.getenv("FOODFLOW_STAFF_USER")
    if env_user:
        return env_user
    return "system"


def _build_meta(
    decision: Mapping[str, Any] | Any,
    enrichment: Mapping[str, Any] | None,
    evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    decision_meta = _access(decision, "meta")
    if isinstance(decision_meta, Mapping):
        meta.update({str(k): v for k, v in decision_meta.items()})

    decision_id = _access(decision, "decision_id") or _access(decision, "id")
    if decision_id and "decision_id" not in meta:
        meta["decision_id"] = decision_id

    event_ref = _first_not_none(
        _access(enrichment, "event_ref"),
        _access(decision, "event_ref"),
        _access(evidence, "event_ref"),
    )
    if event_ref and "event_ref" not in meta:
        meta["event_ref"] = event_ref

    donee_name = _first_not_none(
        _access(decision, "donee_name"),
        _access(evidence, "donee_name"),
    )
    if donee_name:
        meta["donee_name"] = donee_name

    donee_ein = _first_not_none(
        _access(decision, "donee_ein"),
        _access(evidence, "donee_ein"),
    )
    if donee_ein:
        meta["donee_ein"] = donee_ein

    bol_url = _first_not_none(
        _access(evidence, "bol_url"),
        _access(decision, "bol_url"),
    )
    if bol_url:
        meta["bol_url"] = bol_url

    return {key: value for key, value in meta.items() if value is not None}


def _normalize_bool_map(candidate: Any) -> dict[str, bool]:
    if not isinstance(candidate, Mapping):
        return {}
    result: dict[str, bool] = {}
    for key, value in candidate.items():
        result[str(key)] = bool(value)
    return result


def _format_bool(value: Any) -> str:
    return "true" if bool(value) else "false"


def _format_number(value: Any) -> str:
    if value is None:
        return ""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return ""
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:.2f}"


def _parse_datetime(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw.astimezone(timezone.utc)
    if isinstance(raw, date):
        return datetime.combine(raw, datetime.min.time(), tzinfo=timezone.utc)
    if isinstance(raw, str):
        candidate = raw.strip()
        if not candidate:
            raise ValueError("Empty datetime string")
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Invalid datetime value: {raw}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    raise TypeError(f"Unsupported datetime value: {raw!r}")


def _coerce_iso_datetime(raw: Any) -> str | None:
    if raw in (None, "", "null"):
        return None
    try:
        parsed = _parse_datetime(raw)
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _access(source: Mapping[str, Any] | Any | None, key: str) -> Any:
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source.get(key)
    return getattr(source, key, None)


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _coerce_str(value: Any, *, default: str = "") -> str:
    result = _coerce_optional_str(value)
    if result is None or not result.strip():
        return default
    return result.strip()


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned if cleaned else None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _coerce_float(value: Any, *, default: float = 0.0) -> float:
    coerced = _coerce_optional_float(value)
    if coerced is None:
        return default
    return coerced


def _coerce_optional_float(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _coerce_optional_int(value: Any) -> int | None:
    if value in (None, "", "null"):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            return None
    return None


def _coerce_optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "y"):
            return True
        if text in ("false", "0", "no", "n"):
            return False
    return None


def _truncate(value: str, *, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    suffix = "..."
    if max_length <= len(suffix):
        return value[:max_length]
    return value[: max_length - len(suffix)] + suffix


def _as_sequence(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        result: list[str] = []
        for item in value:
            text = _coerce_optional_str(item)
            if text:
                result.append(text)
        return result
    return []


def _demo_payload(outcome: str, store: str, code: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    decision = {
        "default_code": code,
        "product_name": "Demo Product",
        "category": "Produce",
        "reason": "near_expiry",
        "outcome": outcome,
        "suggested_qty": 5,
        "notes": "Auto-generated demo event.",
    }
    enrichment = {
        "product_name": "Demo Product",
        "category": "Produce",
        "store": store,
        "qty": 5,
        "unit_cost": 2.25,
        "list_price": 3.5,
    }
    staff = {"username": "demo_user", "staff_id": "demo-1"}
    return decision, enrichment, staff


def _run_demo(csv_path: Path | None = None) -> None:
    donation_decision, donation_enrichment, donation_staff = _demo_payload("DONATE", "Downtown", "FF-DEMO-DONATE")
    markdown_decision, markdown_enrichment, markdown_staff = _demo_payload("MARKDOWN", "Uptown", "FF-DEMO-MARKDOWN")
    record_donation(donation_decision, donation_enrichment, donation_staff, None, csv_path=csv_path)
    record_markdown(markdown_decision, markdown_enrichment, markdown_staff, None, csv_path=csv_path)
    print(f"Wrote demo compliance events to {(csv_path or DEFAULT_CSV_PATH).resolve()}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compliance event recorder utilities.")
    parser.add_argument("--demo", action="store_true", help="Insert demo donation and markdown events.")
    parser.add_argument(
        "--csv-path",
        type=Path,
        help="Override the compliance CSV output path.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.demo:
        _run_demo(csv_path=args.csv_path)
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover - manual execution
    raise SystemExit(main())
