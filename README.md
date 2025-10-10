# FoodFlow

## Overview

FoodFlow is a developer sandbox that showcases how an Odoo-backed grocery retailer could seed, simulate, and monitor inventory data end to end. The repository includes:
- Staff seeding utilities that provision demo user accounts with pre-configured roles for store workflows.
- Inventory seeding utilities that provision demo products, lots, and stock levels in Odoo.
- A simulator that applies daily sales, expiry, and receiving patterns while logging events to JSONL and SQLite.
- Shrink trigger analysis that flags low-movement and overstock conditions into the shared event log.
- Recall tooling that quarantines products via CLI or API while logging `recall_quarantine` events.
- A FastAPI reporting service exposing recent events, at-risk products, metrics, and label generation.
- Supporting scripts and Make targets that streamline diagnostics, database migrations, and PDF label previews.

See [`docs/overview.md`](docs/overview.md) for an architectural tour and [`docs/structure.md`](docs/structure.md) for a directory-by-directory breakdown.

## Odoo Inventory Seeder

This repository includes a script that seeds a realistic starter inventory into an
Odoo instance using XML-RPC. The seeding operation is idempotent and can be run
multiple times without duplicating records.

### Prerequisites

* Python 3.10+
* Network access to the target Odoo instance with credentials that can create
  inventory data

### Setup

1. Create a virtual environment and install dependencies (none are required
   beyond the Python standard library, but a virtual environment is still
   recommended):

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

2. Copy `.env.example` to `.env` and update it with your Odoo connection
   details.

3. Export the environment variables so the script can read them. One simple
   approach is:

   ```bash
   export $(grep -v '^#' .env | xargs)
   ```

### Seeding inventory

Run the seeding script from the repository root:

```bash
make seed
# Seeded 105 products. Summary written to out/seed_summary.csv.
```

The script will create units of measure, product categories, approximately one
hundred products, traceable lots, and starting quantities in the "Backroom" and
"Sales Floor" locations. A summary of the seeded data is written to
`out/seed_summary.csv`.

### Seeding staff accounts

Provision demo staff users with predefined roles and group memberships:

```bash
make seed-staff
# cashier_1: created
# cashier_2: exists
# ...
```

The script creates cashier, department manager, and store manager accounts, assigns the appropriate Odoo groups via XML IDs, and writes passwords to `.out/staff_credentials.json`. Subsequent runs preserve existing passwords while updating group assignments as needed.

### Demo fixtures & synthetic movements

When running FoodFlow without a live Odoo instance you can rely on the new demo helpers to keep tutorials and prototypes grounded in realistic data.

```python
from datetime import date
from services.integration import (
    fixtures_as_dicts,
    fixtures_to_snapshot,
    generate_fake_movements,
    load_inventory_fixtures,
    movements_as_dicts,
)

fixtures = load_inventory_fixtures(base_date=date(2025, 1, 1))
snapshot = fixtures_to_snapshot(fixtures)
movement_events = generate_fake_movements(fixtures, days=7, start_date=date(2024, 12, 25))

# Persist or inspect structured payloads
inventory_payload = fixtures_as_dicts(fixtures)
movement_payload = movements_as_dicts(movement_events)
```

The fixture loader enriches every catalog item with backroom and sales-floor stock quantities, supplier assignments, and shelf-life metadata derived from per-category perishability windows. The movement generator emits deterministic sale, expiry, markdown, and receiving events for perishable and low-demand products so dashboards and exports remain populated even in offline demos.

### Quick Make Targets

Common workflows are available as single-command Make targets:

