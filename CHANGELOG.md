# Changelog
All notable changes to this project will be documented in this file.


## [0.2.1] - 2025-10-09
### Added
- High-level inventory helpers on the integration service (`fetch_inventory_snapshot`, `fetch_sales`) that aggregate lot/expiry/location data and compute sales velocity from the event store with Odoo fallback.
- `snapshot` subcommand for the integration runner (`PYTHONPATH=. python3 services/integration/runner.py snapshot`) that authenticates, fetches inventory, and prints a summary without executing a full sync cycle.
- Documentation refresh covering the snapshot command in both `README.md` and `docs/overview.md`, ensuring the new workflow is visible alongside existing Make targets.
- Additional integration service unit tests verifying snapshot aggregation, sales velocity sourcing, and location grouping behaviour.
- Shrink detector utilities for the integration service plus a `detect` subcommand (`PYTHONPATH=. python3 services/integration/runner.py detect --days 7`) that emits near-expiry, low-movement, and overstock flags with supporting metrics, alongside dedicated documentation and pytest coverage.

## [0.2.0] - 2025-10-09
### Added
- Staff seeding script and `make seed-staff` target for provisioning demo cashier, department manager, and store manager accounts with credentials exported to `.out/staff_credentials.json`.
- SQLite-backed event store with migration tooling, simulator persistence, and new `/events` plus `/metrics/summary` API endpoints for querying inventory activity.
- Markdown label PDF generation service exposed via `/labels/markdown` and browsable at `/out/labels/`, producing WeasyPrint-compatible templates saved under `out/labels/`.
- Developer UX polish with expanded Make targets (`diagnose`, `seed`, `simulate`, `simulate-start`, `web`, `labels-demo`), improved diagnostics output, and README quick-start coverage for each workflow.
- Repository overview section and documentation (`docs/overview.md`, `docs/structure.md`) outlining architecture, directory responsibilities, key Make targets, and reporting API endpoints.
- Simulator jobs for customer returns and shrinkage that append  JSONL/DB events while keeping on-hand quantities non-negative.
- Shrink trigger detector that raises `flag_low_movement` and `flag_overstock` events from simulator ticks using configurable thresholds in `config/shrink_triggers.yaml`.
- Recall workflow covering `scripts/recall.py`, `/recall/trigger`, and `/recall/quarantined`, ensuring recalled SKUs are moved to a Quarantine location and logged as `recall_quarantine` events.
- Integration service scaffolding with reusable Odoo wrapper, package config, CLI runner, and shared API wiring for inventory sync cycles.
- Scheduled GitHub Actions workflow that runs `make integration-sync` for automated Odoo connectivity checks once repository secrets are configured.

## [0.1.0] - 2025-10-08
### Added
- Idempotent inventory seeding script that provisions UoM categories, products, lots, and summary exports for FoodFlow demos.
- Simulator services covering sell-down, receiving, and expiry jobs with inventory snapshots and JSONL event logging.
- Lightweight XML-RPC `OdooClient` package with environment-driven configuration shared by scripts, simulator, and API.
- FastAPI-based reporting app with `/health`, `/events/recent`, and `/at-risk` endpoints plus CLI for launching the server.
- Supporting developer ergonomics including `.env` scaffolding, Makefile tasks, and pytest coverage for core utilities.
