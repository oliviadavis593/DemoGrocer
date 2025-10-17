"""Seed the FoodFlow demo inventory into Odoo via XML-RPC."""
from __future__ import annotations

import sys
import pathlib

from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from packages.odoo_client import OdooClient, OdooClientError


UOM_CATEGORY_DEFINITIONS = {
    "FoodFlow Units": {"name": "FoodFlow Units"},
    "FoodFlow Weight": {"name": "FoodFlow Weight"},
}

UOM_DEFINITIONS = [
    {
        "name": "EA",
        "category": "FoodFlow Units",
        "uom_type": "reference",
        "factor": 1.0,
        "rounding": 1.0,
    },
    {
        "name": "CASE",
        "category": "FoodFlow Units",
        "uom_type": "bigger",
        "factor_inv": 12.0,
        "rounding": 1.0,
    },
    {
        "name": "LB",
        "category": "FoodFlow Weight",
        "uom_type": "reference",
        "factor": 1.0,
        "rounding": 0.01,
    },
    {
        "name": "OZ",
        "category": "FoodFlow Weight",
        "uom_type": "smaller",
        "factor": 16.0,
        "rounding": 0.01,
    },
]

PRODUCT_CATEGORY_NAMES = [
    "Produce",
    "Dairy",
    "Meat",
    "Deli",
    "Bakery",
    "Center Store",
    "Frozen",
]

CATEGORY_PROFILES: Dict[str, Dict[str, float]] = {
    "Produce": {
        "cost_factor": 0.58,
        "avg_cost_variance": 0.012,
        "base_qty": 26.0,
        "qty_step": 2.5,
        "qty_cycle": 5,
        "backroom_ratio": 0.54,
        "backroom_variance": 0.05,
        "min_qty": 18.0,
    },
    "Dairy": {
        "cost_factor": 0.62,
        "avg_cost_variance": 0.01,
        "base_qty": 38.0,
        "qty_step": 2.5,
        "qty_cycle": 6,
        "backroom_ratio": 0.6,
        "backroom_variance": 0.04,
        "min_qty": 28.0,
    },
    "Meat": {
        "cost_factor": 0.68,
        "avg_cost_variance": 0.018,
        "base_qty": 30.0,
        "qty_step": 3.0,
        "qty_cycle": 5,
        "backroom_ratio": 0.52,
        "backroom_variance": 0.03,
        "min_qty": 22.0,
    },
    "Deli": {
        "cost_factor": 0.6,
        "avg_cost_variance": 0.015,
        "base_qty": 24.0,
        "qty_step": 2.0,
        "qty_cycle": 5,
        "backroom_ratio": 0.5,
        "backroom_variance": 0.04,
        "min_qty": 18.0,
    },
    "Bakery": {
        "cost_factor": 0.52,
        "avg_cost_variance": 0.02,
        "base_qty": 28.0,
        "qty_step": 2.2,
        "qty_cycle": 5,
        "backroom_ratio": 0.42,
        "backroom_variance": 0.04,
        "min_qty": 18.0,
    },
    "Center Store": {
        "cost_factor": 0.57,
        "avg_cost_variance": 0.012,
        "base_qty": 55.0,
        "qty_step": 4.0,
        "qty_cycle": 6,
        "backroom_ratio": 0.66,
        "backroom_variance": 0.03,
        "min_qty": 36.0,
    },
    "Frozen": {
        "cost_factor": 0.6,
        "avg_cost_variance": 0.015,
        "base_qty": 44.0,
        "qty_step": 2.8,
        "qty_cycle": 5,
        "backroom_ratio": 0.68,
        "backroom_variance": 0.025,
        "min_qty": 28.0,
    },
}

DEFAULT_CATEGORY_PROFILE: Dict[str, float] = {
    "cost_factor": 0.6,
    "avg_cost_variance": 0.015,
    "base_qty": 32.0,
    "qty_step": 3.0,
    "qty_cycle": 5,
    "backroom_ratio": 0.6,
    "backroom_variance": 0.035,
    "min_qty": 20.0,
}

