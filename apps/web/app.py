"""FastAPI application exposing resilient reporting endpoints."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Tuple

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse

from packages.db import EventStore
from packages.odoo_client import OdooClient, OdooClientError
from services.docs import MarkdownLabelGenerator
from services.simulator.inventory import InventoryRepository

from .data import (
    calculate_at_risk,
    load_recent_events,
    serialize_at_risk,
    serialize_events,
    serialize_inventory_events,
)

EventsPathProvider = Callable[[], Path]
RepositoryFactory = Callable[[], Optional[InventoryRepository]]
OdooClientProvider = Callable[[], Optional[OdooClient]]
EventStoreProvider = Callable[[], EventStore]
LabelsPathProvider = Callable[[], Path]

_DURATION_RE = re.compile(r"^(?P<value>\d+)(?P<unit>[dhm])$")


def create_app(
    *,
    events_path_provider: EventsPathProvider | None = None,
    repository_factory: RepositoryFactory | None = None,
    odoo_client_provider: OdooClientProvider | None = None,
    event_store_provider: EventStoreProvider | None = None,
    logger: logging.Logger | None = None,
    labels_path_provider: LabelsPathProvider | None = None,
) -> FastAPI:
    """Construct the FastAPI application."""

    app_logger = logger or logging.getLogger("foodflow.web")
    events_provider = events_path_provider or _default_events_path
    repository_provider = repository_factory or _default_repository_factory
    odoo_provider = odoo_client_provider or _default_odoo_client
    store_provider = event_store_provider or _default_event_store
    labels_provider = labels_path_provider or _default_labels_path

    app = FastAPI(title="FoodFlow Reporting API")

    @app.exception_handler(Exception)
    def _handle_unexpected(exc: Exception) -> JSONResponse:
        app_logger.error("Unhandled application error", exc_info=True)
        return JSONResponse(
            {"error": "internal", "detail": "see server logs"},
            status_code=500,
        )

    @app.get("/health", response_class=JSONResponse)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/events/recent", response_class=JSONResponse)
    def recent_events(limit: str = Query("100")) -> dict[str, object]:
        limit_value, clamped = _coerce_int(limit, default=100, minimum=1, maximum=1000)
        events_path = events_provider()
        exists = events_path.exists()
        meta: dict[str, object] = {
            "source": "jsonl",
            "limit": limit_value,
            "exists": exists,
            "clamped": clamped,
        }
        if not exists:
            app_logger.info("Events file %s not found; returning empty result", events_path)
            return {"events": [], "meta": meta}
        try:
            records = load_recent_events(events_path, limit=limit_value)
            events = serialize_events(records)
        except ValueError:
            app_logger.exception("Failed to parse events file %s", events_path)
            meta["error"] = "invalid_events_json"
            return {"events": [], "meta": meta}
        except OSError:
            app_logger.exception("Failed to read events file %s", events_path)
            meta["error"] = "events_read_failed"
            return {"events": [], "meta": meta}
        meta["count"] = len(events)
        return {"events": events, "meta": meta}

    @app.get("/events", response_class=JSONResponse)
    def events(
        limit: int = Query(100, ge=1, le=1000),
        type: str | None = Query(None),
        since: str | None = Query(None),
    ) -> dict[str, object]:
        event_type = type
        meta: dict[str, object] = {
            "source": "database",
            "limit": limit,
            "type": event_type,
            "since": since,
        }
        try:
            since_dt = _parse_since(since)
        except ValueError as exc:
            raise HTTPException(400, {"since": str(exc)}) from exc

        try:
            store = store_provider()
            records = store.list_events(event_type=event_type, since=since_dt, limit=limit)
        except Exception:
            app_logger.exception("Failed to load events from database")
            meta["error"] = "events_query_failed"
            return {"events": [], "meta": meta}

        payload = serialize_inventory_events(records)
        meta["count"] = len(payload)
        return {"events": payload, "meta": meta}

    @app.get("/metrics/summary", response_class=JSONResponse)
    def metrics_summary() -> dict[str, object]:
        try:
            store = store_provider()
            summary = store.metrics_summary()
        except Exception:
            app_logger.exception("Failed to calculate metrics summary")
            return {
                "meta": {"source": "database", "error": "metrics_query_failed"},
                "events": {"total": 0, "by_type": {}},
            }

        return {
            "meta": {"source": "database"},
            "events": {
                "total": summary.get("total_events", 0),
                "by_type": summary.get("events_by_type", {}),
            },
        }

    @app.get("/at-risk", response_class=JSONResponse)
    def at_risk(days: str = Query("3")) -> dict[str, object]:
        days_value, clamped = _coerce_int(days, default=3, minimum=1, maximum=30)
        meta: dict[str, object] = {"days": days_value, "clamped": clamped}

        client = odoo_provider()
        if client is None:
            meta["error"] = "odoo_unreachable"
            app_logger.error("Odoo client unavailable; returning empty at-risk list")
            return {"items": [], "meta": meta}

        try:
            if not _model_exists(client, "stock.lot"):
                meta["reason"] = "no_stock_lot_model"
                return {"items": [], "meta": meta}
            expiry_field = _resolve_expiry_field(client)
            if expiry_field is None:
                meta["reason"] = "no_expiry_field"
                return {"items": [], "meta": meta}
            meta["lot_expiry_field"] = expiry_field
        except OdooClientError:
            meta["error"] = "odoo_unreachable"
            app_logger.exception("Failed to query Odoo metadata")
            return {"items": [], "meta": meta}

        repository = repository_provider()
        if repository is None:
            meta["error"] = "odoo_unreachable"
            app_logger.error("Inventory repository unavailable; returning empty at-risk list")
            return {"items": [], "meta": meta}
        if hasattr(repository, "set_lot_expiry_field"):
            try:
                repository.set_lot_expiry_field(expiry_field)
            except Exception:
                app_logger.exception("Failed to configure repository with expiry field")

        try:
            snapshot = repository.load_snapshot()
        except OdooClientError:
            meta["error"] = "odoo_unreachable"
            app_logger.exception("Failed to load inventory snapshot")
            return {"items": [], "meta": meta}
        except Exception:
            meta["error"] = "odoo_unreachable"
            app_logger.exception("Unexpected error loading inventory snapshot")
            return {"items": [], "meta": meta}

        items = calculate_at_risk(snapshot, threshold_days=days_value)
        payload = serialize_at_risk(items)
        meta["count"] = len(payload)
        return {"items": payload, "meta": meta}

    @app.post("/labels/markdown", response_class=JSONResponse)
    def markdown_labels(default_codes: object = None) -> dict[str, object]:
        if default_codes is None:
            raise HTTPException(400, {"default_codes": "provide JSON body with default_codes list"})
        if not isinstance(default_codes, list):
            raise HTTPException(400, {"default_codes": "expected list of product codes"})
        codes: list[str] = []
        for value in default_codes:
            if not isinstance(value, str):
                raise HTTPException(400, {"default_codes": "all codes must be strings"})
            trimmed = value.strip()
            if not trimmed:
                continue
            if trimmed not in codes:
                codes.append(trimmed)
        if not codes:
            raise HTTPException(400, {"default_codes": "provide at least one product code"})

        client = odoo_provider()
        if client is None:
            app_logger.error("Cannot generate labels: Odoo client unavailable")
            return {
                "labels": [],
                "links": {},
                "meta": {
                    "error": "odoo_unreachable",
                    "requested": codes,
                },
            }

        output_dir = labels_provider()
        generator = MarkdownLabelGenerator(client, output_dir=output_dir)
        try:
            documents = generator.generate(codes)
        except Exception:
            app_logger.exception("Failed to generate label PDFs")
            return {
                "labels": [],
                "links": {},
                "meta": {
                    "error": "label_generation_failed",
                    "requested": codes,
                },
            }

        links = {doc.default_code: str(doc.pdf_path) for doc in documents}
        generated_at = (
            documents[0].generated_at.isoformat() if documents else datetime.now(timezone.utc).isoformat()
        )
        meta: dict[str, object] = {
            "requested": codes,
            "count": len(documents),
            "missing": [doc.default_code for doc in documents if not doc.found],
            "generated_at": generated_at,
            "output_dir": str(output_dir),
        }
        return {
            "labels": [doc.to_dict() for doc in documents],
            "links": links,
            "meta": meta,
        }

    @app.get("/out/labels", response_class=JSONResponse)
    def labels_index_no_slash() -> dict[str, object]:
        output_dir = labels_provider()
        return _labels_directory_listing(output_dir)

    @app.get("/out/labels/", response_class=JSONResponse)
    def labels_index() -> dict[str, object]:
        output_dir = labels_provider()
        return _labels_directory_listing(output_dir)

    return app


def _coerce_int(
    raw_value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> Tuple[int, bool]:
    value = default
    clamped = False
    if raw_value not in (None, ""):
        try:
            value = int(raw_value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            clamped = True
            value = default
    if value < minimum:
        clamped = True
        value = minimum
    if value > maximum:
        clamped = True
        value = maximum
    return value, clamped


def _model_exists(client: OdooClient, model: str) -> bool:
    result = client.search_read(
        "ir.model",
        domain=[["model", "=", model]],
        fields=["id"],
        limit=1,
    )
    return bool(result)


def _field_exists(client: OdooClient, model: str, field: str) -> bool:
    result = client.search_read(
        "ir.model.fields",
        domain=[["model", "=", model], ["name", "=", field]],
        fields=["id"],
        limit=1,
    )
    return bool(result)


def _resolve_expiry_field(client: OdooClient) -> str | None:
    for field in ("life_date", "expiration_date"):
        if _field_exists(client, "stock.lot", field):
            return field
    return None


def _parse_since(value: str | None, *, reference: datetime | None = None) -> datetime | None:
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None

    ref = reference or datetime.now(timezone.utc)
    match = _DURATION_RE.fullmatch(raw.lower())
    if match:
        amount = int(match.group("value"))
        unit = match.group("unit")
        if unit == "d":
            delta = timedelta(days=amount)
        elif unit == "h":
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(minutes=amount)
        return ref - delta

    try:
        normalized = raw.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("Invalid duration or ISO timestamp") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _default_events_path() -> Path:
    from .data import DEFAULT_EVENTS_PATH

    return DEFAULT_EVENTS_PATH


def _default_event_store() -> EventStore:
    return EventStore()


def _default_odoo_client() -> OdooClient | None:
    logger = logging.getLogger("foodflow.web")
    try:
        client = OdooClient()
        client.authenticate()
        return client
    except OdooClientError:
        logger.exception("Failed to authenticate default Odoo client")
        return None
    except Exception:
        logger.exception("Unexpected error creating default Odoo client")
        return None


def _default_repository_factory() -> InventoryRepository | None:
    client = _default_odoo_client()
    if client is None:
        return None
    return InventoryRepository(client)


def _default_labels_path() -> Path:
    return Path("out/labels")


def _labels_directory_listing(directory: Path) -> dict[str, object]:
    if not directory.exists():
        return {
            "labels": [],
            "meta": {
                "exists": False,
                "count": 0,
                "output_dir": str(directory),
            },
        }
    items: list[dict[str, object]] = []
    for pdf_path in sorted(directory.glob("*.pdf")):
        try:
            stat = pdf_path.stat()
        except OSError:
            continue
        items.append(
            {
                "filename": pdf_path.name,
                "path": str(pdf_path),
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    return {
        "labels": items,
        "meta": {
            "exists": True,
            "count": len(items),
            "output_dir": str(directory),
        },
    }


__all__ = ["create_app"]
