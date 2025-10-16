"""FastAPI application exposing resilient reporting endpoints."""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Sequence, Tuple

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from jsonschema import Draft202012Validator
from sqlalchemy import select

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

from packages.db import ComplianceEvent, EventStore, compliance_session, create_all
from packages.odoo_client import OdooClient, OdooClientError
from services.docs import MarkdownLabelGenerator
from services.compliance import CSV_HEADERS as COMPLIANCE_CSV_HEADERS, resolve_csv_path, serialize_event
from services.integration.enricher import enrich_decisions
from services.integration.odoo_service import OdooService
from services.recall import QuarantinedItem, RecallResult, RecallService
from services.simulator.events import EventWriter
from services.simulator.inventory import InventoryRepository

from .data import (
    calculate_at_risk,
    calculate_impact_metrics,
    load_flagged_decisions,
    load_recent_events,
    append_weight_metadata,
    serialize_at_risk,
    serialize_events,
    serialize_inventory_events,
)

EventsPathProvider = Callable[[], Path]
RepositoryFactory = Callable[[], Optional[InventoryRepository]]
OdooClientProvider = Callable[[], Optional[OdooClient]]
EventStoreProvider = Callable[[], EventStore]
LabelsPathProvider = Callable[[], Path]
RecallServiceFactory = Callable[[], Optional[RecallService]]
FlaggedPathProvider = Callable[[], Path]