```bash
make diagnose
# DB name: foodflow
# stock.lot present: true
# life_date present: true

make seed
# Seeded 105 products. Summary written to out/seed_summary.csv.

make seed-staff
# cashier_1: created
# cashier_2: exists

make simulate
# INFO 2024-01-10 12:00:00,000 INFO Simulator once run emitted 13 events

make simulate-start
# INFO 2024-01-10 12:00:00,000 INFO Simulator scheduler tick (Ctrl+C to stop)

make integration-sync
# INFO 2024-01-10 12:00:00,000 INFO Integration cycle complete: 42 quants fetched at 2024-01-10T12:00:00+00:00

PYTHONPATH=. python3 services/integration/runner.py detect --days 7
# [
#   {"product": "...", "reason": "near_expiry", ...},
#   {"product": "...", "reason": "low_movement", ...}
# ]

PYTHONPATH=. python3 services/integration/runner.py decisions --days 7
# [
#   {"default_code": "...", "outcome": "MARKDOWN", ...},
#   {"default_code": "...", "outcome": "DONATE", ...}
# ]

PYTHONPATH=. python3 services/integration/schedule.py once
# INFO ... Wrote 6 flagged decisions to out/flagged.json

make labels-demo
# Generating labels for 2 product codes
# Output directory: out/labels
# - FF101: out/labels/FF101.pdf (found)

make web
# INFO 2024-01-10 12:00:00,000 INFO Starting FoodFlow web server on http://0.0.0.0:8000
```

Each command maps to a common developer workflow:
- `make diagnose` authenticates to Odoo and prints the database name plus capability checks for the `stock.lot` model and `life_date` field.
- `make seed` provisions demo inventory data inside Odoo and summarizes the number of products created.
- `make seed-staff` syncs demo cashier, department manager, and store manager accounts and records their credentials under `.out/staff_credentials.json`.
- `make simulate` runs one simulator cycle, appending sell-down, returns, shrink, expiry, receiving, and analysis flag events to `out/events.jsonl` while persisting everything to `out/foodflow.db`.
- `make simulate-start` launches the background scheduler for continuous simulation until you stop it.
- `make integration-sync` authenticates with Odoo using the new integration service and logs a summary of on-hand inventory fetched during the cycle.
- `PYTHONPATH=. python3 services/integration/runner.py snapshot --summary-limit 5` prints the current inventory count plus a few representative rows (product, lot, quantity, locations, expiry) without running the full sync automation.
- `PYTHONPATH=. python3 services/integration/runner.py detect --days 7` runs the shrink detector once, aggregating inventory and sales velocity to emit a JSON list of near-expiry, low-movement, and overstock flags using the provided thresholds.
- `PYTHONPATH=. python3 services/integration/runner.py decisions --days 7` reads the shrink flags, applies `config/decision_policy.yaml`, and prints reusable decision objects (e.g. `MARKDOWN`, `DONATE`, `RECALL_QUARANTINE`) with optional markdown guidance.
- `PYTHONPATH=. python3 services/integration/schedule.py once` runs the integration shrink detector a single time, writing the mapped decision payload to `out/flagged.json` so downstream tooling (or `curl http://localhost:8000/flagged`) can inspect the latest output.
- `PYTHONPATH=. python3 services/integration/schedule.py start --interval 10` launches the background scheduler and lightweight HTTP server that refreshes `out/flagged.json` every N minutes and serves `/flagged` alongside `/health` on port 8000.
- `make labels-demo` renders sample product labels to PDF under `out/labels`.
- `make web` starts the FastAPI reporting server so `/health` returns 200 once the app is ready.
- Visit `http://localhost:8000/dashboard/flagged` after `make web` to review flagged decisions with store/category/reason filters and kick off bulk label generation via `/labels/markdown`.

`make simulate` and `make simulate-start` automatically migrate the local SQLite
database so events are stored in `out/foodflow.db`. After a run you can spot-check the new activity with:

```bash
tail -n 50 out/events.jsonl | grep -E '"type":"(returns|shrink|flag_low_movement|flag_overstock)"' | head

curl -s "http://localhost:8000/events?type=flag_low_movement&since=7d" | head
curl -s "http://localhost:8000/events?type=flag_overstock&since=7d" | head
curl -s "http://localhost:8000/flagged" | jq
curl -s "http://localhost:8000/flagged?store=Downtown&reason=near_expiry" | jq
```

Each `/flagged` response (and the dashboard that consumes it) now includes `product_name`, `category`, an ordered `stores` list, and an on-hand `qty` sourced directly from Odoo stock quants. Quarantine locations are filtered out automatically; add any additional names to the `inventory.quarantine_locations` array in `services/integration/config.yaml` if your instance uses custom isolation areas.

