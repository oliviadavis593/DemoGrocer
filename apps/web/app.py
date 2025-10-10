"""FastAPI application exposing resilient reporting endpoints."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

try:  # pragma: no cover - optional dependency
    from pydantic import BaseModel
except ModuleNotFoundError:  # pragma: no cover - lightweight fallback
    class BaseModel:
        """Minimal stand-in for pydantic.BaseModel used during tests."""

        def __init__(self, **data: object) -> None:
            annotations = getattr(self, "__annotations__", {})
            for key in annotations:
                setattr(self, key, data.get(key))
            for key, value in data.items():
                if not hasattr(self, key):
                    setattr(self, key, value)

        def model_dump(self) -> dict[str, object]:
            annotations = getattr(self, "__annotations__", {})
            return {key: getattr(self, key, None) for key in annotations}

from packages.db import EventStore
from packages.odoo_client import OdooClient, OdooClientError
from services.docs import MarkdownLabelGenerator
from services.integration.odoo_service import OdooService
from services.recall import QuarantinedItem, RecallResult, RecallService
from services.simulator.events import EventWriter
from services.simulator.inventory import InventoryRepository

from .data import calculate_at_risk, load_flagged_decisions, load_recent_events, serialize_at_risk, serialize_events, serialize_inventory_events

EventsPathProvider = Callable[[], Path]
RepositoryFactory = Callable[[], Optional[InventoryRepository]]
OdooClientProvider = Callable[[], Optional[OdooClient]]
EventStoreProvider = Callable[[], EventStore]
LabelsPathProvider = Callable[[], Path]
RecallServiceFactory = Callable[[], Optional[RecallService]]
FlaggedPathProvider = Callable[[], Path]

_DURATION_RE = re.compile(r"^(?P<value>\d+)(?P<unit>[dhm])$")


class RecallTriggerPayload(BaseModel):
    codes: Optional[Sequence[str]] = None
    categories: Optional[Sequence[str]] = None


def create_app(
    *,
    events_path_provider: EventsPathProvider | None = None,
    repository_factory: RepositoryFactory | None = None,
    odoo_client_provider: OdooClientProvider | None = None,
    event_store_provider: EventStoreProvider | None = None,
    logger: logging.Logger | None = None,
    labels_path_provider: LabelsPathProvider | None = None,
    recall_service_factory: RecallServiceFactory | None = None,
    flagged_path_provider: FlaggedPathProvider | None = None,
) -> FastAPI:
    """Construct the FastAPI application."""

    app_logger = logger or logging.getLogger("foodflow.web")
    events_provider = events_path_provider or _default_events_path
    repository_provider = repository_factory or _default_repository_factory
    odoo_provider = odoo_client_provider or _default_odoo_client
    store_provider = event_store_provider or _default_event_store
    labels_provider = labels_path_provider or _default_labels_path
    recall_provider = recall_service_factory
    flagged_provider = flagged_path_provider or _default_flagged_path

    def _build_recall_service() -> Optional[RecallService]:
        nonlocal recall_provider
        if recall_provider is not None:
            return recall_provider()
        client = odoo_provider()
        if client is None:
            return None
        try:
            store = store_provider()
        except Exception:
            app_logger.exception("Failed to initialize event store for recall operations")
            store = None
        events_path = events_provider()
        writer = EventWriter(events_path, store=store)
        return RecallService(client, writer)

    app = FastAPI(title="FoodFlow Reporting API")

    @app.exception_handler(Exception)
    def _handle_unexpected(exc: Exception) -> JSONResponse:
        app_logger.error("Unhandled application error", exc_info=True)
        return JSONResponse(
            {"error": "internal", "detail": "see server logs"},
            status_code=500,
        )

    @app.get("/", response_class=JSONResponse)
    def index() -> dict[str, object]:
        return {
            "app": "FoodFlow reporting API",
            "status": "ok",
            "links": {
                "health": "/health",
                "events_recent": "/events/recent",
                "events": "/events",
                "metrics_summary": "/metrics/summary",
                "at_risk": "/at-risk",
                "flagged": "/flagged",
                "dashboard_flagged": "/dashboard/flagged",
                "labels_markdown": "/labels/markdown",
                "labels_index": "/out/labels/",
                "recall_trigger": "/recall/trigger",
                "recall_quarantined": "/recall/quarantined",
            },
            "docs": "See README.md for curl examples and Make targets.",
        }

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

    @app.get("/flagged", response_class=JSONResponse)
    def flagged(
        store: str | None = Query(None),
        category: str | None = Query(None),
        reason: str | None = Query(None),
    ) -> dict[str, object]:
        flagged_path = flagged_provider()
        exists = flagged_path.exists()
        meta: dict[str, object] = {
            "source": str(flagged_path),
            "exists": exists,
            "active_filters": {
                "store": (store or "").strip(),
                "category": (category or "").strip(),
                "reason": (reason or "").strip(),
            },
        }
        try:
            records = load_flagged_decisions(flagged_path)
        except ValueError:
            meta["error"] = "invalid_flagged_json"
            return {"items": [], "meta": meta}
        except OSError:
            meta["error"] = "flagged_read_failed"
            return {"items": [], "meta": meta}

        stores_set: set[str] = set()
        categories_set: set[str] = set()
        reasons_set: set[str] = set()
        for record in records:
            store_value = record.get("store")
            if isinstance(store_value, str) and store_value.strip():
                stores_set.add(store_value.strip())
            store_list = record.get("stores")
            if isinstance(store_list, Sequence):
                for entry in store_list:
                    if isinstance(entry, str) and entry.strip():
                        stores_set.add(entry.strip())
            category_value = record.get("category")
            if isinstance(category_value, str) and category_value.strip():
                categories_set.add(category_value.strip())
            reason_value = record.get("reason")
            if isinstance(reason_value, str) and reason_value.strip():
                reasons_set.add(reason_value.strip())

        store_filter = (store or "").strip()
        category_filter = (category or "").strip()
        reason_filter = (reason or "").strip()

        def _matches(entry: dict[str, object]) -> bool:
            if store_filter:
                entry_store = entry.get("store")
                entry_stores = entry.get("stores")
                matches_primary = isinstance(entry_store, str) and entry_store == store_filter
                matches_secondary = (
                    isinstance(entry_stores, Sequence) and any((isinstance(item, str) and item == store_filter) for item in entry_stores)
                )
                if not (matches_primary or matches_secondary):
                    return False
            if category_filter:
                category_value = entry.get("category")
                if not (isinstance(category_value, str) and category_value == category_filter):
                    return False
            if reason_filter:
                reason_value = entry.get("reason")
                if not (isinstance(reason_value, str) and reason_value == reason_filter):
                    return False
            return True

        filtered = [entry for entry in records if _matches(entry)]
        filtered.sort(
            key=lambda item: (
                str(item.get("store") or ""),
                str(item.get("reason") or ""),
                str(item.get("default_code") or ""),
            )
        )

        meta["total"] = len(records)
        meta["count"] = len(filtered)
        meta["filters"] = {
            "stores": sorted(stores_set),
            "categories": sorted(categories_set),
            "reasons": sorted(reasons_set),
        }
        return {"items": filtered, "meta": meta}

    @app.get("/dashboard/flagged", response_class=HTMLResponse)
    def dashboard_flagged() -> HTMLResponse:
        html = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Flagged Decisions Dashboard</title>
  <style>
    :root { color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { margin: 0; padding: 1.5rem; background: #f7f7f7; color: #111; }
    header { margin-bottom: 1rem; }
    h1 { font-size: 1.6rem; margin: 0 0 0.25rem 0; }
    p.subtitle { margin: 0; color: #555; }
    .controls { display: flex; flex-wrap: wrap; gap: 0.75rem; margin-bottom: 1rem; align-items: flex-end; }
    .controls label { display: flex; flex-direction: column; font-size: 0.9rem; gap: 0.25rem; min-width: 12rem; }
    select, button { padding: 0.4rem 0.6rem; font-size: 0.95rem; }
    button.primary { background: #2563eb; border: none; color: #fff; border-radius: 4px; cursor: pointer; }
    button.secondary { background: #e5e7eb; border: none; color: #111; border-radius: 4px; cursor: pointer; }
    button:disabled { opacity: 0.6; cursor: not-allowed; }
    table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 6px; overflow: hidden; }
    th, td { padding: 0.65rem; text-align: left; border-bottom: 1px solid #e5e7eb; vertical-align: top; font-size: 0.9rem; }
    th { background: #f3f4f6; text-transform: uppercase; letter-spacing: 0.05em; font-size: 0.75rem; }
    tbody tr:hover { background: #f5faff; }
    .pill { display: inline-flex; align-items: center; padding: 0.15rem 0.4rem; border-radius: 999px; font-size: 0.75rem; background: #e0f2fe; color: #0369a1; }
    #status { margin-top: 1rem; min-height: 1.5rem; font-size: 0.95rem; }
    #status.error { color: #b91c1c; }
    #status.success { color: #0f766e; }
    #status.info { color: #2563eb; }
    @media (prefers-color-scheme: dark) {
      body { background: #0f172a; color: #e2e8f0; }
      table { background: #1e293b; }
      th { background: #0f172a; }
      tbody tr:hover { background: #1e293b; }
      select, button.primary, button.secondary { color: inherit; }
      button.secondary { background: #334155; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Flagged Decisions Dashboard</h1>
    <p class="subtitle">Review flagged inventory, filter by store, category, or reason, and print labels in bulk.</p>
  </header>
  <section class="controls">
    <label>Store
      <select id="filter-store">
        <option value="">All stores</option>
      </select>
    </label>
    <label>Category
      <select id="filter-category">
        <option value="">All categories</option>
      </select>
    </label>
    <label>Reason
      <select id="filter-reason">
        <option value="">All reasons</option>
      </select>
    </label>
    <div class="actions">
      <button class="secondary" id="refresh-btn" type="button">Refresh</button>
      <button class="primary" id="print-btn" type="button">Print Labels</button>
    </div>
  </section>
  <section>
    <table aria-describedby="status">
      <thead>
        <tr>
          <th scope="col"></th>
          <th scope="col">Code</th>
          <th scope="col">Product</th>
          <th scope="col">Reason</th>
          <th scope="col">Store(s)</th>
          <th scope="col">Category</th>
          <th scope="col">Qty</th>
          <th scope="col">Outcome</th>
          <th scope="col">Notes</th>
        </tr>
      </thead>
      <tbody id="flagged-tbody">
        <tr><td colspan="9">Loading flagged items…</td></tr>
      </tbody>
    </table>
  </section>
  <div id="status" role="status" aria-live="polite"></div>
  <script>
    const state = {
      filters: { store: "", category: "", reason: "" },
      data: [],
      meta: {}
    };

    async function loadFlagged() {
      const params = new URLSearchParams();
      for (const [key, value] of Object.entries(state.filters)) {
        if (value) params.append(key, value);
      }
      setStatus("Loading flagged decisions…", "info");
      try {
        const response = await fetch("/flagged" + (params.toString() ? `?${params}` : ""));
        if (!response.ok) {
          throw new Error(`Request failed with status ${response.status}`);
        }
        const payload = await response.json();
        state.data = Array.isArray(payload.items) ? payload.items : [];
        state.meta = payload.meta || {};
        populateFilters(state.meta.filters || {});
        renderTable();
        const count = state.meta.count ?? state.data.length;
        setStatus(`Showing ${count} flagged item${count === 1 ? "" : "s"}.`, "success");
      } catch (error) {
        console.error(error);
        setStatus("Failed to load flagged decisions. See console for details.", "error");
      }
    }

    function populateFilters(filters) {
      populateSelect(document.getElementById("filter-store"), filters.stores || []);
      populateSelect(document.getElementById("filter-category"), filters.categories || []);
      populateSelect(document.getElementById("filter-reason"), filters.reasons || []);
    }

    function populateSelect(element, values) {
      if (!element) return;
      const current = element.value;
      element.innerHTML = "";
      const defaultOption = document.createElement("option");
      defaultOption.value = "";
      defaultOption.textContent = element.id === "filter-store" ? "All stores" :
        element.id === "filter-category" ? "All categories" : "All reasons";
      element.appendChild(defaultOption);
      for (const value of values) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        element.appendChild(option);
      }
      element.value = current;
    }

    function renderTable() {
      const tbody = document.getElementById("flagged-tbody");
      if (!tbody) return;
      tbody.innerHTML = "";
      if (!state.data.length) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = 9;
        cell.textContent = "No flagged decisions match the selected filters.";
        row.appendChild(cell);
        tbody.appendChild(row);
        return;
      }

      for (const entry of state.data) {
        const row = document.createElement("tr");
        const checkboxCell = document.createElement("td");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = entry.default_code || "";
        checkbox.dataset.code = entry.default_code || "";
        if (!entry.default_code) {
          checkbox.disabled = true;
          checkbox.title = "No default code available for label printing.";
        }
        checkboxCell.appendChild(checkbox);
        row.appendChild(checkboxCell);

        row.appendChild(createCell(entry.default_code || "—"));
        row.appendChild(createCell(entry.product || "—"));

        const reasonCell = document.createElement("td");
        const reasonPill = document.createElement("span");
        reasonPill.className = "pill";
        reasonPill.textContent = entry.reason || "—";
        reasonCell.appendChild(reasonPill);
        row.appendChild(reasonCell);

        const stores = Array.isArray(entry.stores) ? entry.stores.join(", ") : (entry.store || "—");
        row.appendChild(createCell(stores || "—"));
        row.appendChild(createCell(entry.category || "—"));

        const qtyCell = document.createElement("td");
        if (entry.quantity !== undefined && entry.quantity !== null && !Number.isNaN(Number(entry.quantity))) {
          qtyCell.textContent = Number(entry.quantity).toFixed(2);
        } else {
          qtyCell.textContent = "—";
        }
        row.appendChild(qtyCell);

        row.appendChild(createCell(entry.outcome || "—"));
        row.appendChild(createCell(entry.notes || "—"));

        tbody.appendChild(row);
      }
    }

    function createCell(value) {
      const cell = document.createElement("td");
      cell.textContent = typeof value === "string" ? value : value ?? "—";
      return cell;
    }

    function setStatus(message, tone) {
      const status = document.getElementById("status");
      if (!status) return;
      status.className = tone || "";
      status.textContent = message;
    }

    function gatherSelectedCodes() {
      const checkboxes = document.querySelectorAll("#flagged-tbody input[type='checkbox']:checked");
      const codes = [];
      for (const checkbox of checkboxes) {
        const code = checkbox.dataset.code || "";
        if (code && !codes.includes(code)) {
          codes.push(code);
        }
      }
      return codes;
    }

    async function printLabels() {
      const codes = gatherSelectedCodes();
      if (!codes.length) {
        setStatus("Select at least one item with a default code to print labels.", "info");
        return;
      }
      setStatus(`Generating labels for ${codes.length} item${codes.length === 1 ? "" : "s"}…`, "info");
      try {
        const response = await fetch("/labels/markdown", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ default_codes: codes })
        });
        const payload = await response.json();
        if (!response.ok) {
          const detail = payload && payload.detail;
          const message = typeof detail === "string" ? detail : (detail && detail.default_codes) || "Label generation failed.";
          throw new Error(message);
        }
        const count = payload.meta && typeof payload.meta.count === "number" ? payload.meta.count : codes.length;
        const generated = payload.links ? Object.keys(payload.links).join(", ") : "";
        const message = generated
          ? `Generated ${count} PDF label${count === 1 ? "" : "s"} for ${generated}.`
          : `Generated ${count} PDF label${count === 1 ? "" : "s"}.`;
        setStatus(message, "success");
      } catch (error) {
        console.error(error);
        setStatus(error instanceof Error ? error.message : "Label generation failed.", "error");
      }
    }

    document.getElementById("filter-store")?.addEventListener("change", (event) => {
      state.filters.store = (event.target.value || "").trim();
      loadFlagged();
    });
    document.getElementById("filter-category")?.addEventListener("change", (event) => {
      state.filters.category = (event.target.value || "").trim();
      loadFlagged();
    });
    document.getElementById("filter-reason")?.addEventListener("change", (event) => {
      state.filters.reason = (event.target.value || "").trim();
      loadFlagged();
    });
    document.getElementById("refresh-btn")?.addEventListener("click", loadFlagged);
    document.getElementById("print-btn")?.addEventListener("click", printLabels);

    loadFlagged();
  </script>
</body>
</html>
        """.strip()
        return HTMLResponse(content=html)

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

    @app.post("/recall/trigger", response_class=JSONResponse)
    def recall_trigger(payload: RecallTriggerPayload = Body(...)) -> dict[str, object]:
        if isinstance(payload, dict):  # Compatibility with lightweight FastAPI stub
            payload = RecallTriggerPayload(**payload)
        service = _build_recall_service()
        if service is None:
            raise HTTPException(503, {"error": "odoo_unreachable"})
        codes = list(payload.codes or [])
        categories = list(payload.categories or [])
        try:
            results = service.recall(default_codes=codes, categories=categories)
        except ValueError as exc:
            raise HTTPException(400, {"detail": str(exc)}) from exc
        except OdooClientError:
            app_logger.exception("Failed to run recall due to Odoo error")
            raise HTTPException(503, {"error": "odoo_unreachable"}) from None
        except Exception:
            app_logger.exception("Failed to quarantine inventory for recall")
            raise HTTPException(500, {"error": "recall_failed"}) from None

        items = [_serialize_recall_result(result) for result in results]
        meta: dict[str, object] = {
            "requested_codes": codes,
            "requested_categories": categories,
            "count": len(items),
        }
        return {"items": items, "meta": meta}

    @app.get("/recall/quarantined", response_class=JSONResponse)
    def recall_quarantined() -> dict[str, object]:
        service = _build_recall_service()
        if service is None:
            return {"items": [], "meta": {"error": "odoo_unreachable"}}
        try:
            items = service.list_quarantined()
        except OdooClientError:
            app_logger.exception("Failed to query quarantined inventory in Odoo")
            return {"items": [], "meta": {"error": "odoo_unreachable"}}
        except Exception:
            app_logger.exception("Failed to list quarantined inventory")
            return {"items": [], "meta": {"error": "recall_query_failed"}}
        payload = [_serialize_quarantine_item(item) for item in items]
        return {"items": payload, "meta": {"count": len(payload)}}

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
        service = OdooService(logger=logger.getChild("integration"))
        return service.client()
    except OdooClientError:
        logger.exception("Failed to authenticate default Odoo client")
        return None
    except Exception:
        logger.exception("Unexpected error creating default Odoo client")
        return None


def _default_repository_factory() -> InventoryRepository | None:
    logger = logging.getLogger("foodflow.web")
    service = OdooService(logger=logger.getChild("integration"))
    try:
        return service.inventory_repository()
    except OdooClientError:
        logger.exception("Failed to authenticate default Odoo client")
        return None
    except Exception:
        logger.exception("Unexpected error creating default inventory repository")
        return None


def _default_labels_path() -> Path:
    return Path("out/labels")


def _default_flagged_path() -> Path:
    from .data import DEFAULT_FLAGGED_PATH

    return DEFAULT_FLAGGED_PATH


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


def _serialize_recall_result(result: RecallResult) -> dict[str, object]:
    return {
        "product": result.product,
        "default_code": result.default_code,
        "lot": result.lot,
        "quantity": result.quantity,
        "source_location": result.source_location,
        "destination_location": result.destination_location,
    }


def _serialize_quarantine_item(item: QuarantinedItem) -> dict[str, object]:
    return {
        "product": item.product,
        "default_code": item.default_code,
        "lot": item.lot,
        "quantity": item.quantity,
    }


__all__ = ["create_app"]