_DURATION_RE = re.compile(r"^(?P<value>\d+)(?P<unit>[dhm])$")
FLAGGED_CSV_HEADERS: tuple[str, ...] = (
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
)
EVENTS_CSV_HEADERS: tuple[str, ...] = (
    "timestamp",
    "type",
    "product",
    "lot",
    "quantity",
    "before_quantity",
    "after_quantity",
    "source",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPLIANCE_SCHEMA_PATH = REPO_ROOT / "contracts" / "schemas" / "compliance.schema.json"
try:
    _COMPLIANCE_SCHEMA = json.loads(COMPLIANCE_SCHEMA_PATH.read_text(encoding="utf-8"))
    _COMPLIANCE_VALIDATOR = Draft202012Validator(_COMPLIANCE_SCHEMA)
except Exception:
    _COMPLIANCE_SCHEMA = None
    _COMPLIANCE_VALIDATOR = None


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

    assets_root = _resolve_repo_path(Path("out"))
    try:
        assets_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        app_logger.exception("Failed to ensure static asset directory %s", assets_root)

    api_key_env = os.getenv("FOODFLOW_WEB_API_KEY") or os.getenv("FOODFLOW_API_KEY")

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
    app.mount("/static", StaticFiles(directory=str(assets_root), html=True), name="static")

    def _labels_root() -> Path:
        return _resolve_repo_path(labels_provider()).resolve()

    def _static_label_url(path: Path, *, labels_root: Path) -> str:
        try:
            relative = path.resolve().relative_to(labels_root)
        except ValueError:
            return "/static/" + path.name
        return "/static/labels/" + relative.as_posix()

    def _refresh_labels_static_index(directory: Path) -> None:
        labels_root = _resolve_repo_path(directory).resolve()
        try:
            labels_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            app_logger.exception("Failed to ensure labels directory %s", labels_root)
            return
        try:
            pdf_files = sorted(
                entry for entry in labels_root.iterdir() if entry.is_file() and entry.suffix.lower() == ".pdf"
            )
        except OSError:
            app_logger.exception("Failed to list labels directory %s", labels_root)
            return
        index_path = labels_root / "index.html"
        if not pdf_files:
            if index_path.exists():
                try:
                    index_path.unlink()
                except OSError:
                    app_logger.debug("Unable to remove empty labels index at %s", index_path)
            return
        lines = [
            "<!DOCTYPE html>",
            "<html>",
            "  <head>",
            '    <meta charset="utf-8" />',
            "    <title>FoodFlow Labels</title>",
            "    <style>",
            "      body { font-family: Helvetica, Arial, sans-serif; padding: 24px; }",
            "      h1 { margin-bottom: 16px; font-size: 20px; }",
            "      ul { list-style: none; padding: 0; }",
            "      li { margin-bottom: 8px; }",
            "      a { color: #2563eb; text-decoration: none; }",
            "      a:hover { text-decoration: underline; }",
            "    </style>",
            "  </head>",
            "  <body>",
            "    <h1>Generated Labels</h1>",
            "    <ul>",
        ]
        for entry in pdf_files:
            url = _static_label_url(entry, labels_root=labels_root)
            lines.append(
                f'      <li><a href="{html.escape(url, quote=True)}">{html.escape(entry.name)}</a></li>'
            )
        lines.extend(
            [
                "    </ul>",
                "  </body>",
                "</html>",
            ]
        )
        try:
            index_path.write_text("\n".join(lines), encoding="utf-8")
        except OSError:
            app_logger.exception("Failed to write labels index at %s", index_path)

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
                "metrics_last_sync": "/metrics/last_sync",
                "metrics_impact": "/metrics/impact",
                "at_risk": "/at-risk",
                "flagged": "/flagged",
                "dashboard_flagged": "/dashboard/flagged",
                "dashboard_at_risk": "/dashboard/at-risk",
                "labels_markdown": "/labels/markdown",
                "labels_index": "/out/labels/",
                "recall_trigger": "/recall/trigger",
                "recall_quarantined": "/recall/quarantined",
                "flagged_export": "/export/flagged.csv",
                "events_export": "/export/events.csv",
                "compliance_events": "/compliance/events",
                "compliance_export": "/compliance/export.csv",
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

        try:
            client = odoo_provider()
        except Exception:
            app_logger.exception("Failed to initialize Odoo client for enrichment")
            client = None
        try:
            records = enrich_decisions(records, client=client, allow_remote=client is not None)
        except Exception:
            app_logger.exception("Failed to enrich flagged decisions for /flagged endpoint")

        append_weight_metadata(records)

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
        meta["estimated_weight_lbs"] = round(
            sum(
                float(entry.get("estimated_weight_lbs") or 0.0)
                for entry in filtered
            ),
            2,
        )
        meta["filters"] = {
            "stores": sorted(stores_set),
            "categories": sorted(categories_set),
            "reasons": sorted(reasons_set),
        }
        return {"items": filtered, "meta": meta}

    def _require_api_key(candidate: str | None) -> None:
        if not api_key_env:
            return
        if candidate == api_key_env:
            return
        raise HTTPException(401, {"error": "unauthorized"})

    def _empty_impact() -> dict[str, float]:
        return {
            "diverted_value_usd": 0.0,
            "donated_weight_lbs": 0.0,
            "markdown_count": 0,
            "donation_count": 0,
        }

    @app.get("/metrics/impact", response_class=JSONResponse)
    def metrics_impact() -> dict[str, object]:
        flagged_path = flagged_provider()
        exists = flagged_path.exists()
        impact = _empty_impact()
        meta: dict[str, object] = {
            "source": str(flagged_path),
            "exists": exists,
        }
        if not exists:
            return {"impact": impact, "meta": meta}
        try:
            records = load_flagged_decisions(flagged_path)
        except ValueError:
            meta["error"] = "invalid_flagged_json"
            return {"impact": impact, "meta": meta}
        except OSError:
            meta["error"] = "flagged_read_failed"
            return {"impact": impact, "meta": meta}
        meta["count"] = len(records)
        impact_data = calculate_impact_metrics(records)
        meta["markdown_count"] = impact_data.get("markdown_count", 0)
        meta["donation_count"] = impact_data.get("donation_count", 0)
        return {"impact": impact_data, "meta": meta}

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
    header { margin-bottom: 1.5rem; }
    h1 { font-size: 1.6rem; margin: 0 0 0.25rem 0; }
    p.subtitle { margin: 0; color: #555; }
    .sync-banner { display: none; margin-top: 0.75rem; padding: 0.75rem 1rem; border-radius: 6px; border: 1px solid #fecaca; background: #fef2f2; color: #991b1b; font-size: 0.95rem; }
    .sync-banner.visible { display: block; }
    .sync-banner.info { border-color: #bfdbfe; background: #eff6ff; color: #1d4ed8; }
    .overview { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }
    .card { background: #fff; border-radius: 8px; padding: 1rem; box-shadow: 0 16px 24px rgba(15, 23, 42, 0.08); }
    .card h2 { margin: 0; font-size: 1rem; letter-spacing: 0.02em; text-transform: uppercase; color: #475569; }
    .metric { font-size: 1.8rem; font-weight: 600; margin: 0.35rem 0; color: #111827; }
    .metric-caption { margin: 0; color: #4b5563; font-size: 0.9rem; }
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
    #status {
      margin: 0;
      min-height: 1.5rem;
      font-size: 0.95rem;
      padding: 0.75rem 1rem;
      border-radius: 8px;
      background: #f8fafc;
      color: #1f2937;
      border: 1px solid transparent;
      display: inline-flex;
      flex-wrap: wrap;
      gap: 0.5rem;
      align-items: center;
    }
    #status.hidden { display: none; }
    #status.error { border-color: #b91c1c; background: #fef2f2; color: #b91c1c; }
    #status.success { border-color: #0f766e; background: #ecfdf5; color: #0f766e; }
    #status.info { border-color: #2563eb; background: #eff6ff; color: #1d4ed8; }
    #status a { color: inherit; text-decoration: underline; }
    #status .status-links { display: inline-flex; flex-wrap: wrap; gap: 0.5rem; }
    #status .status-view-all { margin-left: 0.5rem; font-size: 0.85rem; opacity: 0.8; }
    @media (prefers-color-scheme: dark) {
      body { background: #0f172a; color: #e2e8f0; }
      table { background: #1e293b; }
      th { background: #0f172a; }
      tbody tr:hover { background: #1e293b; }
      select, button.primary, button.secondary { color: inherit; }
      button.secondary { background: #334155; }
      .card { background: #1e293b; box-shadow: none; }
      .card h2 { color: #cbd5f5; }
      .metric { color: #f8fafc; }
      .metric-caption { color: #94a3b8; }
      .sync-banner { background: rgba(127, 29, 29, 0.35); color: #fecaca; border-color: rgba(185, 28, 28, 0.7); }
      .sync-banner.info { background: rgba(30, 64, 175, 0.35); color: #dbeafe; border-color: rgba(59, 130, 246, 0.6); }
    }
  </style>
</head>
<body>
  <header>
    <h1>Flagged Decisions Dashboard</h1>
    <p class="subtitle">Review flagged inventory, monitor impact, filter by store, category, or reason, and print labels in bulk. Need to triage expiring lots? Visit the <a href="/dashboard/at-risk">At-Risk Inventory dashboard</a>.</p>
    <div id="sync-banner" class="sync-banner" role="status" aria-live="polite"></div>
  </header>
  <section class="overview" aria-label="Impact overview">
    <article class="card">
      <h2>Waste Diverted</h2>
      <p class="metric" id="impact-diverted">$0</p>
      <p class="metric-caption">Estimated retail value preserved via markdowns</p>
    </article>
    <article class="card">
      <h2>Estimated Weight</h2>
      <p class="metric" id="impact-weight">0 lbs</p>
      <p class="metric-caption">Pounds represented by current filters</p>
    </article>
    <article class="card">
      <h2>Donations</h2>
      <p class="metric" id="impact-donated">0 lbs</p>
      <p class="metric-caption">Estimated pounds redirected through donations</p>
    </article>
  </section>
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
      <button class="primary" id="print-btn" type="button" disabled>Print Labels</button>
      <div id="status" role="status" aria-live="polite" class="hidden"></div>
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
          <th scope="col">Weight (lbs)</th>
          <th scope="col">Outcome</th>
          <th scope="col">Notes</th>
        </tr>
      </thead>
      <tbody id="flagged-tbody">
        <tr><td colspan="10">Loading flagged items…</td></tr>
      </tbody>
    </table>
  </section>
  <script>
    const state = {
      filters: { store: "", category: "", reason: "" },
      data: [],
      meta: {},
      impact: { diverted_value_usd: 0, donated_weight_lbs: 0 },
      lastSyncIso: null,
      lastSyncMeta: {},
      lastSyncError: false
    };

    const SYNC_STALE_MINUTES = 30;

    function renderSyncBanner() {
      const banner = document.getElementById("sync-banner");
      if (!banner) return;
      banner.className = "sync-banner";

      if (state.lastSyncError) {
        banner.textContent = "Unable to load the latest integration sync time.";
        banner.classList.add("visible");
        return;
      }

      const iso = typeof state.lastSyncIso === "string" ? state.lastSyncIso : null;
      const metaStatus = state.lastSyncMeta ? state.lastSyncMeta.status : null;

      if (!iso) {
        if (metaStatus === "not_recorded") {
          banner.textContent = "No integration sync has been recorded yet.";
          banner.classList.add("visible", "info");
        }
        return;
      }

      const parsed = new Date(iso);
      if (Number.isNaN(parsed.getTime())) {
        return;
      }
      const diffMs = Date.now() - parsed.getTime();
      if (diffMs < 0) {
        return;
      }
      const diffMinutes = Math.round(diffMs / 60000);
      if (diffMinutes <= SYNC_STALE_MINUTES) {
        return;
      }
      const minutesText = diffMinutes === 1 ? "1 minute" : `${diffMinutes} minutes`;
      banner.textContent = `Last sync ${minutesText} ago`;
      banner.classList.add("visible");
    }

    async function loadLastSync() {
      try {
        const response = await fetch("/metrics/last_sync");
        if (!response.ok) {
          throw new Error(`Request failed with status ${response.status}`);
        }
        const payload = await response.json();
        state.lastSyncIso = payload && typeof payload.last_sync === "string" ? payload.last_sync : null;
        state.lastSyncMeta = payload && typeof payload === "object" && payload.meta ? payload.meta : {};
        state.lastSyncError = false;
      } catch (error) {
        console.error(error);
        state.lastSyncIso = null;
        state.lastSyncMeta = {};
        state.lastSyncError = true;
      }
      renderSyncBanner();
    }

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
        state.meta = payload.meta && typeof payload.meta === "object" ? payload.meta : {};
        populateFilters(state.meta.filters || {});
        renderTable();
        const count = state.meta.count ?? state.data.length;
        const weightTotal = Number(state.meta.estimated_weight_lbs || 0);
        const weightText = weightTotal > 0 ? ` (≈${weightTotal.toFixed(2)} lbs)` : "";
        setStatus(`Showing ${count} flagged item${count === 1 ? "" : "s"}${weightText}.`, "success");
        renderImpact();
      } catch (error) {
        console.error(error);
        setStatus("Failed to load flagged decisions. See console for details.", "error");
      }
    }

    async function loadImpact() {
      try {
        const response = await fetch("/metrics/impact");
        if (!response.ok) {
          throw new Error(`Request failed with status ${response.status}`);
        }
        const payload = await response.json();
        const impact = payload && typeof payload === "object" ? payload.impact : null;
        if (impact && typeof impact === "object") {
          state.impact = impact;
        } else {
          state.impact = { diverted_value_usd: 0, donated_weight_lbs: 0 };
        }
      } catch (error) {
        console.error(error);
        state.impact = { diverted_value_usd: 0, donated_weight_lbs: 0 };
      }
      renderImpact();
    }

    function renderImpact() {
      const divertedElement = document.getElementById("impact-diverted");
      const flaggedWeightElement = document.getElementById("impact-weight");
      const donatedElement = document.getElementById("impact-donated");
      if (divertedElement) {
        const amount = Number(state.impact.diverted_value_usd || 0);
        const decimals = amount < 1000 ? 2 : 0;
        const formatter = new Intl.NumberFormat("en-US", {
          style: "currency",
          currency: "USD",
          minimumFractionDigits: decimals,
          maximumFractionDigits: decimals
        });
        divertedElement.textContent = formatter.format(amount);
      }
      if (flaggedWeightElement) {
        const pounds = Number(state.meta && state.meta.estimated_weight_lbs ? state.meta.estimated_weight_lbs : 0);
        flaggedWeightElement.textContent = `${pounds.toFixed(1)} lbs`;
      }
      if (donatedElement) {
        const pounds = Number(state.impact.donated_weight_lbs || 0);
        donatedElement.textContent = `${pounds.toFixed(1)} lbs`;
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
        cell.colSpan = 10;
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
        const productName = entry.product_name || entry.product || "—";
        row.appendChild(createCell(productName));

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
        const qtyValue = entry.qty ?? entry.quantity;
        if (qtyValue !== undefined && qtyValue !== null && !Number.isNaN(Number(qtyValue))) {
          qtyCell.textContent = Number(qtyValue).toFixed(2);
        } else {
          qtyCell.textContent = "—";
        }
        row.appendChild(qtyCell);

        const weightCell = document.createElement("td");
        const weightValue = entry.estimated_weight_lbs;
        if (weightValue !== undefined && weightValue !== null && !Number.isNaN(Number(weightValue))) {
          weightCell.textContent = Number(weightValue).toFixed(2);
        } else {
          weightCell.textContent = "—";
        }
        row.appendChild(weightCell);

        row.appendChild(createCell(entry.outcome || "—"));
        row.appendChild(createCell(entry.notes || "—"));

        tbody.appendChild(row);
      }
      updatePrintButtonState();
    }

    function createCell(value) {
      const cell = document.createElement("td");
      cell.textContent = typeof value === "string" ? value : value ?? "—";
      return cell;
    }

    function setStatus(message, tone, options) {
      const status = document.getElementById("status");
      if (!status) return;
      const opts = options && typeof options === "object" ? options : {};
      const links = Array.isArray(opts.links) ? opts.links : [];
      status.className = tone ? tone : "";
      status.innerHTML = "";
      if (message) {
        const text = document.createElement("span");
        text.textContent = message;
        status.appendChild(text);
      }
      if (links.length) {
        const linkGroup = document.createElement("span");
        linkGroup.className = "status-links";
        links.forEach((link, index) => {
          if (!link || typeof link.href !== "string") {
            return;
          }
          const anchor = document.createElement("a");
          anchor.href = link.href;
          anchor.target = "_blank";
          anchor.rel = "noopener";
          anchor.textContent = link.label || link.href;
          linkGroup.appendChild(anchor);
          if (index < links.length - 1) {
            linkGroup.appendChild(document.createTextNode(" • "));
          }
        });
        if (linkGroup.childNodes.length) {
          status.appendChild(linkGroup);
        }
      }
      if (opts.trailingLink && typeof opts.trailingLink.href === "string") {
        const viewAll = document.createElement("a");
        viewAll.href = opts.trailingLink.href;
        viewAll.target = "_blank";
        viewAll.rel = "noopener";
        viewAll.className = "status-view-all";
        viewAll.textContent = opts.trailingLink.label || opts.trailingLink.href;
        status.appendChild(viewAll);
      }
      const shouldHide = !message && !links.length && !opts.trailingLink;
      status.classList.toggle("hidden", shouldHide);
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

    function updatePrintButtonState() {
      const button = document.getElementById("print-btn");
      if (!button) return;
      button.disabled = gatherSelectedCodes().length === 0;
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
        if (payload && typeof payload.error === "string") {
          const code = payload.error;
          if (code === "odoo_unreachable") {
            throw new Error("Labels are unavailable right now; try again once Odoo is reachable.");
          }
          throw new Error("Label generation failed.");
        }
        const generated = Array.isArray(payload.generated) ? payload.generated : [];
        const count = typeof payload.count === "number" ? payload.count : generated.length || codes.length;
        const missing = Array.isArray(payload.missing) ? payload.missing : [];
        const links = generated
          .filter((entry) => entry && typeof entry.url === "string")
          .map((entry) => ({
            href: entry.url,
            label: entry.code || entry.url
          }));
        let message = `Generated ${count} PDF label${count === 1 ? "" : "s"}.`;
        if (missing.length) {
          message += ` Missing data for ${missing.join(", ")}.`;
        }
        setStatus(message, "success", {
          links,
          trailingLink: { href: "/static/labels/", label: "View all labels" }
        });
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
    document.getElementById("refresh-btn")?.addEventListener("click", () => {
      loadImpact();
      loadFlagged();
      loadLastSync();
    });
    document.getElementById("flagged-tbody")?.addEventListener("change", (event) => {
      if (event.target && event.target.matches("input[type='checkbox']")) {
        updatePrintButtonState();
      }
    });
    document.getElementById("print-btn")?.addEventListener("click", printLabels);

    loadImpact();
    loadFlagged();
    loadLastSync();
    updatePrintButtonState();
  </script>
</body>
</html>
        """.strip()
        return HTMLResponse(content=html)

    @app.get("/dashboard/at-risk", response_class=HTMLResponse)
    def dashboard_at_risk() -> HTMLResponse:
        html = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>At-Risk Inventory Dashboard</title>
  <style>
    :root { color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { margin: 0; padding: 1.75rem; background: #f8fafc; color: #0f172a; }
    header { margin-bottom: 1.5rem; }
    h1 { font-size: 1.75rem; margin: 0 0 0.5rem; }
    .subtitle { margin: 0; color: #475569; font-size: 0.95rem; line-height: 1.5; max-width: 72ch; }
    .subtitle a { color: inherit; text-decoration: underline; }
    nav.top-nav { margin-top: 1rem; font-size: 0.9rem; color: #64748b; }
    nav.top-nav a { color: inherit; text-decoration: none; margin-right: 1rem; }
    nav.top-nav a:hover { text-decoration: underline; }
    .status-banner { margin: 0 0 1.25rem; padding: 0.75rem 1rem; border-radius: 8px; border: 1px solid transparent; background: #e0f2fe; color: #0c4a6e; font-size: 0.95rem; display: none; }
    .status-banner.visible { display: block; }
    .status-banner.error { background: #fef2f2; border-color: #fecaca; color: #b91c1c; }
    .status-banner.success { background: #ecfdf5; border-color: #bbf7d0; color: #047857; }
    .status-banner.info { background: #eff6ff; border-color: #bfdbfe; color: #1d4ed8; }
    .status-banner.warning { background: #fffbeb; border-color: #fde68a; color: #b45309; }
    .overview { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }
    .card { background: #fff; border-radius: 10px; padding: 1rem; box-shadow: 0 20px 30px rgba(15, 23, 42, 0.08); }
    .card h2 { margin: 0; font-size: 0.95rem; letter-spacing: 0.04em; text-transform: uppercase; color: #64748b; }
    .metric { font-size: 2rem; font-weight: 600; margin: 0.35rem 0 0.2rem; color: #0f172a; }
    .metric-caption { margin: 0; color: #475569; font-size: 0.9rem; }
    .metric-footer { margin: 0.4rem 0 0; font-size: 0.78rem; color: #64748b; }
    .controls { display: flex; flex-wrap: wrap; gap: 1rem; margin-bottom: 1rem; align-items: flex-end; }
    .controls label { display: flex; flex-direction: column; font-size: 0.9rem; gap: 0.35rem; min-width: 10rem; color: #1f2937; }
    select, button { padding: 0.45rem 0.6rem; font-size: 0.95rem; border-radius: 6px; border: 1px solid #cbd5f5; background: #fff; color: inherit; }
    button.primary { background: #2563eb; border-color: #2563eb; color: #fff; cursor: pointer; }
    button.primary:disabled { opacity: 0.6; cursor: not-allowed; }
    .controls a { font-size: 0.9rem; color: #2563eb; text-decoration: none; }
    .controls a:hover { text-decoration: underline; }
    #status { min-height: 1.5rem; padding: 0.75rem 1rem; border-radius: 8px; background: #e2e8f0; color: #1e293b; border: 1px solid transparent; margin-bottom: 1rem; font-size: 0.95rem; display: none; }
    #status.visible { display: block; }
    #status.error { background: #fef2f2; border-color: #fecaca; color: #b91c1c; }
    #status.info { background: #eff6ff; border-color: #bfdbfe; color: #1d4ed8; }
    #status.success { background: #ecfdf5; border-color: #bbf7d0; color: #047857; }
    #status.warning { background: #fffbeb; border-color: #fde68a; color: #b45309; }
    table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 12px 18px rgba(15, 23, 42, 0.05); }
    th, td { padding: 0.75rem; text-align: left; border-bottom: 1px solid #e2e8f0; font-size: 0.92rem; vertical-align: top; }
    th { background: #f1f5f9; text-transform: uppercase; letter-spacing: 0.05em; font-size: 0.75rem; color: #475569; }
    tbody tr:hover { background: #f8fafc; }
    tbody tr.overdue { background: #fff1f2; }
    tbody tr.due-today { background: #fefce8; }
    .badge { display: inline-flex; align-items: center; padding: 0.15rem 0.45rem; border-radius: 999px; font-size: 0.75rem; background: #e0f2fe; color: #0369a1; font-weight: 600; }
    .badge.overdue { background: #fee2e2; color: #b91c1c; }
    .badge.due-today { background: #fef08a; color: #92400e; }
    .meta-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 0.75rem; margin-bottom: 1.25rem; }
    .meta-card { background: #fff; border-radius: 8px; padding: 0.75rem 1rem; border: 1px solid #e2e8f0; font-size: 0.88rem; color: #475569; }
    .meta-card strong { display: block; font-size: 0.78rem; letter-spacing: 0.04em; text-transform: uppercase; color: #1f2937; margin-bottom: 0.2rem; }
    footer { margin-top: 1.5rem; font-size: 0.8rem; color: #64748b; }
    @media (prefers-color-scheme: dark) {
      body { background: #0f172a; color: #e2e8f0; }
      .subtitle { color: #cbd5f5; }
      nav.top-nav a { color: #93c5fd; }
      .status-banner { background: rgba(14, 116, 144, 0.35); color: #bae6fd; border-color: rgba(14, 116, 144, 0.6); }
      .status-banner.error { background: rgba(127, 29, 29, 0.35); color: #fecaca; border-color: rgba(185, 28, 28, 0.7); }
      .status-banner.success { background: rgba(13, 148, 136, 0.35); color: #99f6e4; border-color: rgba(13, 148, 136, 0.6); }
      .status-banner.info { background: rgba(30, 64, 175, 0.35); color: #dbeafe; border-color: rgba(59, 130, 246, 0.6); }
      .status-banner.warning { background: rgba(146, 64, 14, 0.35); color: #fcd34d; border-color: rgba(217, 119, 6, 0.6); }
      .card, table, .meta-card { background: #1e293b; border-color: #334155; box-shadow: none; color: #e2e8f0; }
      .metric { color: #f8fafc; }
      .metric-caption { color: #cbd5f5; }
      .metric-footer { color: #a5b4fc; }
      th { background: #0f172a; color: #94a3b8; }
      tbody tr:hover { background: rgba(51, 65, 85, 0.45); }
      tbody tr.overdue { background: rgba(190, 18, 60, 0.25); }
      tbody tr.due-today { background: rgba(217, 119, 6, 0.25); }
      select, button { background: #1e293b; border-color: #475569; color: inherit; }
      .controls a { color: #93c5fd; }
      .controls label { color: #e2e8f0; }
      .meta-card { color: #e2e8f0; }
      .meta-card strong { color: #cbd5f5; }
      #meta-expiry-field,
      #meta-window,
      #meta-refreshed,
      #meta-source { color: #e2e8f0; }
      #status { background: #1e293b; border-color: #334155; color: #e2e8f0; }
      #status.info { background: rgba(30, 64, 175, 0.35); border-color: rgba(30, 64, 175, 0.55); color: #dbeafe; }
      #status.error { background: rgba(127, 29, 29, 0.35); border-color: rgba(127, 29, 29, 0.6); color: #fecaca; }
      #status.success { background: rgba(13, 148, 136, 0.35); border-color: rgba(13, 148, 136, 0.6); color: #99f6e4; }
      #status.warning { background: rgba(146, 64, 14, 0.35); border-color: rgba(146, 64, 14, 0.6); color: #fcd34d; }
    }
  </style>
</head>
<body>
  <header>
    <h1>At-Risk Inventory Dashboard</h1>
    <p class="subtitle">
      Monitor lots that are approaching expiry, triage overdue items, and share visibility with store teams.
      Looking for markdown or donation decisions? Visit the <a href="/dashboard/flagged">Flagged Decisions dashboard</a>.
    </p>
    <nav class="top-nav">
      <a href="/">API index</a>
      <a href="/dashboard/flagged">Flagged Decisions</a>
      <a href="/export/flagged.csv">Flagged CSV</a>
      <a href="/at-risk">Raw JSON</a>
    </nav>
  </header>

  <div id="sync-status" class="status-banner"></div>

  <section class="overview" aria-label="At-risk summary">
    <article class="card">
      <h2>Total At-Risk</h2>
      <p class="metric" id="metric-total">—</p>
      <p class="metric-caption">Lots within the selected window</p>
      <p class="metric-footer" id="metric-total-footer"></p>
    </article>
    <article class="card">
      <h2>Due Today</h2>
      <p class="metric" id="metric-due-today">—</p>
      <p class="metric-caption">Lots that expire before midnight</p>
      <p class="metric-footer" id="metric-due-today-footer"></p>
    </article>
    <article class="card">
      <h2>Overdue</h2>
      <p class="metric" id="metric-overdue">—</p>
      <p class="metric-caption">Lots past their expiry date</p>
      <p class="metric-footer" id="metric-overdue-footer"></p>
    </article>
    <article class="card">
      <h2>Upcoming</h2>
      <p class="metric" id="metric-upcoming">—</p>
      <p class="metric-caption">Lots expiring in the window</p>
      <p class="metric-footer" id="metric-upcoming-footer"></p>
    </article>
  </section>

  <section class="controls">
    <label for="filter-days">Expiry window
      <select id="filter-days" name="filter-days"></select>
    </label>
    <button class="primary" id="refresh-btn" type="button">Refresh</button>
    <a id="download-json" href="/at-risk" target="_blank" rel="noopener">Open JSON</a>
  </section>

  <div id="status" role="status" aria-live="polite"></div>

  <div class="meta-grid" aria-label="At-risk metadata">
    <div class="meta-card">
      <strong>Lot Expiry Field</strong>
      <span id="meta-expiry-field">—</span>
    </div>
    <div class="meta-card">
      <strong>Window</strong>
      <span id="meta-window">—</span>
    </div>
    <div class="meta-card">
      <strong>Last Refreshed</strong>
      <span id="meta-refreshed">—</span>
    </div>
    <div class="meta-card">
      <strong>Source</strong>
      <span id="meta-source">Odoo inventory snapshot</span>
    </div>
  </div>

  <section aria-label="At-risk lots table">
    <table>
      <thead>
        <tr>
          <th scope="col">Product</th>
          <th scope="col">Code</th>
          <th scope="col">Lot</th>
          <th scope="col">Expiry</th>
          <th scope="col">Days Left</th>
          <th scope="col">Quantity</th>
        </tr>
      </thead>
      <tbody id="at-risk-tbody">
        <tr><td colspan="6">Loading at-risk lots…</td></tr>
      </tbody>
    </table>
  </section>

  <footer>
    Inventory data is sourced from the most recent Odoo snapshot. Adjust the expiry window to expand or narrow the queue for store teams.
  </footer>

  <script>
    const state = {
      items: [],
      meta: {},
      error: null,
      loading: false,
      filters: { days: 3 },
      lastFetched: null
    };

    const DAY_OPTIONS = [1, 2, 3, 5, 7, 10, 14, 21, 30];

    function populateDays() {
      const select = document.getElementById("filter-days");
      if (!select) return;
      select.innerHTML = "";
      DAY_OPTIONS.forEach((value) => {
        const option = document.createElement("option");
        option.value = String(value);
        option.textContent = value === 1 ? "1 day" : `${value} days`;
        if (value === state.filters.days) {
          option.selected = true;
        }
        select.appendChild(option);
      });
    }

    function parseDays(entry) {
      const raw = Number(entry?.days_left ?? entry?.days_until);
      if (Number.isNaN(raw)) {
        return null;
      }
      return raw;
    }

    function filterAtRiskItems(items, windowDays) {
      if (!Array.isArray(items)) {
        return [];
      }
      return items.filter((entry) => {
        const days = parseDays(entry);
        if (days === null) {
          return true;
        }
        if (days < 0) {
          return true;
        }
        return days <= windowDays;
      });
    }

    function getFilteredItems() {
      return filterAtRiskItems(state.items, state.filters.days);
    }

    function setStatus(message, tone) {
      const status = document.getElementById("status");
      if (!status) return;
      if (!message) {
        status.className = "";
        status.classList.remove("visible");
        status.textContent = "";
        return;
      }
      status.className = tone ? `${tone} visible` : "visible";
      status.textContent = message;
    }

    function setSyncBanner(message, tone) {
      const banner = document.getElementById("sync-status");
      if (!banner) return;
      if (!message) {
        banner.className = "status-banner";
        banner.textContent = "";
        return;
      }
      banner.className = `status-banner visible ${tone || ""}`.trim();
      banner.textContent = message;
    }

    function formatDate(value) {
      if (!value) return "—";
      const parsed = new Date(value);
      if (Number.isNaN(parsed.getTime())) {
        return value;
      }
      return parsed.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
    }

    function formatDateTime(value) {
      if (!value) return "—";
      return value.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    }

    function renderMeta() {
      const meta = state.meta || {};
      document.getElementById("meta-expiry-field").textContent = meta.lot_expiry_field || "—";
      const windowText = state.filters.days === 1 ? "Next 1 day" : `Next ${state.filters.days} days`;
      document.getElementById("meta-window").textContent = windowText;
      document.getElementById("meta-refreshed").textContent = formatDateTime(state.lastFetched);
    }

    function renderSummary() {
      const items = Array.isArray(state.items) ? state.items : [];
      const filtered = getFilteredItems();
      let total = filtered.length;
      let dueToday = 0;
      let overdue = 0;
      let upcoming = 0;
      filtered.forEach((item) => {
        const raw = parseDays(item);
        if (raw === null) {
          return;
        }
        if (raw < 0) {
          overdue += 1;
        } else if (raw === 0) {
          dueToday += 1;
        } else if (raw > 0 && raw <= state.filters.days) {
          upcoming += 1;
        }
      });

      document.getElementById("metric-total").textContent = total;
      document.getElementById("metric-total-footer").textContent = total === 1 ? "1 lot flagged" : `${total} lots flagged`;

      document.getElementById("metric-due-today").textContent = dueToday;
      document.getElementById("metric-due-today-footer").textContent = dueToday ? "Prioritize these lots first" : "No lots expiring today";

      document.getElementById("metric-overdue").textContent = overdue;
      document.getElementById("metric-overdue-footer").textContent = overdue ? "Escalate overdue lots immediately" : "No overdue lots";

      document.getElementById("metric-upcoming").textContent = upcoming;
      const span = state.filters.days;
      const caption = upcoming ? `Expiring in the next ${span} day${span === 1 ? "" : "s"}` : `No lots expiring in the next ${span} day${span === 1 ? "" : "s"}`;
      document.getElementById("metric-upcoming-footer").textContent = caption;
    }

    function renderTable() {
      const tbody = document.getElementById("at-risk-tbody");
      if (!tbody) return;
      tbody.innerHTML = "";

      if (state.loading) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = 6;
        cell.textContent = "Loading at-risk lots…";
        row.appendChild(cell);
        tbody.appendChild(row);
        return;
      }

      const meta = state.meta || {};
      if (meta.error === "odoo_unreachable") {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = 6;
        cell.textContent = "Connect to Odoo to retrieve at-risk inventory.";
        row.appendChild(cell);
        tbody.appendChild(row);
        return;
      }
      if (meta.reason === "no_stock_lot_model") {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = 6;
        cell.textContent = "Enable the stock.lot model in Odoo to surface at-risk items.";
        row.appendChild(cell);
        tbody.appendChild(row);
        return;
      }
      if (meta.reason === "no_expiry_field") {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = 6;
        cell.textContent = "Configure a lot expiry field in Odoo to compute at-risk windows.";
        row.appendChild(cell);
        tbody.appendChild(row);
        return;
      }

      const filteredItems = getFilteredItems();
      if (!filteredItems.length) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = 6;
        cell.textContent = `No at-risk lots within the next ${state.filters.days} day${state.filters.days === 1 ? "" : "s"}.`;
        row.appendChild(cell);
        tbody.appendChild(row);
        return;
      }

      filteredItems.forEach((entry) => {
        const row = document.createElement("tr");
        const daysRaw = parseDays(entry);
        if (daysRaw !== null) {
          if (daysRaw < 0) {
            row.classList.add("overdue");
          } else if (daysRaw === 0) {
            row.classList.add("due-today");
          }
        }

        const productCell = document.createElement("td");
        productCell.textContent = entry.product || entry.product_name || "—";
        row.appendChild(productCell);

        row.appendChild(createCell(entry.default_code || "—"));
        row.appendChild(createCell(entry.lot || "—"));
        row.appendChild(createCell(formatDate(entry.life_date)));

        const daysCell = document.createElement("td");
        if (daysRaw === null) {
          daysCell.textContent = "—";
        } else {
          if (daysRaw < 0) {
            const badge = document.createElement("span");
            badge.className = "badge overdue";
            badge.textContent = `${daysRaw} days`;
            daysCell.appendChild(badge);
          } else if (daysRaw === 0) {
            const badge = document.createElement("span");
            badge.className = "badge due-today";
            badge.textContent = "Expires today";
            daysCell.appendChild(badge);
          } else {
            daysCell.textContent = `${daysRaw} day${daysRaw === 1 ? "" : "s"}`;
          }
        }
        row.appendChild(daysCell);

        const qtyCell = document.createElement("td");
        const quantity = Number(entry.quantity ?? entry.qty);
        if (Number.isNaN(quantity)) {
          qtyCell.textContent = "—";
        } else {
          qtyCell.textContent = quantity.toFixed(2);
        }
        row.appendChild(qtyCell);

        tbody.appendChild(row);
      });
    }

    function createCell(value) {
      const cell = document.createElement("td");
      cell.textContent = typeof value === "string" ? value : value ?? "—";
      return cell;
    }

    async function loadData() {
      state.loading = true;
      state.error = null;
      setStatus("Loading at-risk lots…", "info");
      setSyncBanner("", "");
      renderTable();
      try {
        const response = await fetch(`/at-risk?days=${encodeURIComponent(state.filters.days)}`);
        if (!response.ok) {
          throw new Error(`Request failed with status ${response.status}`);
        }
        const payload = await response.json();
        state.items = Array.isArray(payload.items) ? payload.items : [];
        state.meta = payload && typeof payload.meta === "object" ? payload.meta : {};
        state.lastFetched = new Date();
        state.loading = false;
        renderSummary();
        renderTable();
        renderMeta();

        const meta = state.meta || {};
        const filteredCount = getFilteredItems().length;
        if (meta.error === "odoo_unreachable") {
          setStatus("Live Odoo connectivity is required to populate at-risk inventory.", "error");
          setSyncBanner("Unable to reach Odoo. Check credentials or retry shortly.", "error");
          return;
        }
        if (meta.reason === "no_stock_lot_model") {
          setStatus("Enable the stock.lot model in Odoo to retrieve at-risk lots.", "warning");
          return;
        }
        if (meta.reason === "no_expiry_field") {
          setStatus("Configure a lot expiry field (life_date or expiration_date) in Odoo to compute at-risk lots.", "warning");
          return;
        }
        if (!filteredCount) {
          setStatus(`No lots are within ${state.filters.days} day${state.filters.days === 1 ? "" : "s"} of expiry.`, "success");
        } else {
          setStatus(`Tracking ${filteredCount} at-risk lot${filteredCount === 1 ? "" : "s"} within the next ${state.filters.days} day${state.filters.days === 1 ? "" : "s"}.`, "success");
        }
      } catch (error) {
        console.error(error);
        state.loading = false;
        state.items = [];
        state.meta = {};
        state.error = error instanceof Error ? error.message : "unknown";
        renderSummary();
        renderTable();
        renderMeta();
        setStatus("Unable to load at-risk lots. Check console output for details.", "error");
        setSyncBanner("The last refresh failed. Re-run the loader after verifying connectivity.", "warning");
      }
    }

    document.getElementById("filter-days")?.addEventListener("change", (event) => {
      const value = Number(event.target.value);
      if (!Number.isFinite(value) || value <= 0) {
        return;
      }
      state.filters.days = value;
      const link = document.getElementById("download-json");
      if (link) {
        link.href = `/at-risk?days=${encodeURIComponent(value)}`;
      }
      renderMeta();
      loadData();
    });

    document.getElementById("refresh-btn")?.addEventListener("click", loadData);

    populateDays();
    const link = document.getElementById("download-json");
    if (link) {
      link.href = `/at-risk?days=${encodeURIComponent(state.filters.days)}`;
    }
    renderMeta();
    renderSummary();
    loadData();
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

    @app.get("/compliance/events", response_class=JSONResponse)
    def compliance_events(
        since: str | None = Query(None),
        type: str | None = Query(None),
        code: str | None = Query(None),
        store: str | None = Query(None),
        limit: int = Query(100, ge=1, le=500),
    ) -> List[dict[str, object]]:
        try:
            create_all()
        except Exception:
            app_logger.exception("Failed to ensure compliance tables exist before querying events")
            raise HTTPException(500, {"detail": "compliance_setup_failed"}) from None

        try:
            since_dt = _parse_since(since)
        except ValueError as exc:
            raise HTTPException(400, {"since": str(exc)}) from exc

        stmt = select(ComplianceEvent).order_by(ComplianceEvent.timestamp.desc())
        conditions = []
        if since_dt:
            conditions.append(ComplianceEvent.timestamp >= since_dt)
        if type:
            conditions.append(ComplianceEvent.event_type == type.lower())
        if code:
            conditions.append(ComplianceEvent.product_code == code)
        if store:
            conditions.append(ComplianceEvent.store == store)
        if conditions:
            stmt = stmt.where(*conditions)
        stmt = stmt.limit(limit)

        try:
            with compliance_session() as session:
                records = session.execute(stmt).scalars().all()
        except Exception:
            app_logger.exception("Failed to load compliance events from database")
            raise HTTPException(500, {"detail": "compliance_query_failed"}) from None

        payload: List[dict[str, object]] = []
        for record in records:
            data = serialize_event(record)
            if _COMPLIANCE_VALIDATOR is not None:
                _COMPLIANCE_VALIDATOR.validate(data)
            payload.append(data)
        return payload

    @app.get("/compliance/export.csv")
    def export_compliance_csv() -> Response:
        csv_path = resolve_csv_path(None)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        if csv_path.exists():
            try:
                text = csv_path.read_text(encoding="utf-8")
            except OSError:
                app_logger.exception("Failed to read compliance export at %s", csv_path)
            else:
                return _csv_response(text, filename="compliance_events.csv")
        empty_text = _render_csv([], COMPLIANCE_CSV_HEADERS)
        return _csv_response(empty_text, filename="compliance_events.csv")

    @app.get("/export/flagged.csv")
    def export_flagged_csv(
        store: str | None = Query(None),
        category: str | None = Query(None),
        reason: str | None = Query(None),
        api_key: str | None = Query(None),
    ) -> Response:
        _require_api_key(api_key)
        result = flagged(store=store, category=category, reason=reason)
        items = result.get("items", [])
        rows = _serialize_flagged_csv_rows(items)
        csv_text = _render_csv(rows, FLAGGED_CSV_HEADERS)
        return _csv_response(csv_text, filename="flagged.csv")

    @app.get("/export/events.csv")
    def export_events_csv(
        limit: int = Query(100, ge=1, le=1000),
        type: str | None = Query(None),
        since: str | None = Query(None),
        api_key: str | None = Query(None),
    ) -> Response:
        _require_api_key(api_key)
        result = events(limit=limit, type=type, since=since)
        entries = result.get("events", [])
        rows = _serialize_events_csv_rows(entries)
        csv_text = _render_csv(rows, EVENTS_CSV_HEADERS)
        return _csv_response(csv_text, filename="events.csv")

    @app.get("/metrics/last_sync", response_class=JSONResponse)
    def metrics_last_sync() -> dict[str, object]:
        try:
            store = store_provider()
        except Exception:
            app_logger.exception("Failed to initialize event store for last sync metric")
            return {"last_sync": None, "meta": {"source": "database", "error": "store_init_failed"}}
        try:
            timestamp = store.get_last_integration_sync()
        except Exception:
            app_logger.exception("Failed to retrieve last integration sync timestamp")
            return {"last_sync": None, "meta": {"source": "database", "error": "query_failed"}}

        iso_value = timestamp.astimezone(timezone.utc).isoformat() if timestamp else None
        meta: dict[str, object] = {"source": "database"}
        if iso_value is None:
            meta["status"] = "not_recorded"
        return {"last_sync": iso_value, "meta": meta}

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

    @app.post("/labels/markdown")
    def markdown_labels(
        payload: Mapping[str, object] | None = Body(None),
        combined: bool = Query(False),
    ):
        if payload is None:
            raise HTTPException(400, {"default_codes": "provide JSON body with default_codes list"})
        if not isinstance(payload, Mapping):
            raise HTTPException(400, {"default_codes": "expected JSON object with default_codes list"})
        raw_codes = payload.get("default_codes")
        if not isinstance(raw_codes, list):
            raise HTTPException(400, {"default_codes": "expected list of product codes"})

        codes: list[str] = []
        for value in raw_codes:
            if not isinstance(value, str):
                raise HTTPException(400, {"default_codes": "all codes must be strings"})
            trimmed = value.strip()
            if trimmed and trimmed not in codes:
                codes.append(trimmed)
        if not codes:
            raise HTTPException(400, {"default_codes": "provide at least one product code"})

        client = odoo_provider()
        if client is None:
            app_logger.error("Cannot generate labels: Odoo client unavailable")
            return {
                "generated": [],
                "count": 0,
                "error": "odoo_unreachable",
                "requested": codes,
            }

        output_dir = _resolve_repo_path(labels_provider())
        generator = MarkdownLabelGenerator(client, output_dir=output_dir)
        try:
            documents = generator.generate(codes)
        except Exception:
            app_logger.exception("Failed to generate label PDFs")
            return {
                "generated": [],
                "count": 0,
                "error": "label_generation_failed",
                "requested": codes,
            }

        if combined:
            labels_root = output_dir.resolve()
            cache_key = "|".join(codes)
            cache_hash = hashlib.sha1(cache_key.encode("utf-8")).hexdigest()[:12]
            combined_filename = f"labels-combined-{len(codes)}-{cache_hash}.pdf"
            combined_path = labels_root / combined_filename
            if not combined_path.exists():
                try:
                    combined_payload = generator.render_combined_pdf(documents)
                    combined_path.write_bytes(combined_payload)
                except Exception:
                    app_logger.exception("Failed to build combined label PDF")
                    raise HTTPException(500, {"detail": "combined_pdf_failed"}) from None
                _refresh_labels_static_index(output_dir)
            try:
                response = FileResponse(combined_path, media_type="application/pdf")
            except Exception:
                app_logger.exception("Failed to stream combined label PDF")
                raise HTTPException(500, {"detail": "combined_pdf_failed"}) from None
            response.headers["Content-Disposition"] = f'inline; filename="{combined_path.name}"'
            return response

        labels_root = output_dir.resolve()
        generated_items: list[dict[str, object]] = []
        for doc in documents:
            url = _static_label_url(Path(doc.pdf_path), labels_root=labels_root)
            entry: dict[str, object] = {
                "code": doc.default_code,
                "path": str(doc.pdf_path),
                "url": url,
            }
            if not doc.found:
                entry["found"] = False
            generated_items.append(entry)

        missing = [doc.default_code for doc in documents if not doc.found]
        result: dict[str, object] = {
            "generated": generated_items,
            "count": len(documents),
            "requested": codes,
        }
        if missing:
            result["missing"] = missing
        _refresh_labels_static_index(output_dir)
        return result

    @app.get("/static/labels/", include_in_schema=False)
    def static_labels_root() -> Response:
        labels_root = _labels_root()
        index_path = labels_root / "index.html"
        if index_path.exists():
            try:
                content = index_path.read_text(encoding="utf-8")
            except OSError:
                raise HTTPException(500, {"detail": "labels_index_unreadable"}) from None
            return HTMLResponse(content)
        raise HTTPException(404, {"detail": "Not found"})

    @app.get("/static/labels/{requested_path:path}", include_in_schema=False)
    def static_label_asset(requested_path: str) -> Response:
        labels_root = _labels_root()
        target = (labels_root / requested_path).resolve()
        try:
            target.relative_to(labels_root)
        except ValueError:
            raise HTTPException(404, {"detail": "Not found"})
        if target.is_dir():
            index_path = target / "index.html"
            if index_path.exists():
                try:
                    content = index_path.read_text(encoding="utf-8")
                except OSError:
                    raise HTTPException(500, {"detail": "labels_index_unreadable"}) from None
                return HTMLResponse(content)
            raise HTTPException(404, {"detail": "Not found"})
        if not target.exists():
            raise HTTPException(404, {"detail": "Not found"})
        media_type = "application/pdf" if target.suffix.lower() == ".pdf" else "application/octet-stream"
        response = FileResponse(target, media_type=media_type)
        response.headers["Content-Disposition"] = f"inline; filename=\"{target.name}\""
        return response

    @app.get("/out/labels", response_class=JSONResponse)
    def labels_index_no_slash() -> dict[str, object]:
        output_dir = labels_provider()
        return _labels_directory_listing(output_dir)

    @app.get("/out/labels/", response_class=JSONResponse)
    def labels_index() -> dict[str, object]:
        output_dir = labels_provider()
        return _labels_directory_listing(output_dir)

    return app


def _serialize_flagged_csv_rows(items: Sequence[Mapping[str, object]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for entry in items:
        if not isinstance(entry, Mapping):
            continue
        row = {
            "default_code": _stringify(entry.get("default_code")),
            "product": _stringify(entry.get("product_name") or entry.get("product")),
            "lot": _stringify(entry.get("lot")),
            "reason": _stringify(entry.get("reason")),
            "outcome": _stringify(entry.get("outcome")),
            "suggested_qty": _stringify(entry.get("suggested_qty")),
            "quantity": _stringify(entry.get("qty") or entry.get("quantity")),
            "unit": _stringify(entry.get("unit") or entry.get("unit_of_measure") or entry.get("uom")),
            "estimated_weight_lbs": _stringify(entry.get("estimated_weight_lbs")),
            "price_markdown_pct": _stringify(entry.get("price_markdown_pct")),
            "store": _stringify(entry.get("store")),
            "stores": _stringify_sequence(entry.get("stores")),
            "category": _stringify(entry.get("category")),
            "notes": _stringify(entry.get("notes")),
        }
        rows.append(row)
    return rows


def _serialize_events_csv_rows(entries: Sequence[Mapping[str, object]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        row = {
            "timestamp": _stringify(entry.get("ts") or entry.get("timestamp")),
            "type": _stringify(entry.get("type")),
            "product": _stringify(entry.get("product")),
            "lot": _stringify(entry.get("lot")),
            "quantity": _stringify(entry.get("qty") or entry.get("quantity")),
            "before_quantity": _stringify(entry.get("before") or entry.get("before_qty") or entry.get("before_quantity")),
            "after_quantity": _stringify(entry.get("after") or entry.get("after_qty") or entry.get("after_quantity")),
            "source": _stringify(entry.get("source")),
        }
        rows.append(row)
    return rows


def _render_csv(rows: Sequence[Mapping[str, str]], headers: Sequence[str]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(headers),
        extrasaction="ignore",
        lineterminator="\r\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in headers})
    return buffer.getvalue()


def _csv_response(text: str, *, filename: str) -> Response:
    payload = text if text.startswith("\ufeff") else f"\ufeff{text}"
    try:
        response = Response(payload, media_type="text/csv; charset=utf-8")
    except TypeError:
        response = Response(payload)
        response.media_type = "text/csv; charset=utf-8"
    else:
        response.media_type = "text/csv; charset=utf-8"
    headers = getattr(response, "headers", None)
    if headers is None:
        headers = {}
        setattr(response, "headers", headers)
    headers.setdefault("Content-Type", "text/csv; charset=utf-8")
    headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _stringify(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.10g}"
    return str(value)


def _stringify_sequence(value: object) -> str:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        parts = [text for item in value if (text := _stringify(item))]
        return "; ".join(parts)
    return _stringify(value)


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


def _resolve_repo_path(path: Path | str) -> Path:
    path_obj = path if isinstance(path, Path) else Path(path)
    if path_obj.is_absolute():
        return path_obj
    return (REPO_ROOT / path_obj).resolve()


def _default_labels_path() -> Path:
    return REPO_ROOT / "out" / "labels"


def _default_flagged_path() -> Path:
    from .data import DEFAULT_FLAGGED_PATH

    return DEFAULT_FLAGGED_PATH


def _labels_directory_listing(directory: Path) -> dict[str, object]:
    labels_root = _resolve_repo_path(directory).resolve()
    if not labels_root.exists():
        return {
            "labels": [],
            "meta": {
                "exists": False,
                "count": 0,
                "output_dir": str(labels_root),
            },
        }
    items: list[dict[str, object]] = []
    for pdf_path in sorted(labels_root.glob("*.pdf")):
        try:
            stat = pdf_path.stat()
        except OSError:
            continue
        url_value = "/static/labels/" + pdf_path.relative_to(labels_root).as_posix()
        items.append(
            {
                "filename": pdf_path.name,
                "path": str(pdf_path),
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "url": url_value,
            }
        )
    return {
        "labels": items,
        "meta": {
            "exists": True,
            "count": len(items),
            "output_dir": str(labels_root),
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
