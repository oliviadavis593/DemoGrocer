# FoodFlow Overview

FoodFlow bundles the tooling needed to demonstrate a realistic grocery retail operation on top of an Odoo instance. The project focuses on three pillars:

1. **Seed** – populate Odoo with demo products, lots, and starting stock levels.
2. **Simulate** – run background jobs that mimic daily movements (sales, expiries, receipts) to keep the dataset dynamic.
3. **Report** – expose a lightweight API for reviewing activity, generating labels, and diagnosing upstream capabilities.

## Core Components

- **packages/odoo_client** – Thin XML-RPC client that loads credentials from `.env` and shares session logic across scripts, the simulator, and the web app.
- **scripts/seed_inventory.py** – Idempotent importer that provisions units of measure, categories, products, lots, and starting balances. The script writes a CSV summary under `out/`.
- **services/simulator** – Orchestrates sell-down, returns, shrink, expiry, and receiving jobs. Events flow to `out/events.jsonl` and optionally to SQLite via `packages/db`.
- **services/analysis/shrink_triggers.py** – Evaluates recent sales history against configurable thresholds to emit `flag_low_movement` and `flag_overstock` analysis events during simulator ticks.
- **packages/db** – Local SQLite helpers and schema migration tooling that store simulator events for historical reporting.
- **apps/web** – FastAPI application that surfaces diagnostics, recent events, inventory metrics, PDF label generation, and directory listings for rendered labels.
- **services/docs/labels.py** – Markdown-to-PDF label generator used both by the API and by the `make labels-demo` helper script.

## Typical Workflow

1. Fill in `.env` with Odoo URL, database, username, and password.
2. Run `make diagnose` to confirm connectivity and the presence of `stock.lot` and its `life_date` field.
3. Execute `make seed` to load the demo catalog and starting inventory data.
4. Invoke `make simulate` (single pass) or `make simulate-start` (continuous) to produce event activity. Confirm the new movement and analysis events with:
   - `tail -n 50 out/events.jsonl | grep -E '"type":"(returns|shrink|flag_low_movement|flag_overstock)"' | head`
   - `curl -s "http://localhost:8000/events?type=flag_low_movement&since=7d" | head`
5. Launch `make web`, then browse `http://localhost:8000/` for links to health checks, events, metrics, at-risk products, and label endpoints.

## Key Make Targets

| Target | Description |
| ------ | ----------- |
| `make diagnose` | Authenticates with Odoo and prints database plus capability checks. |
| `make seed` | Loads demo products, lots, and quantities; yields a CSV summary. |
| `make simulate` | Runs one simulator cycle and records events. |
| `make simulate-start` | Starts the scheduler for continuous simulation (Ctrl+C to stop). |
| `make web` | Serves the FastAPI reporting layer on port 8000. |
| `make labels-demo` | Generates sample PDF labels for demo SKUs in `out/labels`. |

## Directory Guide

- `apps/` – Application entry points (`web` for reporting, `fastapi/` for alternative deployments).
- `packages/` – Reusable modules shared across scripts and services (`db`, `odoo_client`).
- `scripts/` – Command-line utilities for seeding, diagnosing, migrating, and label demos.
- `services/` – Domain services including the simulator and label rendering helper.
- `tests/` – Pytest suites covering simulator logic, web endpoints, and utilities.
- `out/` – Generated artifacts such as JSONL event logs, PDFs, CSV summaries, and the SQLite database.
