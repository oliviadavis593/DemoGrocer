# FoodFlow File Structure Guide

This guide walks through the repository layout so new contributors can quickly locate the moving parts behind seeding, simulation, and reporting.

## Top-Level Directories

| Path | Purpose |
| ---- | ------- |
| `apps/` | Application entry points (FastAPI web server and ASGI adapters). |
| `config/` | Configuration files such as simulator defaults. |
| `docs/` | Project documentation, including this guide and the high-level overview. |
| `fastapi/` | Minimal FastAPI wrapper for alternative deployments. |
| `packages/` | Reusable Python packages shared across scripts and services (`db`, `odoo_client`). |
| `scripts/` | Command-line utilities for seeding, diagnostics, migrations, and demos. |
| `services/` | Domain services for the simulator, reporting helpers, and label generation. |
| `tests/` | Pytest suites covering the simulator, web API, and supporting utilities. |
| `out/` | Generated artifacts (JSONL logs, CSV summaries, SQLite database, PDF labels). |

## apps/

- `apps/web/app.py` – FastAPI application factory with all HTTP routes (health, events, metrics, at-risk, labels, and directory listings).
- `apps/web/main.py` – CLI entry point for launching the Uvicorn server; wires environment loading and dependency injection.
- `fastapi/app.py` – Alternate ASGI application module used by lightweight deployment targets.

## packages/

- `packages/odoo_client/client.py` – XML-RPC client encapsulating authentication, search, create, and write helpers against Odoo.
- `packages/db/core.py` – SQLite connection utilities, path discovery, and context managers.
- `packages/db/events.py` – Inventory event dataclass plus persistence layer for reading and writing simulator events.
- `packages/db/__init__.py` – Re-export of database helpers to simplify imports across the code base.

## scripts/

- `scripts/seed_inventory.py` – Idempotent importer that provisions units of measure, categories, products, lots, and starting stock; writes `out/seed_summary.csv`.
- `scripts/diagnose_odoo.py` – Connectivity health check that prints the database name and verifies `stock.lot` and its `life_date` field.
- `scripts/db_migrate.py` – SQLite schema migration utility that creates indexes and tables for the simulator event store.
- `scripts/labels_demo.py` – Convenience helper to generate PDF labels for sample products and report the output paths.

## services/

- `services/simulator/` – Core simulation engine:
  - `config.py` – Data classes and parsing logic for simulator configuration (`sell_down`, `receiving`, `daily_expiry`).
  - `service.py` – High-level orchestration that runs jobs and coordinates persistence.
  - `jobs.py` – Individual job implementations for sell-down, receiving, and daily expiry adjustments.
  - `events.py` – JSONL writer and database bridge for simulator events.
  - `inventory.py` – Odoo inventory repository for loading and mutating stock quants.
  - `state.py` & `scheduler.py` – Track job execution intervals and schedule recurring runs.
- `services/docs/labels.py` – Markdown-based PDF label renderer with optional WeasyPrint integration and pure-Python fallback.

## apps/web/data.py

Although located alongside the web app, this module deserves a call-out:
- Provides parsers and serializers for recent events, metrics, and at-risk items.
- Houses helper functions (`load_recent_events`, `serialize_events`, etc.) that power several API endpoints.

## tests/

- `tests/test_web_app.py` – Comprehensive coverage for the FastAPI routes, label generation, and error handling paths.
- `tests/test_simulator.py` – Simulator orchestration tests exercising fake Odoo clients and job pipelines.
- `tests/test_odoo_client.py` – Unit tests for the XML-RPC client, ensuring authentication and request handling behave as expected.

## Generated Output

- `out/events.jsonl` – Append-only log of simulator activity written by `EventWriter`.
- `out/foodflow.db` – SQLite database storing inventory events when migrations are applied.
- `out/labels/` – Directory filled with PDF labels generated via API calls or `make labels-demo`.
- `out/seed_summary.csv` – Summary of the seeded inventory written by the seeding script.

Use this guide alongside `docs/overview.md` to understand both the bird’s-eye view and the concrete file responsibilities.***
