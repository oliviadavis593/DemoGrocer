# Changelog
All notable changes to this project will be documented in this file.

## [Unreleased]
### Added
- Staff seeding script and `make seed-staff` target for provisioning demo cashier, department manager, and store manager accounts with credentials exported to `.out/staff_credentials.json`.
- SQLite-backed event store with migration tooling, simulator persistence, and new `/events` plus `/metrics/summary` API endpoints for querying inventory activity.
- Markdown label PDF generation service exposed via `/labels/markdown` and browsable at `/out/labels/`, producing WeasyPrint-compatible templates saved under `out/labels/`.
- Developer UX polish with expanded Make targets (`diagnose`, `seed`, `simulate`, `simulate-start`, `web`, `labels-demo`), improved diagnostics output, and README quick-start coverage for each workflow.
- Repository overview section and documentation (`docs/overview.md`, `docs/structure.md`) outlining architecture, directory responsibilities, key Make targets, and reporting API endpoints.

## [0.1.0] - 2025-10-08
### Added
- Idempotent inventory seeding script that provisions UoM categories, products, lots, and summary exports for FoodFlow demos.
- Simulator services covering sell-down, receiving, and expiry jobs with inventory snapshots and JSONL event logging.
- Lightweight XML-RPC `OdooClient` package with environment-driven configuration shared by scripts, simulator, and API.
- FastAPI-based reporting app with `/health`, `/events/recent`, and `/at-risk` endpoints plus CLI for launching the server.
- Supporting developer ergonomics including `.env` scaffolding, Makefile tasks, and pytest coverage for core utilities.