UOM_QUANTITY_FACTORS: Dict[str, float] = {
    "EA": 1.0,
    "LB": 1.0,
    "OZ": 1.0,
    "CASE": 0.4,
}


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _product_catalog() -> List[Dict[str, object]]:
    data: Dict[str, Sequence[Sequence[object]]] = {
        "Produce": [
            ("Gala Apples", "LB", 2.99),
            ("Honeycrisp Apples", "LB", 3.49),
            ("Bananas", "LB", 0.79),
            ("Lemons", "EA", 0.69),
            ("Limes", "EA", 0.59),
            ("Navel Oranges", "EA", 1.09),
            ("Bartlett Pears", "LB", 2.59),
            ("Strawberries 1lb", "EA", 3.99),
            ("Blueberries Pint", "EA", 4.49),
            ("Romaine Lettuce", "EA", 1.99),
            ("Baby Spinach", "LB", 4.99),
            ("Broccoli Crowns", "LB", 1.89),
            ("Carrots 2lb", "EA", 2.29),
            ("Vine Tomatoes", "LB", 2.49),
            ("Russet Potatoes 5lb", "EA", 3.99),
        ],
        "Dairy": [
            ("Whole Milk Gallon", "EA", 3.99),
            ("2% Milk Half Gallon", "EA", 2.69),
            ("Greek Yogurt 32oz", "EA", 4.99),
            ("Cheddar Cheese Block", "LB", 6.99),
            ("Mozzarella Shredded", "LB", 5.49),
            ("Salted Butter 4pk", "EA", 3.79),
            ("Large Eggs 12ct", "EA", 2.99),
            ("Cottage Cheese", "EA", 3.49),
            ("Sour Cream", "EA", 1.99),
            ("Whipping Cream Pint", "EA", 2.89),
            ("Parmesan Cheese", "LB", 8.49),
            ("String Cheese 12ct", "EA", 4.59),
            ("Chocolate Milk Quart", "EA", 2.79),
            ("Vanilla Yogurt Cups 6ct", "CASE", 5.49),
            ("Almond Milk Unsweetened", "EA", 3.69),
        ],
        "Meat": [
            ("Chicken Breast Boneless", "LB", 5.49),
            ("Ground Beef 85%", "LB", 4.99),
            ("Pork Chops", "LB", 4.79),
            ("Salmon Fillet", "LB", 9.99),
            ("Turkey Breast", "LB", 5.99),
            ("Bacon Thick Cut", "LB", 6.49),
            ("Italian Sausage", "LB", 4.59),
            ("Beef Sirloin", "LB", 7.99),
            ("Chicken Thighs", "LB", 3.99),
            ("Shrimp 16-20", "LB", 11.99),
            ("Ham Steak", "LB", 5.49),
            ("Beef Brisket", "LB", 6.99),
            ("Lamb Chops", "LB", 12.99),
            ("Tilapia Fillets", "LB", 4.99),
            ("Meatballs Fresh", "LB", 5.59),
        ],
        "Deli": [
            ("Potato Salad", "LB", 4.49),
            ("Coleslaw", "LB", 3.99),
            ("Turkey Sandwich", "EA", 6.49),
            ("Veggie Wrap", "EA", 6.29),
            ("Chicken Salad", "LB", 7.49),
            ("Hummus 16oz", "EA", 4.29),
            ("Quinoa Salad", "LB", 7.99),
            ("Pasta Salad", "LB", 6.49),
            ("Roast Beef Sandwich", "EA", 6.99),
            ("Caprese Salad", "LB", 7.29),
            ("Greek Salad", "LB", 6.99),
            ("Spinach Artichoke Dip", "LB", 5.49),
            ("Fruit Cup", "EA", 3.49),
            ("Sushi Combo", "EA", 9.99),
            ("Cheese Tray Small", "CASE", 24.99),
        ],
        "Bakery": [
            ("Sourdough Loaf", "EA", 4.49),
            ("French Baguette", "EA", 2.49),
            ("Whole Wheat Bread", "EA", 3.59),
            ("Butter Croissant 4pk", "EA", 4.99),
            ("Blueberry Muffins 4pk", "EA", 5.49),
            ("Chocolate Cake", "EA", 14.99),
            ("Apple Pie", "EA", 9.99),
            ("Bagels 6ct", "EA", 4.29),
            ("Cinnamon Rolls 4ct", "EA", 5.99),
            ("Dinner Rolls 12ct", "CASE", 6.49),
            ("Rosemary Focaccia", "EA", 5.49),
            ("Banana Bread", "EA", 6.29),
            ("Gluten Free Bread", "EA", 7.49),
            ("Chocolate Donuts 6ct", "EA", 5.29),
            ("Fudge Brownies 6ct", "EA", 6.49),
        ],
        "Center Store": [
            ("Extra Virgin Olive Oil 1L", "EA", 8.99),
            ("Canned Tomatoes 12ct", "CASE", 14.99),
            ("Black Beans 4pk", "CASE", 5.49),
            ("Peanut Butter 16oz", "EA", 3.49),
            ("Spaghetti Pasta", "EA", 1.79),
            ("Jasmine Rice 5lb", "LB", 6.99),
            ("Corn Flakes Cereal", "EA", 3.99),
            ("Granola Bars 12ct", "CASE", 8.49),
            ("Whole Almonds 8oz", "OZ", 6.29),
            ("Tortilla Chips", "EA", 3.49),
            ("Salsa 16oz", "EA", 2.99),
            ("Chicken Broth 6ct", "CASE", 7.99),
            ("Coffee Beans 12oz", "OZ", 9.49),
            ("Green Tea Bags 20ct", "EA", 3.99),
            ("Cooking Spray", "EA", 3.49),
        ],
        "Frozen": [
            ("Frozen Peas 16oz", "EA", 1.99),
            ("Frozen Corn 16oz", "EA", 1.99),
            ("Frozen Mixed Berries", "EA", 4.99),
            ("Vanilla Ice Cream", "EA", 5.49),
            ("Frozen Pepperoni Pizza", "EA", 6.99),
            ("Frozen Waffles", "EA", 3.29),
            ("Frozen Spinach", "EA", 2.49),
            ("Frozen Shrimp", "LB", 12.99),
            ("Frozen Broccoli", "EA", 2.79),
            ("Frozen Lasagna", "EA", 9.49),
            ("Frozen Meatballs", "EA", 7.49),
            ("Frozen French Fries", "EA", 2.99),
            ("Frozen Edamame", "EA", 3.49),
            ("Frozen Chicken Nuggets", "EA", 6.49),
            ("Frozen Veggie Burgers", "EA", 5.99),
        ],
    }

    products: List[Dict[str, object]] = []
    sku_index = 101
    for category, items in data.items():
        profile = CATEGORY_PROFILES.get(category, DEFAULT_CATEGORY_PROFILE)
        cost_factor = float(profile.get("cost_factor", DEFAULT_CATEGORY_PROFILE["cost_factor"]))
        avg_cost_variance = float(
            profile.get("avg_cost_variance", DEFAULT_CATEGORY_PROFILE["avg_cost_variance"])
        )
        base_qty = float(profile.get("base_qty", DEFAULT_CATEGORY_PROFILE["base_qty"]))
        qty_step = float(profile.get("qty_step", DEFAULT_CATEGORY_PROFILE["qty_step"]))
        qty_cycle = max(int(profile.get("qty_cycle", DEFAULT_CATEGORY_PROFILE["qty_cycle"])), 1)
        min_qty = float(profile.get("min_qty", DEFAULT_CATEGORY_PROFILE["min_qty"]))
        backroom_ratio_base = float(
            profile.get("backroom_ratio", DEFAULT_CATEGORY_PROFILE["backroom_ratio"])
        )
        backroom_variance = float(
            profile.get("backroom_variance", DEFAULT_CATEGORY_PROFILE["backroom_variance"])
        )
        for item_offset, (name, uom, price) in enumerate(items):
            default_code = f"FF{sku_index:03d}"
            sku_index += 1

            list_price = round(float(price), 2)
            qty_position = item_offset % qty_cycle
            raw_quantity = base_qty + qty_position * qty_step
            raw_quantity = max(raw_quantity, min_qty)
            uom_factor = UOM_QUANTITY_FACTORS.get(uom, 1.0)
            quantity_on_hand_raw = raw_quantity * uom_factor

            ratio_delta = ((item_offset % 3) - 1) * backroom_variance
            backroom_ratio = max(0.2, min(0.8, backroom_ratio_base + ratio_delta))
            backroom_qty_raw = quantity_on_hand_raw * backroom_ratio
            sales_floor_qty_raw = quantity_on_hand_raw - backroom_qty_raw
            min_sales = quantity_on_hand_raw * 0.2
            if sales_floor_qty_raw < min_sales:
                sales_floor_qty_raw = min_sales
                backroom_qty_raw = quantity_on_hand_raw - sales_floor_qty_raw

            backroom_qty = round(backroom_qty_raw, 2)
            sales_floor_qty = round(sales_floor_qty_raw, 2)
            quantity_on_hand = round(backroom_qty + sales_floor_qty, 2)
            if sales_floor_qty <= 0:
                sales_floor_qty = round(max(quantity_on_hand_raw * 0.25, 1.0), 2)
                backroom_qty = round(max(quantity_on_hand_raw - sales_floor_qty, 0.5), 2)
                quantity_on_hand = round(backroom_qty + sales_floor_qty, 2)
            if backroom_qty <= 0:
                backroom_qty = round(max(quantity_on_hand_raw * 0.6, 1.0), 2)
                sales_floor_qty = round(max(quantity_on_hand_raw - backroom_qty, 1.0), 2)
                quantity_on_hand = round(backroom_qty + sales_floor_qty, 2)
            if quantity_on_hand <= 0:
                fallback_total = max(quantity_on_hand_raw, min_qty)
                backroom_qty = round(max(fallback_total * 0.6, 1.0), 2)
                sales_floor_qty = round(max(fallback_total - backroom_qty, 1.0), 2)
                quantity_on_hand = round(backroom_qty + sales_floor_qty, 2)

            unit_cost = round(list_price * cost_factor, 4)
            avg_adjustment = ((item_offset % 4) - 1.5) * avg_cost_variance
            average_cost = round(max(0.01, unit_cost * (1.0 + avg_adjustment)), 4)
            standard_price = average_cost

            products.append(
                {
                    "name": name,
                    "category": category,
                    "uom": uom,
                    "default_code": default_code,
                    "list_price": list_price,
                    "standard_price": standard_price,
                    "unit_cost": unit_cost,
                    "average_cost": average_cost,
                    "quantity_on_hand": quantity_on_hand,
                    "backroom_qty": backroom_qty,
                    "sales_floor_qty": sales_floor_qty,
                }
            )
    return products


