# Inventory Seeding Concepts

This note unpacks the terms and calculations that show up in the seeding
scripts so you can tie the numbers back to what you see inside Odoo.

## Key Definitions
- **Unit of Measure (UoM)** – How a product is tracked (each, pound, ounce,
  case). The seeder registers custom UoMs under `FoodFlow Units` and
  `FoodFlow Weight` so every SKU has a matching sell and purchase unit.
- **List Price** – The retail price customers pay in Odoo (`product.template`
  `list_price`). We source this directly from the catalog data in
  `_product_catalog`.
- **Unit Cost** – A wholesale cost approximation per SKU. We compute it by
  multiplying the list price by the category’s `cost_factor` (e.g. Produce ≈
  0.58 of retail), then apply a tiny variance so items in the same family are
  not identical.
- **Average Cost / Standard Price** – The value Odoo uses for inventory
  valuation (`product.template.standard_price`). A small positive/negative
  adjustment is applied to the unit cost for realism, then stored as both
  `average_cost` in the catalog and `standard_price` on the product template.
- **Quantity on Hand** – Total units we seed into stock. Each category profile
  defines a realistic baseline (`base_qty`) plus an `item_offset` step pattern
  so related items fluctuate around that base.
- **Backroom vs Sales Floor** – The script splits the on-hand quantity between
  “Backroom” and “Sales Floor” locations using the category’s
  `backroom_ratio`. We clamp the ratio so the sales floor never drops below 20%
  of the total and the backroom never exceeds 80%, keeping the sum equal to
  `quantity_on_hand`.
- **Inventory Mode** – An Odoo context flag that lets us adjust quants the same
  way the stock adjustment wizard would. By writing `quantity` while
  `inventory_mode=True`, we ensure Odoo recalculates its on-hand totals and
  valuation immediately.

## How Quantities Are Built
For every SKU we:
1. Look up the category profile (see `CATEGORY_PROFILES` in
   `scripts/seed_inventory.py`) to fetch cost factors, quantity baselines, and
   ratio bounds.
2. Generate a raw quantity using `base_qty + item_offset × qty_step`, then clamp
   to the category minimum.
3. Scale the backroom/sales split via the bounded ratio. All fallback branches
   keep the math balanced so `backroom_qty + sales_floor_qty == quantity_on_hand`.
4. Round to two decimals before writing to Odoo.

## Why the Numbers Match Odoo
- We pre-compute all catalog fields in `_product_catalog` and export them to
  `out/seed_summary.csv` (so you can inspect the exact totals outside of Odoo).
- The seeding run sets the same values directly on each product template (prices
  and costs) and on each `stock.quant` (quantities) under inventory mode.
- Because Odoo recalculates on-hand and valuation from those fields, the UI will
  always match the CSV.

If you need to audit a specific item, open `out/seed_summary.csv` and compare the
row with the product view in Odoo—the unit cost, quantity on hand, and valuation
will line up exactly.***
