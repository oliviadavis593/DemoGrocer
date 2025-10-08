"""FastAPI application exposing resilient reporting endpoints."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional, Tuple

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from packages.odoo_client import OdooClient, OdooClientError
from services.simulator.inventory import InventoryRepository

from .data import (
    calculate_at_risk,
    load_recent_events,
    serialize_at_risk,
    serialize_events,
)

EventsPathProvider = Callable[[], Path]
RepositoryFactory = Callable[[], Optional[InventoryRepository]]
OdooClientProvider = Callable[[], Optional[OdooClient]]


def create_app(
    *,
    events_path_provider: EventsPathProvider | None = None,
    repository_factory: RepositoryFactory | None = None,
    odoo_client_provider: OdooClientProvider | None = None,
    logger: logging.Logger | None = None,
) -> FastAPI:
    """Construct the FastAPI application."""

    app_logger = logger or logging.getLogger("foodflow.web")
    events_provider = events_path_provider or _default_events_path
    repository_provider = repository_factory or _default_repository_factory
    odoo_provider = odoo_client_provider or _default_odoo_client

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


def _default_events_path() -> Path:
    from .data import DEFAULT_EVENTS_PATH

    return DEFAULT_EVENTS_PATH


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


__all__ = ["create_app"]