Shrink trigger thresholds live in `config/shrink_triggers.yaml`. Tweak the sales window, minimum units sold, or per-category days-of-supply limits and re-run `make simulate` to observe how many `flag_low_movement` and `flag_overstock` events the detector emits.

Decision outcomes, markdown percentages, and donation rules live in `config/decision_policy.yaml`. Adjust the YAML to tune outcomes (e.g. increase markdown percentages for overstock) and re-run `PYTHONPATH=. python3 services/integration/runner.py decisions` to review the updated recommendations.

### Recalls and Quarantine

Quarantine products by default code or category with the recall script:

```bash
PYTHONPATH=. python3 scripts/recall.py --codes FF101,FF102
PYTHONPATH=. python3 scripts/recall.py --categories Dairy
```

Each match is zeroed from its sellable location, moved into the `Quarantine` location (created on demand), and logged as `type:"recall_quarantine"` in both `out/events.jsonl` and the SQLite event store. Inspect the quarantined stock and confirm the database entries through the API:

```bash
curl -s -X POST "http://localhost:8000/recall/trigger" \
  -H "Content-Type: application/json" \
  -d '{"codes":["FF101"]}'

curl -s "http://localhost:8000/recall/quarantined" | jq
```

Adjust thresholds or disable recall monitoring by editing `config/shrink_triggers.yaml` and rerunning the simulator or recall script as needed.

The long running targets (`simulate-start` and `web`) can be stopped with `Ctrl+C`.

### Automation & Monitoring

The repository ships with a scheduled GitHub Actions workflow (`.github/workflows/integration-sync.yml`) that executes `make integration-sync` every morning at 08:00 UTC and on demand via the workflow dispatch UI. To enable it:

1. In the repository settings, add the following secrets sourced from your target Odoo environment: `ODOO_URL`, `ODOO_DB`, `ODOO_USERNAME`, and `ODOO_PASSWORD`.
2. Ensure the integration runner can reach Odoo from GitHub-hosted runners (firewall/allowlist as needed).
3. Monitor the Actions tab for failures; notifications alert you if authentication or inventory fetches fail, providing early warning that upstream Odoo credentials or connectivity need attention.

### Reporting API at a Glance

Once the simulator has been seeded and is generating activity, start the
lightweight reporting app located in `apps/web`. The service loads environment
variables from `.env`, authenticates to Odoo once on boot, and exposes JSON
endpoints suitable for scripting or quick spot checks:

```bash
# Install dependencies (first run only)
python -m pip install -r requirements.txt

make web
```

Example requests:

