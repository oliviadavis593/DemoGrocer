# Changelog
All notable changes to this project will be documented in this file.

## [Unreleased]


## [0.4.1] - 2025-10-16
### Added
- Richer product catalog metadata powering the seeding flow, including realistic quantity on hand splits, wholesale unit costs, and average cost baselines replicated across Odoo and offline fixtures.
- Inventory seeding glossary at `docs/inventory_seed.md` covering terminology, cost math, and how quantities are applied in Odoo.

### Changed
- Inventory seed script now adjusts stock quants in inventory mode so Odoo reflects the new on-hand quantities immediately, and README highlights the expanded cost/quantity summary written to `out/seed_summary.csv`.


## [0.4.0] - 2025-10-11
### Added
- Compliance data model and recorder for IRS 170(e)(3) events, including SQLAlchemy-backed `ComplianceEvent` table, JSON-schema validation (`contracts/schemas/compliance.schema.json`), CSV export mirroring, audit logging, and demo CLI support.
- `/compliance/events` API endpoint with optional filters and schema validation plus `/compliance/export.csv` streaming of the canonical CSV.
- `make compliance-migrate` and `make compliance-export` targets for applying migrations and packaging compliance event exports.
- Test coverage for compliance persistence and HTTP endpoints (`tests/test_compliance_recorder.py`, additional FastAPI assertions).

### Changed
- README updated with compliance field definitions, source-of-truth mapping, and workflow instructions for recording and exporting events.


## [0.3.0] - 2025-10-10
### Added
- `/flagged` JSON endpoint and `/dashboard/flagged` dashboard for reviewing flagged decisions with filter controls and one-click label generation.
- `/metrics/impact` API plus dashboard overview cards summarising waste diverted (USD) and donated weight derived from decision outcomes, including documentation and tests.
- Persistent tracking of the last integration sync with a `/metrics/last_sync` endpoint and dashboard banner that highlights when data is more than 30 minutes old.
- CSV exports for flagged decisions and inventory events (`/export/flagged.csv`, `/export/events.csv`) including optional API key protection, documentation updates, and automated tests validating headers.
- Inventory fixture helpers (`services/integration/fixtures.py`) that provide stock quantities, shelf-life metadata, and supplier assignments for use in demos and offline simulations.
- Deterministic fake movement generator (`services/integration/movements.py`) that produces repeatable sale, expiry, and clearance events for perishable and low-demand items.
- Decision policy adjustments for low-movement shelf-stable categories so donation outcomes appear on the flagged dashboard alongside markdowns.

### Changed
- `/flagged` responses (scheduler API, reporting API, and CSV export) now enrich each decision with live `product_name`, `category`, store names, and on-hand `qty` sourced from Odoo stock while excluding configurable quarantine locations for faster dashboard reviews.
- `/labels/markdown` now returns JSON summaries for each generated PDF (`generated[].url`), persists files under `out/labels/<CODE>.pdf`, caches combined PDF requests based on the requested codes, and serves them via `/static/labels/` alongside a refreshed dashboard toast linking to each label.
- `/flagged` responses and the dashboard now include per-record `estimated_weight_lbs` alongside aggregated pound totals, helping highlight waste avoidance next to markdown dollars.


## [0.2.1] - 2025-10-09
### Added
- High-level inventory helpers on the integration service (`fetch_inventory_snapshot`, `fetch_sales`) that aggregate lot/expiry/location data and compute sales velocity from the event store with Odoo fallback.
- `snapshot` subcommand for the integration runner (`PYTHONPATH=. python3 services/integration/runner.py snapshot`) that authenticates, fetches inventory, and prints a summary without executing a full sync cycle.
- Documentation refresh covering the snapshot command in both `README.md` and `docs/overview.md`, ensuring the new workflow is visible alongside existing Make targets.
- Additional integration service unit tests verifying snapshot aggregation, sales velocity sourcing, and location grouping behaviour.
- Shrink detector utilities for the integration service plus a `detect` subcommand (`PYTHONPATH=. python3 services/integration/runner.py detect --days 7`) that emits near-expiry, low-movement, and overstock flags with supporting metrics, alongside dedicated documentation and pytest coverage.
- Decision policy engine that maps shrink detector flags to reusable decision objects via `config/decision_policy.yaml`, exposes a `decisions` runner command (`PYTHONPATH=. python3 services/integration/runner.py decisions`), and adds pytest coverage for the mapper logic.
- Integration scheduler (`services/integration/schedule.py`) that periodically maps shrink flags to decisions, writes `out/flagged.json`, and exposes `/flagged` plus `/health` on port 8000 for downstream consumers.

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
