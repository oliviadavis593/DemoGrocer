# FoodFlow

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
python scripts/seed_inventory.py
```

The script will create units of measure, product categories, approximately one
hundred products, traceable lots, and starting quantities in the "Backroom" and
"Sales Floor" locations. A summary of the seeded data is written to
`out/seed_summary.csv`.

### Reporting API at a Glance

Once the simulator has been seeded and is generating activity, start the
lightweight reporting app located in `apps/web`. The service loads environment
variables from `.env`, authenticates to Odoo once on boot, and exposes JSON
endpoints suitable for scripting or quick spot checks:

```bash
# Install the minimal dependencies for the web layer
python -m pip install -r requirements.txt

# Launch the API
PYTHONPATH=. python3 -m apps.web.main
```

Example requests:

```bash
curl -s http://localhost:8000/health
# {"status":"ok"}

curl -s "http://localhost:8000/events/recent?limit=5"
# {"events":[{"ts":"...","type":"sell_down",...}], "meta":{"source":"jsonl","limit":5,"exists":true}}

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