```bash
curl -s http://localhost:8000/
# {"app":"FoodFlow reporting API","status":"ok","links":{"health":"/health","events_recent":"/events/recent","events":"/events","metrics_summary":"/metrics/summary","metrics_last_sync":"/metrics/last_sync","metrics_impact":"/metrics/impact","at_risk":"/at-risk","flagged":"/flagged","dashboard_flagged":"/dashboard/flagged","labels_markdown":"/labels/markdown","labels_index":"/out/labels/","flagged_export":"/export/flagged.csv","events_export":"/export/events.csv"},"docs":"See README.md for curl examples and Make targets."}

curl -s http://localhost:8000/health
# {"status":"ok"}

curl -s "http://localhost:8000/events/recent?limit=5"
# {"events":[{"ts":"...","type":"sell_down",...}], "meta":{"source":"jsonl","limit":5,"exists":true}}

curl -s "http://localhost:8000/events?limit=5"
# {"events":[{"ts":"...","type":"receiving",...}], "meta":{"source":"database","limit":5,"count":5}}

curl -s "http://localhost:8000/metrics/summary"
# {"events":{"total_events":120,"events_by_type":{"receiving":40,"sell_down":60,"daily_expiry":20}}, "meta":{"source":"database"}}

curl -s "http://localhost:8000/metrics/last_sync"
# {"last_sync":"2024-01-12T15:45:00+00:00","meta":{"source":"database"}}

curl -s "http://localhost:8000/metrics/impact"
# {"impact":{"diverted_value_usd":418.75,"donated_weight_lbs":132.5,"markdown_count":24,"donation_count":6},"meta":{"source":"out/flagged.json","exists":true,"count":30}}

curl -s "http://localhost:8000/at-risk?days=3"
# {"items":[{"default_code":"FF101","product":"Whole Milk","lot":"LOT-FF101","days_left":2,"quantity":5.0}],
#  "meta":{"days":3,"count":1}}

curl -I "http://localhost:8000/export/flagged.csv"
# HTTP/1.1 200 OK
# content-type: text/csv; charset=utf-8
# content-disposition: attachment; filename="flagged.csv"

curl -s "http://localhost:8000/export/events.csv?type=receiving&since=7d" | head -n 3
# timestamp,type,product,lot,quantity,before_quantity,after_quantity,source
# 2024-01-12T15:30:00+00:00,receiving,Gala Apples,LOT-1,5,10,15,simulator

curl -s -X POST "http://localhost:8000/labels/markdown" \
  -H "Content-Type: application/json" \
  -d '{"default_codes":["FF101","FF102"]}'
# {"generated":[{"code":"FF101","path":"out/labels/FF101.pdf","url":"/static/labels/FF101.pdf"},...],
#  "count":2,"requested":["FF101","FF102"]}

# Append `?combined=true` to cache and reuse a single PDF containing all labels:
# curl -s -X POST "http://localhost:8000/labels/markdown?combined=true" \
#   -H "Content-Type: application/json" \
#   -d '{"default_codes":["FF101","FF102"]}' > combined.pdf
# Subsequent requests with the same codes reuse the cached file saved under `out/labels/labels-combined-<hash>.pdf` and exposed at `/static/labels/`.

curl -s "http://localhost:8000/out/labels/"
# {"labels":[{"filename":"FF101.pdf","path":"out/labels/FF101.pdf","url":"/static/labels/FF101.pdf",...},...],
#  "meta":{"count":2,"exists":true}}

curl -s -X POST "http://localhost:8000/recall/trigger" \
  -H "Content-Type: application/json" \
  -d '{"codes":["FF101","FF102"]}'
# {"items":[{"product":"Whole Milk","default_code":"FF102","lot":"LOT-FF102","quantity":8.0,"source_location":"Sales Floor","destination_location":"Quarantine"},...],
#  "meta":{"requested_codes":["FF101","FF102"],"requested_categories":[],"count":2}}

curl -s "http://localhost:8000/recall/quarantined"
# {"items":[{"product":"Whole Milk","default_code":"FF102","lot":"LOT-FF102","quantity":8.0}], "meta":{"count":1}}
```

The `/dashboard/flagged` view consumes `/metrics/last_sync` and surfaces a red banner if the most recent integration sync is more than 30 minutes old, helping surface stale inventory data during reviews.

If `out/events.jsonl` is missing or contains invalid JSON, the API returns
`{"events": [], "meta": {"exists": false, "error": "..."} }` with a 200
status. The `/at-risk` endpoint performs capability checks against Odoo
(`stock.lot` model and the `life_date` field) and falls back to informative
metadata (for example `{"reason": "no_life_date_field"}`) instead of erroring.

#### CSV exports

- `/export/flagged.csv` emits `default_code, product, lot, reason, outcome, suggested_qty, quantity, unit, price_markdown_pct, store, stores, category, notes`. Query parameters `store`, `category`, and `reason` mirror the JSON endpoint and the response defaults to UTF-8 with a BOM so Excel opens the file without manual encoding tweaks. When either `FOODFLOW_WEB_API_KEY` or `FOODFLOW_API_KEY` is set you must add `?api_key=...` (or include the same in your query parameters) or the service will reply with `401 Unauthorized`.
- `/export/events.csv` shares the JSON filters (`limit`, `type`, `since`) and produces `timestamp, type, product, lot, quantity, before_quantity, after_quantity, source`. The same optional API key guard applies, so append `api_key` when the environment variable is configured.

### Tests

Basic unit tests are located in the `tests/` directory. Run them with:

```bash
python -m pytest
```
