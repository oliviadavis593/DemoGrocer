"""Inventory data access for simulator jobs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from packages.odoo_client import OdooClient


@dataclass
class QuantRecord:
    """Represents a stock quant with resolved relationships."""

    id: int
    product_id: int
    product_name: str
    category: str
    quantity: float
    lot_id: Optional[int]
    lot_name: Optional[str]
    life_date: Optional[date]


class InventorySnapshot:
    """In-memory representation of quant inventory."""

    def __init__(self, quants: Iterable[QuantRecord]) -> None:
        self._quants: Dict[int, QuantRecord] = {quant.id: quant for quant in quants}

    def quants(self) -> Iterable[QuantRecord]:
        return list(self._quants.values())

    def get(self, quant_id: int) -> Optional[QuantRecord]:
        return self._quants.get(quant_id)

    def update_quantity(self, quant_id: int, quantity: float) -> None:
        if quant_id in self._quants:
            self._quants[quant_id].quantity = quantity


class InventoryRepository:
    """Fetch and hydrate inventory information from Odoo."""

    def __init__(self, client: OdooClient) -> None:
        self.client = client

    def load_snapshot(self) -> InventorySnapshot:
        quant_records = self.client.search_read(
            "stock.quant",
            domain=[],
            fields=["id", "product_id", "quantity", "lot_id"],
        )
        quants: List[QuantRecord] = []
        product_ids = _collect_relational_ids(quant_records, "product_id")
        lot_ids = _collect_relational_ids(quant_records, "lot_id")

        products = self._load_products(product_ids)
        lots = self._load_lots(lot_ids)

        for record in quant_records:
            quant_id = int(record["id"])
            product_id = _resolve_relational_id(record.get("product_id"))
            product = products.get(product_id)
            if not product:
                continue
            category = product.get("category", "Unknown")
            lot_id = _resolve_relational_id(record.get("lot_id"))
            lot = lots.get(lot_id) if lot_id is not None else None
            quants.append(
                QuantRecord(
                    id=quant_id,
                    product_id=product_id,
                    product_name=product.get("name", f"Product {product_id}"),
                    category=category,
                    quantity=float(record.get("quantity", 0.0) or 0.0),
                    lot_id=lot_id,
                    lot_name=lot.get("name") if lot else None,
                    life_date=lot.get("life_date") if lot else None,
                )
            )
        return InventorySnapshot(quants)

    def _load_products(self, product_ids: Sequence[int]) -> Dict[int, Dict[str, object]]:
        if not product_ids:
            return {}
        products = self.client.search_read(
            "product.product",
            domain=[["id", "in", list(product_ids)]],
            fields=["id", "name", "categ_id"],
        )
        output: Dict[int, Dict[str, object]] = {}
        for product in products:
            product_id = int(product["id"])
            category_name = _resolve_relational_name(product.get("categ_id")) or "Unknown"
            output[product_id] = {
                "name": product.get("name", f"Product {product_id}"),
                "category": category_name,
            }
        return output

    def _load_lots(self, lot_ids: Sequence[int]) -> Dict[int, Dict[str, object]]:
        if not lot_ids:
            return {}
        lots = self.client.search_read(
            "stock.lot",
            domain=[["id", "in", list(lot_ids)]],
            fields=["id", "name", "life_date"],
        )
        output: Dict[int, Dict[str, object]] = {}
        for lot in lots:
            lot_id = int(lot["id"])
            life_date = _parse_date(lot.get("life_date"))
            output[lot_id] = {
                "name": lot.get("name", f"Lot {lot_id}"),
                "life_date": life_date,
            }
        return output


def _collect_relational_ids(records: Iterable[Mapping[str, object]], key: str) -> List[int]:
    ids: List[int] = []
    for record in records:
        resolved = _resolve_relational_id(record.get(key))
        if resolved is not None:
            ids.append(resolved)
    return ids


def _resolve_relational_id(value: object) -> Optional[int]:
    if value in (None, False):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)) and value:
        first = value[0]
        if isinstance(first, int):
            return first
        try:
            return int(first)
        except (TypeError, ValueError):
            return None
    return None


def _resolve_relational_name(value: object) -> Optional[str]:
    if value in (None, False):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        if len(value) >= 2 and isinstance(value[1], str):
            return value[1]
        if value and isinstance(value[0], str):
            return value[0]
    return None


def _parse_date(value: object) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None
    return None


__all__ = ["QuantRecord", "InventorySnapshot", "InventoryRepository"]
