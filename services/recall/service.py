"""Helpers for quarantining recalled inventory."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from packages.odoo_client import OdooClient, OdooClientError
from services.simulator.events import EventWriter, SimulatorEvent


@dataclass
class RecallResult:
    """Details about a single recalled quant."""

    product: str
    default_code: Optional[str]
    lot: Optional[str]
    quantity: float
    source_location: str
    destination_location: str


@dataclass
class QuarantinedItem:
    """Summary of inventory currently held in the quarantine location."""

    product: str
    default_code: Optional[str]
    lot: Optional[str]
    quantity: float


class RecallService:
    """Move matching inventory into a quarantine location and log events."""

    def __init__(self, client: OdooClient, event_writer: Optional[EventWriter] = None) -> None:
        self.client = client
        self.event_writer = event_writer

    def recall(
        self,
        *,
        default_codes: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> List[RecallResult]:
        codes = _normalize_tokens(default_codes)
        categories = _normalize_tokens(categories)
        if not codes and not categories:
            raise ValueError("Provide at least one default code or category")

        product_map = self._load_products(codes=codes, categories=categories)
        if not product_map:
            return []

        quarantine_id, quarantine_name = self._ensure_quarantine_location()
        quants = self._load_quants(list(product_map.keys()))

        now = datetime.now(timezone.utc)
        events: List[SimulatorEvent] = []
        results: List[RecallResult] = []

        for quant in quants:
            quant_id = int(quant["id"])
            quantity = float(quant.get("quantity") or 0.0)
            if quantity <= 0:
                continue

            location_id, location_name = _resolve_relational(quant.get("location_id"))
            if location_id == quarantine_id:
                continue

            product_id, _ = _resolve_relational(quant.get("product_id"))
            product_info = product_map.get(product_id)
            if not product_info:
                continue

            lot_id, lot_name = _resolve_relational(quant.get("lot_id"))

            self.client.write("stock.quant", quant_id, {"quantity": 0.0})
            self._increment_quarantine_quant(product_id, lot_id, quantity, quarantine_id)

            events.append(
                SimulatorEvent(
                    ts=now,
                    type="recall_quarantine",
                    product=product_info["name"],
                    lot=lot_name,
                    qty=-quantity,
                    before=quantity,
                    after=0.0,
                )
            )
            results.append(
                RecallResult(
                    product=product_info["name"],
                    default_code=product_info.get("default_code"),
                    lot=lot_name,
                    quantity=quantity,
                    source_location=location_name or "Unknown",
                    destination_location=quarantine_name,
                )
            )

        if events and self.event_writer is not None:
            self.event_writer.write(events)

        return results

    def list_quarantined(self) -> List[QuarantinedItem]:
        try:
            quarantine_id, _ = self._ensure_quarantine_location()
        except OdooClientError:
            return []
        items = self.client.search_read(
            "stock.quant",
            domain=[
                ("location_id", "=", quarantine_id),
                ("quantity", ">", 0.0),
            ],
            fields=["id", "product_id", "lot_id", "quantity"],
        )
        if not items:
            return []

        product_ids = {int(_resolve_relational(item.get("product_id"))[0]) for item in items if item.get("product_id")}
        products = self._fetch_products_by_ids(product_ids)

        output: List[QuarantinedItem] = []
        for item in items:
            product_id, _ = _resolve_relational(item.get("product_id"))
            if not product_id:
                continue
            product = products.get(product_id)
            if not product:
                continue
            lot_id, lot_name = _resolve_relational(item.get("lot_id"))
            quantity = float(item.get("quantity") or 0.0)
            if quantity <= 0:
                continue
            output.append(
                QuarantinedItem(
                    product=product["name"],
                    default_code=product.get("default_code"),
                    lot=lot_name,
                    quantity=quantity,
                )
            )
        return output

    # Internal helpers ---------------------------------------------------------
    def _ensure_quarantine_location(self) -> Tuple[int, str]:
        records = self.client.search_read(
            "stock.location",
            domain=[("name", "=", "Quarantine"), ("usage", "=", "internal")],
            fields=["id", "name"],
            limit=1,
        )
        if records:
            record = records[0]
            return int(record["id"]), str(record.get("name") or "Quarantine")

        parent = self._find_stock_parent()
        values: Dict[str, object] = {"name": "Quarantine", "usage": "internal"}
        if parent:
            values["location_id"] = parent
        location_id = self.client.create("stock.location", values)
        return location_id, "Quarantine"

    def _find_stock_parent(self) -> Optional[int]:
        records = self.client.search_read(
            "stock.location",
            domain=[("usage", "=", "view"), ("name", "=", "Stock")],
            fields=["id"],
            limit=1,
        )
        if records:
            return int(records[0]["id"])
        return None

    def _increment_quarantine_quant(
        self,
        product_id: int,
        lot_id: Optional[int],
        quantity: float,
        quarantine_id: int,
    ) -> None:
        domain: List[Tuple[str, str, object]] = [
            ("product_id", "=", product_id),
            ("location_id", "=", quarantine_id),
        ]
        if lot_id is not None:
            domain.append(("lot_id", "=", lot_id))
        else:
            domain.append(("lot_id", "=", False))
        existing = self.client.search_read(
            "stock.quant",
            domain=domain,
            fields=["id", "quantity"],
            limit=1,
        )
        if existing:
            record = existing[0]
            quant_id = int(record["id"])
            current_qty = float(record.get("quantity") or 0.0)
            self.client.write("stock.quant", quant_id, {"quantity": round(current_qty + quantity, 4)})
        else:
            values: Dict[str, object] = {
                "product_id": product_id,
                "location_id": quarantine_id,
                "quantity": round(quantity, 4),
                "reserved_quantity": 0.0,
            }
            if lot_id is not None:
                values["lot_id"] = lot_id
            self.client.create("stock.quant", values)

    def _load_quants(self, product_ids: Sequence[int]) -> List[Dict[str, object]]:
        if not product_ids:
            return []
        return self.client.search_read(
            "stock.quant",
            domain=[("product_id", "in", list(product_ids))],
            fields=["id", "product_id", "lot_id", "quantity", "location_id"],
        )

    def _load_products(
        self,
        *,
        codes: Sequence[str],
        categories: Sequence[str],
    ) -> Dict[int, Dict[str, object]]:
        product_ids: Dict[int, Dict[str, object]] = {}
        if codes:
            records = self.client.search_read(
                "product.product",
                domain=[("default_code", "in", list(codes))],
                fields=["id", "name", "default_code", "categ_id"],
            )
            for record in records:
                product_ids[int(record["id"])] = {
                    "name": record.get("name", f"Product {record['id']}"),
                    "default_code": record.get("default_code"),
                    "category_id": _resolve_relational(record.get("categ_id"))[0],
                }
        if categories:
            category_ids = self._fetch_category_ids(categories)
            if category_ids:
                records = self.client.search_read(
                    "product.product",
                    domain=[("categ_id", "in", list(category_ids))],
                    fields=["id", "name", "default_code", "categ_id"],
                )
                for record in records:
                    product_ids[int(record["id"])] = {
                        "name": record.get("name", f"Product {record['id']}"),
                        "default_code": record.get("default_code"),
                        "category_id": _resolve_relational(record.get("categ_id"))[0],
                    }
        return product_ids

    def _fetch_category_ids(self, names: Iterable[str]) -> List[int]:
        cleaned = [name.strip() for name in names if name.strip()]
        if not cleaned:
            return []
        records = self.client.search_read(
            "product.category",
            domain=[("name", "in", cleaned)],
            fields=["id"],
        )
        return [int(record["id"]) for record in records]

    def _fetch_products_by_ids(self, product_ids: Iterable[int]) -> Dict[int, Dict[str, object]]:
        ids = [int(pid) for pid in product_ids if pid]
        if not ids:
            return {}
        records = self.client.search_read(
            "product.product",
            domain=[("id", "in", ids)],
            fields=["id", "name", "default_code"],
        )
        output: Dict[int, Dict[str, object]] = {}
        for record in records:
            output[int(record["id"])] = {
                "name": record.get("name", f"Product {record['id']}"),
                "default_code": record.get("default_code"),
            }
        return output


def _normalize_tokens(values: Sequence[str] | None) -> List[str]:
    if not values:
        return []
    tokens: List[str] = []
    for value in values:
        if not value:
            continue
        parts = [part.strip() for part in value.split(",")]
        tokens.extend(part for part in parts if part)
    seen = set()
    unique: List[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique


def _resolve_relational(value: object) -> Tuple[Optional[int], Optional[str]]:
    if value in (None, False):
        return None, None
    if isinstance(value, int):
        return value, None
    if isinstance(value, (list, tuple)) and value:
        first = value[0]
        name = value[1] if len(value) > 1 else None
        if isinstance(first, int):
            return first, str(name) if name is not None else None
    return None, None
