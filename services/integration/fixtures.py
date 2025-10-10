"""Static inventory fixtures used for demos and offline simulations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from typing import Iterable, List, Sequence

from services.simulator.inventory import InventorySnapshot, QuantRecord

_CATEGORY_CONFIG: dict[str, dict[str, object]] = {
    "Produce": {
        "supplier": "River Valley Farms Cooperative",
        "shelf_life_days": 6,
        "perishable": True,
        "demand_profile": "high",
    },
    "Dairy": {
        "supplier": "Sunrise Dairy Collective",
        "shelf_life_days": 12,
        "perishable": True,
        "demand_profile": "steady",
    },
    "Meat": {
        "supplier": "Heritage Meats Ltd.",
        "shelf_life_days": 10,
        "perishable": True,
        "demand_profile": "steady",
    },
    "Deli": {
        "supplier": "Market Square Prepared Foods",
        "shelf_life_days": 5,
        "perishable": True,
        "demand_profile": "steady",
    },
    "Bakery": {
        "supplier": "Golden Crust Bakers",
        "shelf_life_days": 4,
        "perishable": True,
        "demand_profile": "high",
    },
    "Frozen": {
        "supplier": "Polar Peak Foods",
        "shelf_life_days": 150,
        "perishable": False,
        "demand_profile": "steady",
    },
    "Center Store": {
        "supplier": "Pantry Partners Distribution",
        "shelf_life_days": 180,
        "perishable": False,
        "demand_profile": "low",
    },
}

_DEFAULT_CATEGORY_CONFIG: dict[str, object] = {
    "supplier": "FoodFlow Wholesale",
    "shelf_life_days": 90,
    "perishable": False,
    "demand_profile": "steady",
}


@dataclass(frozen=True)
class InventoryFixture:
    """Representation of a single inventory item for offline demos."""

    default_code: str
    product: str
    category: str
    supplier: str
    uom: str
    list_price: float
    backroom_qty: float
    sales_floor_qty: float
    shelf_life_days: int
    life_date: date
    perishable: bool
    demand_profile: str

    @property
    def stock_on_hand(self) -> float:
        return round(self.backroom_qty + self.sales_floor_qty, 4)

    @property
    def lot_name(self) -> str:
        return f"LOT-{self.default_code}"

    def to_dict(self) -> dict[str, object]:
        return {
            "default_code": self.default_code,
            "product": self.product,
            "category": self.category,
            "supplier": self.supplier,
            "uom": self.uom,
            "list_price": self.list_price,
            "backroom_qty": self.backroom_qty,
            "sales_floor_qty": self.sales_floor_qty,
            "stock_on_hand": self.stock_on_hand,
            "shelf_life_days": self.shelf_life_days,
            "life_date": self.life_date.isoformat(),
            "perishable": self.perishable,
            "demand_profile": self.demand_profile,
        }

    def to_quant_record(
        self,
        *,
        quant_id: int,
        product_id: int,
        lot_id: int,
    ) -> QuantRecord:
        """Convert the fixture into a QuantRecord for simulator usage."""

        return QuantRecord(
            id=quant_id,
            product_id=product_id,
            product_name=self.product,
            default_code=self.default_code,
            category=self.category,
            quantity=self.stock_on_hand,
            lot_id=lot_id,
            lot_name=self.lot_name,
            life_date=self.life_date,
        )


def load_inventory_fixtures(base_date: date | None = None) -> List[InventoryFixture]:
    """Generate inventory fixtures with stock, shelf life, and supplier data."""

    catalog = _load_product_catalog()
    if not catalog:
        return []

    today = base_date or date.today()
    fixtures: List[InventoryFixture] = []

    for index, entry in enumerate(catalog):
        product = str(entry.get("name") or f"Product {index + 1}")
        category = str(entry.get("category") or "Unknown")
        default_code = str(entry.get("default_code") or f"SKU-{index + 1}")

        config = _CATEGORY_CONFIG.get(category, _DEFAULT_CATEGORY_CONFIG)
        supplier = str(config.get("supplier") or _DEFAULT_CATEGORY_CONFIG["supplier"])
        perishable = bool(config.get("perishable", False))
        demand_profile = str(config.get("demand_profile") or "steady")

        base_shelf_life = int(config.get("shelf_life_days") or _DEFAULT_CATEGORY_CONFIG["shelf_life_days"])
        shelf_life_days = max(3, base_shelf_life + ((index % 4) - 1))
        life_date = today + timedelta(days=shelf_life_days)

        backroom_qty, sales_floor_qty = _derive_quantities(perishable, demand_profile, index)
        uom = str(entry.get("uom") or "EA")

        list_price_raw = entry.get("list_price")
        try:
            list_price = float(list_price_raw) if list_price_raw is not None else 0.0
        except (TypeError, ValueError):
            list_price = 0.0

        fixtures.append(
            InventoryFixture(
                default_code=default_code,
                product=product,
                category=category,
                supplier=supplier,
                uom=uom,
                list_price=list_price,
                backroom_qty=backroom_qty,
                sales_floor_qty=sales_floor_qty,
                shelf_life_days=shelf_life_days,
                life_date=life_date,
                perishable=perishable,
                demand_profile=demand_profile,
            )
        )

    return fixtures


def fixtures_to_snapshot(fixtures: Sequence[InventoryFixture]) -> InventorySnapshot:
    """Convert fixtures into an InventorySnapshot for downstream consumers."""

    quants: List[QuantRecord] = []
    for offset, fixture in enumerate(fixtures, start=1):
        quant_id = offset
        product_id = 1000 + offset
        lot_id = 5000 + offset
        quants.append(
            fixture.to_quant_record(
                quant_id=quant_id,
                product_id=product_id,
                lot_id=lot_id,
            )
        )
    return InventorySnapshot(quants)


def fixtures_as_dicts(fixtures: Iterable[InventoryFixture]) -> List[dict[str, object]]:
    """Serialize fixtures to dictionaries suitable for JSON output."""

    return [fixture.to_dict() for fixture in fixtures]


@lru_cache(maxsize=1)
def _load_product_catalog() -> Sequence[dict[str, object]]:
    try:
        from scripts.seed_inventory import _product_catalog  # type: ignore
    except Exception:
        return []
    try:
        return _product_catalog()
    except Exception:
        return []


def _derive_quantities(perishable: bool, demand_profile: str, index: int) -> tuple[float, float]:
    """Compute representative backroom and sales floor quantities."""

    if perishable:
        base_backroom = 16.0
        base_sales = 7.0
    else:
        base_backroom = 26.0
        base_sales = 10.0

    if demand_profile == "high":
        base_backroom += 4.0
        base_sales += 3.0
    elif demand_profile == "low":
        base_backroom -= 6.0
        base_sales -= 3.0

    backroom = max(1.0, round(base_backroom - (index % 5) * 1.4, 2))
    sales_floor = max(0.5, round(base_sales - (index % 3) * 0.9, 2))
    return backroom, sales_floor


__all__ = [
    "InventoryFixture",
    "fixtures_as_dicts",
    "fixtures_to_snapshot",
    "load_inventory_fixtures",
]
