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

### Tests

Basic unit tests are located in the `tests/` directory. Run them with:

```bash
python -m pytest
```
