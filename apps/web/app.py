"""FastAPI application exposing reporting endpoints."""
from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Callable, Iterable, List

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from packages.odoo_client import OdooClient, OdooClientError
from services.simulator.inventory import InventoryRepository

from .data import calculate_at_risk, load_recent_events

EventsPathProvider = Callable[[], Path]
RepositoryFactory = Callable[[], InventoryRepository]


def _default_events_path() -> Path:
    from .data import DEFAULT_EVENTS_PATH

    return DEFAULT_EVENTS_PATH


def _default_repository_factory() -> InventoryRepository:
    client = OdooClient()
    client.authenticate()
    return InventoryRepository(client)


def create_app(
    *,
    events_path_provider: EventsPathProvider | None = None,
    repository_factory: RepositoryFactory | None = None,
) -> FastAPI:
    """Construct the FastAPI application."""

    app = FastAPI(title="FoodFlow Reporting API")

    app.dependency_overrides[_get_events_path] = events_path_provider or _default_events_path
    app.dependency_overrides[_get_repository] = repository_factory or _default_repository_factory

    @app.get("/health", response_class=JSONResponse)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/events/recent", response_class=HTMLResponse)
    def recent_events(
        limit: int = Query(20, ge=1, le=200),
        events_path: Path = Depends(_get_events_path),
    ) -> HTMLResponse:
        records = load_recent_events(events_path, limit=limit)
        body = _render_table(
            title="Recent Events",
            headers=["Timestamp", "Type", "Product", "Lot", "Quantity", "Before", "After"],
            rows=[
                [
                    escape(record.ts.isoformat()),
                    escape(record.type or ""),
                    escape(record.product or ""),
                    escape(record.lot or ""),
                    f"{record.qty:+.2f}",
                    f"{record.before:.2f}",
                    f"{record.after:.2f}",
                ]
                for record in records
            ],
            empty_message="No events have been recorded yet.",
        )
        return HTMLResponse(content=body)

    @app.get("/at-risk", response_class=HTMLResponse)
    def at_risk(
        threshold_days: int = Query(3, ge=0, le=30),
        repository: InventoryRepository = Depends(_get_repository),
    ) -> HTMLResponse:
        try:
            snapshot = repository.load_snapshot()
        except OdooClientError as exc:  # pragma: no cover - exercised in runtime usage
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        items = calculate_at_risk(snapshot, threshold_days=threshold_days)
        body = _render_table(
            title="At-Risk Inventory",
            headers=["Product", "Lot", "Expiry Date", "Days Remaining", "Quantity"],
            rows=[
                [
                    escape(item.product),
                    escape(item.lot or ""),
                    escape(item.life_date.isoformat()),
                    str(item.days_until),
                    f"{item.quantity:.2f}",
                ]
                for item in items
            ],
            empty_message="No inventory items are within the risk window.",
        )
        return HTMLResponse(content=body)

    return app


def _get_events_path(provider: EventsPathProvider = Depends(_default_events_path)) -> Path:
    return provider()


def _get_repository(
    factory: RepositoryFactory = Depends(_default_repository_factory),
) -> InventoryRepository:
    return factory()


def _render_table(
    *, title: str, headers: Iterable[str], rows: Iterable[Iterable[str]], empty_message: str
) -> str:
    header_list = [escape(header) for header in headers]
    rendered_rows: List[str] = []
    for row in rows:
        rendered_cells = "".join(f"<td>{cell}</td>" for cell in row)
        rendered_rows.append(f"<tr>{rendered_cells}</tr>")
    rows_html = "".join(rendered_rows)
    if not rows_html:
        rows_html = f"<tr><td colspan='{len(header_list)}'>{escape(empty_message)}</td></tr>"
    header_html = "".join(f"<th>{header}</th>" for header in header_list)
    title_html = escape(title)
    table_html = (
        f"<html><head><title>{title_html}</title>"
        "<style>table{border-collapse:collapse;}th,td{border:1px solid #ccc;padding:4px;}</style>"
        "</head><body>"
        f"<h1>{title_html}</h1>"
        f"<table><thead><tr>{header_html}</tr></thead><tbody>{rows_html}</tbody></table>"
        "</body></html>"
    )
    return table_html


__all__ = ["create_app"]