@dataclass
class SeedResult:
    default_code: str
    template_id: int
    product_id: int
    lot_id: int
    backroom_qty: float
    sales_floor_qty: float
    quantity_on_hand: float
    unit_cost: float
    average_cost: float
    list_price: float


class InventorySeeder:
    """Seed inventory data into Odoo in an idempotent fashion."""

    def __init__(self, client: OdooClient) -> None:
        self.client = client
        self.uom_categories: Dict[str, int] = {}
        self.uoms: Dict[str, int] = {}
        self.product_categories: Dict[str, int] = {}
        self.locations: Dict[str, int] = {}

    # Public API -----------------------------------------------------------------
    def run(self) -> List[SeedResult]:
        self._assert_models()
        self._ensure_uom_categories()
        self._ensure_uoms()
        self._ensure_product_categories()
        self._ensure_locations()

        results: List[SeedResult] = []
        products = _product_catalog()
        for index, product in enumerate(products):
                list_price = round(_coerce_float(product.get("list_price")), 2)
                standard_price = _coerce_float(product.get("standard_price"))
                unit_cost = _coerce_float(product.get("unit_cost"), standard_price)
                average_cost = _coerce_float(product.get("average_cost"), standard_price)
                fallback_cost = round(max(list_price * 0.6, 0.01), 4)
                if unit_cost <= 0:
                    unit_cost = standard_price if standard_price > 0 else fallback_cost
                if average_cost <= 0:
                    average_cost = unit_cost if unit_cost > 0 else fallback_cost
                unit_cost = round(unit_cost, 4)
                average_cost = round(average_cost, 4)
                product["list_price"] = list_price
                product["unit_cost"] = unit_cost
                product["average_cost"] = average_cost
                product["standard_price"] = average_cost

                backroom_qty = _coerce_float(product.get("backroom_qty"))
                sales_floor_qty = _coerce_float(product.get("sales_floor_qty"))
                if backroom_qty <= 0 or sales_floor_qty <= 0:
                    backroom_qty, sales_floor_qty = self._fallback_quantities(index)
                total_quantity = backroom_qty + sales_floor_qty
                if total_quantity <= 0:
                    backroom_qty, sales_floor_qty = self._fallback_quantities(index)
                    total_quantity = backroom_qty + sales_floor_qty
                catalog_quantity = _coerce_float(product.get("quantity_on_hand"))
                if catalog_quantity > 0 and total_quantity > 0:
                    scale = catalog_quantity / total_quantity
                    backroom_qty *= scale
                    sales_floor_qty *= scale
                    total_quantity = backroom_qty + sales_floor_qty

                backroom_qty = round(backroom_qty, 4)
                sales_floor_qty = round(sales_floor_qty, 4)
                quantity_on_hand = round(backroom_qty + sales_floor_qty, 4)
                if quantity_on_hand <= 0:
                    backroom_qty, sales_floor_qty = self._fallback_quantities(index)
                    backroom_qty = round(backroom_qty, 4)
                    sales_floor_qty = round(sales_floor_qty, 4)
                    quantity_on_hand = round(backroom_qty + sales_floor_qty, 4)

                product["backroom_qty"] = backroom_qty
                product["sales_floor_qty"] = sales_floor_qty
                product["quantity_on_hand"] = quantity_on_hand

                template_id = self._upsert_product_template(product)
                product_id = self._get_single_variant(template_id)
                lot_id = self._ensure_lot(product_id, product["default_code"], index)
                self._ensure_quant(product_id, lot_id, self.locations["Backroom"], backroom_qty)
                self._ensure_quant(product_id, lot_id, self.locations["Sales Floor"], sales_floor_qty)
                results.append(
                    SeedResult(
                        default_code=str(product["default_code"]),
                        template_id=template_id,
                        product_id=product_id,
                        lot_id=lot_id,
                        backroom_qty=backroom_qty,
                        sales_floor_qty=sales_floor_qty,
                        quantity_on_hand=quantity_on_hand,
                        unit_cost=unit_cost,
                        average_cost=average_cost,
                        list_price=list_price,
                    )
                )
        return results

    def _assert_models(self) -> None:
        lot_model = self.client.search_read(
            "ir.model",
            [("model", "=", "stock.lot")],
            ["id"],
            limit=1,
        )
        if not lot_model:
            raise OdooClientError(
                "Odoo Inventory app not installed in this DB (missing model 'stock.lot'). "
                "Install Apps → Inventory."
            )

    # Setup helpers --------------------------------------------------------------
    def _ensure_uom_categories(self) -> None:
        for name, values in UOM_CATEGORY_DEFINITIONS.items():
            record = self._upsert_single(
                "uom.category",
                domain=[("name", "=", name)],
                values=values,
            )
            self.uom_categories[name] = record

    def _ensure_uoms(self) -> None:
        for uom in UOM_DEFINITIONS:
            category_id = self.uom_categories[uom["category"]]
            domain = [("name", "=", uom["name"]), ("category_id", "=", category_id)]
            values = {
                "name": uom["name"],
                "uom_type": uom["uom_type"],
                "rounding": uom["rounding"],
                "active": True,
                "category_id": category_id,
            }
            if "factor" in uom:
                values["factor"] = uom["factor"]
            if "factor_inv" in uom:
                values["factor_inv"] = uom["factor_inv"]
            uom_id = self._upsert_single("uom.uom", domain=domain, values=values)
            self.uoms[uom["name"]] = uom_id

    def _ensure_product_categories(self) -> None:
        for name in PRODUCT_CATEGORY_NAMES:
            category_id = self._upsert_single(
                "product.category",
                domain=[("name", "=", name)],
                values={"name": name},
            )
            self.product_categories[name] = category_id

    def _ensure_locations(self) -> None:
        parent = self._get_default_stock_location()
        for name in ("Backroom", "Sales Floor"):
            values = {"name": name, "usage": "internal"}
            if parent:
                values["location_id"] = parent
            location_id = self._upsert_single(
                "stock.location",
                domain=[("name", "=", name)],
                values=values,
            )
            self.locations[name] = location_id

    def _fallback_quantities(self, index: int) -> tuple[float, float]:
        backroom = max(4.0, 20.0 - (index % 5) * 1.8)
        sales_floor = max(2.0, 10.0 - (index % 3) * 1.2)
        return float(backroom), float(sales_floor)

    # Entity helpers -------------------------------------------------------------
    def _upsert_product_template(self, product: Dict[str, object]) -> int:
        domain = [("default_code", "=", product["default_code"])]
        values = {
            "name": product["name"],
            "default_code": product["default_code"],
            "categ_id": self.product_categories[product["category"]],
            "uom_id": self.uoms[product["uom"]],
            "uom_po_id": self.uoms[product["uom"]],
            "type": "product",
            "tracking": "lot",
            "list_price": product["list_price"],
            "standard_price": product["standard_price"],
            "sale_ok": True,
            "purchase_ok": True,
        }
        return self._upsert_single("product.template", domain=domain, values=values)

    def _get_single_variant(self, template_id: int) -> int:
        variants = self.client.search_read(
            "product.product",
            domain=[("product_tmpl_id", "=", template_id)],
            fields=["id"],
            limit=1,
        )
        if not variants:
            raise OdooClientError(
                f"No variants found for product template {template_id}. "
                "Check if product variants are generated."
            )
        return int(variants[0]["id"])

    def _ensure_lot(self, product_id: int, code: str, index: int) -> int:
        lot_name = f"LOT-{code}"
        values = {
            "name": lot_name,
            "product_id": product_id,
        }
        has_life = self.client.search_read(
            "ir.model.fields",
            [("model", "=", "stock.lot"), ("name", "=", "expiration_date")],
            ["id"],
            limit=1,
        )
        if has_life:
            life_date = date.today() + timedelta(days=30 + (index % 60))
            values["expiration_date"] = life_date.isoformat()
        return self._upsert_single(
            "stock.lot",
            domain=[("name", "=", lot_name), ("product_id", "=", product_id)],
            values=values,
        )

    def _ensure_quant(
        self,
        product_id: int,
        lot_id: Optional[int],
        location_id: int,
        quantity: float,
    ) -> int:
        context = {
            "inventory_mode": True,
            "inventory_adjustment_name": "FoodFlow Seed",
        }
        domain = [
            ("product_id", "=", product_id),
            ("location_id", "=", location_id),
        ]
        if lot_id is not None:
            domain.append(("lot_id", "=", lot_id))
        existing = self.client.search_read(
            "stock.quant",
            domain=domain,
            fields=["id"],
            limit=1,
        )
        values: Dict[str, object] = {
            "quantity": float(quantity),
            "reserved_quantity": 0.0,
            "inventory_quantity": 0.0,
        }
        if existing:
            record_id = int(existing[0]["id"])
            self.client.write("stock.quant", record_id, values, context=context)
            return record_id

        create_values: Dict[str, object] = {
            "product_id": product_id,
            "location_id": location_id,
            "quantity": float(quantity),
            "reserved_quantity": 0.0,
            "inventory_quantity": 0.0,
        }
        if lot_id is not None:
            create_values["lot_id"] = lot_id
        record_id = self.client.create("stock.quant", create_values, context=context)
        return record_id

    # Generic helpers ------------------------------------------------------------
    def _upsert_single(self, model: str, domain: Sequence[object], values: Dict[str, object]) -> int:
        records = self.client.search_read(model, domain=domain, fields=["id"], limit=1)
        if records:
            record_id = int(records[0]["id"])
            self.client.write(model, record_id, values)
            return record_id
        return self.client.create(model, values)

    def _get_default_stock_location(self) -> int:
        records = self.client.search_read(
            "stock.location",
            domain=[("usage", "=", "internal")],
            fields=["id"],
            limit=1,
        )
        return int(records[0]["id"]) if records else 0



def write_summary(results: Iterable[SeedResult], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "default_code",
                "product_template_id",
                "product_variant_id",
                "lot_id",
                "list_price",
                "unit_cost",
                "average_cost",
                "quantity_on_hand",
                "backroom_qty",
                "sales_floor_qty",
            ]
        )
        for result in results:
            writer.writerow(
                [
                    result.default_code,
                    result.template_id,
                    result.product_id,
                    result.lot_id,
                    f"{result.list_price:.2f}",
                    f"{result.unit_cost:.4f}",
                    f"{result.average_cost:.4f}",
                    f"{result.quantity_on_hand:.4f}",
                    f"{result.backroom_qty:.4f}",
                    f"{result.sales_floor_qty:.4f}",
                ]
            )


def main() -> None:
    client = OdooClient()
    client.authenticate()
    seeder = InventorySeeder(client)
    results = seeder.run()
    output_path = Path("out/seed_summary.csv")
    write_summary(results, output_path)
    print(f"Seeded {len(results)} products. Summary written to {output_path}.")


if __name__ == "__main__":
    try:
        main()
    except OdooClientError as exc:
        raise SystemExit(f"Failed to seed inventory: {exc}")
