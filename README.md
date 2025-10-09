# FoodFlow

## Overview

FoodFlow is a developer sandbox that showcases how an Odoo-backed grocery retailer could seed, simulate, and monitor inventory data end to end. The repository includes:
- Inventory seeding utilities that provision demo products, lots, and stock levels in Odoo.
- A simulator that applies daily sales, expiry, and receiving patterns while logging events to JSONL and SQLite.
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

### Quick Make Targets

Common workflows are available as single-command Make targets:

```bash
make diagnose
# DB name: foodflow
# stock.lot present: true
# life_date present: true

make seed
# Seeded 105 products. Summary written to out/seed_summary.csv.

make simulate
# INFO 2024-01-10 12:00:00,000 INFO Simulator once run emitted 6 events

make simulate-start
# INFO 2024-01-10 12:00:00,000 INFO Simulator scheduler tick (Ctrl+C to stop)

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
- `make simulate` runs one simulator cycle, appending events to `out/events.jsonl` and persisting them to `out/foodflow.db`.
- `make simulate-start` launches the background scheduler for continuous simulation until you stop it.
- `make labels-demo` renders sample product labels to PDF under `out/labels`.
- `make web` starts the FastAPI reporting server so `/health` returns 200 once the app is ready.

`make simulate` and `make simulate-start` automatically migrate the local SQLite
database so events are stored in `out/foodflow.db`. The long running targets
(`simulate-start` and `web`) can be stopped with `Ctrl+C`.

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
# {"app":"FoodFlow reporting API","status":"ok","links":{"health":"/health","events_recent":"/events/recent","events":"/events","metrics_summary":"/metrics/summary","at_risk":"/at-risk","labels_markdown":"/labels/markdown","labels_index":"/out/labels/"},"docs":"See README.md for curl examples and Make targets."}

curl -s http://localhost:8000/health
# {"status":"ok"}

curl -s "http://localhost:8000/events/recent?limit=5"
# {"events":[{"ts":"...","type":"sell_down",...}], "meta":{"source":"jsonl","limit":5,"exists":true}}

curl -s "http://localhost:8000/events?limit=5"
# {"events":[{"ts":"...","type":"receiving",...}], "meta":{"source":"database","limit":5,"count":5}}

curl -s "http://localhost:8000/metrics/summary"
# {"events":{"total_events":120,"events_by_type":{"receiving":40,"sell_down":60,"daily_expiry":20}}, "meta":{"source":"database"}}

curl -s "http://localhost:8000/at-risk?days=3"
# {"items":[{"default_code":"FF101","product":"Whole Milk","lot":"LOT-FF101","days_left":2,"quantity":5.0}],
#  "meta":{"days":3,"count":1}}

curl -s -X POST "http://localhost:8000/labels/markdown" \
  -H "Content-Type: application/json" \
  -d '{"default_codes":["FF101","FF102"]}'
# {"labels":[{"default_code":"FF101","path":"out/labels/FF101.pdf",...}],
#  "meta":{"count":2,"output_dir":"out/labels"}}

curl -s "http://localhost:8000/out/labels/"
# {"labels":[{"filename":"FF101.pdf","path":"out/labels/FF101.pdf",...}], "meta":{"count":2,"exists":true}}
```

If `out/events.jsonl` is missing or contains invalid JSON, the API returns
`{"events": [], "meta": {"exists": false, "error": "..."} }` with a 200
status. The `/at-risk` endpoint performs capability checks against Odoo
(`stock.lot` model and the `life_date` field) and falls back to informative
metadata (for example `{"reason": "no_life_date_field"}`) instead of erroring.

### Tests

Basic unit tests are located in the `tests/` directory. Run them with:

```bash
python -m pytest
```
