"""Helpers to enrich flagged decisions with product metadata and stock details."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Mapping, MutableMapping, Sequence

from packages.odoo_client import OdooClient, OdooClientError
from services.integration.config import IntegrationConfig, load_config

LOGGER = logging.getLogger("foodflow.integration.enricher")
MISSING_VALUE = "—"


@dataclass(frozen=True)
class ProductInfo:
    """Metadata resolved from Odoo for a default code."""

    code: str
    product_id: int | None
    template_id: int | None
    name: str | None
    category: str | None


@dataclass(frozen=True)
class LocationInfo:
    """Minimal stock.location representation for deriving store names."""

    location_id: int
    name: str
    usage: str | None
    parent_id: int | None


@dataclass(frozen=True)
class QuantRow:
    """Data extracted from stock.quant search results."""

    product_id: int
    lot_name: str | None
    quantity: float
    location_id: int


def enrich_decisions(
    decisions: Sequence[Mapping[str, object]],
    *,
    client: OdooClient | None = None,
    config: IntegrationConfig | None = None,
    allow_remote: bool = True,
) -> list[dict[str, object]]:
    """Augment flagged decisions with product metadata and live stock details."""

    if not decisions:
        return []

    records: list[dict[str, object]] = []
    codes: list[str] = []
    for entry in decisions:
        if not isinstance(entry, Mapping):
            continue
        record = dict(entry)
        code = _normalize_code(record.get("default_code"))
        lot = _normalize_lot(record.get("lot"))
        if code:
            codes.append(code)
        records.append(record)

    if not records:
        return []

    if not codes:
        for record in records:
            _apply_defaults(record)
        return records

    config = config or _load_integration_config()
    quarantine_tokens = _normalise_quarantine_tokens(config.inventory.quarantine_locations)

    odoo: OdooClient | None
    try:
        if client is not None:
            odoo = client
        elif allow_remote:
            odoo = _build_client()
        else:
            odoo = None
    except Exception:
        LOGGER.exception("Failed to initialize Odoo client for enrichment; returning defaults")
        for record in records:
            _apply_defaults(record)
        return records

    if odoo is None:
        for record in records:
            _apply_defaults(record)
        return records

    _authenticate_client(odoo)

    product_map = _load_products(odoo, codes)
    if not product_map:
        for record in records:
            _apply_defaults(record)
        return records

    product_id_lookup = {
        info.product_id: info.code for info in product_map.values() if info.product_id is not None
    }
    quant_rows = _load_quants(odoo, list(product_id_lookup.keys()))
    if quant_rows:
        location_ids = {row.location_id for row in quant_rows}
        location_map = _load_locations(odoo, location_ids)
        stock_map = _aggregate_stock(
            quant_rows,
            product_id_lookup,
            location_map,
            quarantine_tokens,
        )
    else:
        stock_map = {}

    for record in records:
        code = _normalize_code(record.get("default_code"))
        lot = _normalize_lot(record.get("lot"))
        info = product_map.get(code) if code else None
        stock_key = (code, lot) if code else None
        stock = stock_map.get(stock_key) if stock_key else None
        fallback_stock = stock_map.get((code, None)) if code else None

        product_name = info.name if info and info.name else record.get("product")
        if not product_name:
            product_name = MISSING_VALUE
        record["product_name"] = product_name
        record.setdefault("product", product_name)

        category = info.category if info and info.category else record.get("category")
        if not category:
            category = MISSING_VALUE
        record["category"] = category

        if stock is not None:
            aggregated = stock
        elif lot is None:
            aggregated = fallback_stock
        else:
            aggregated = None
        if aggregated:
            qty_value = round(aggregated["quantity"], 4)
            stores = sorted(aggregated["stores"])
        else:
            qty_value = 0.0
            stores = []

        record["qty"] = qty_value
        record["quantity"] = qty_value
        record["stores"] = stores
        record["store"] = stores[0] if len(stores) == 1 else ("Multiple" if stores else "Unassigned")

    return records


def _apply_defaults(record: MutableMapping[str, object]) -> None:
    record.setdefault("product_name", record.get("product") or MISSING_VALUE)
    record.setdefault("category", record.get("category") or MISSING_VALUE)
    record.setdefault("stores", list(record.get("stores") or []))
    record.setdefault("store", record.get("store") or "Unassigned")
    qty = record.get("qty")
    if qty is None:
        quantity = record.get("quantity")
        try:
            qty_value = float(quantity) if quantity is not None else 0.0
        except (TypeError, ValueError):
            qty_value = 0.0
        record["qty"] = qty_value
        record["quantity"] = qty_value


@lru_cache(maxsize=1)
def _load_integration_config() -> IntegrationConfig:
    try:
        return load_config()
    except Exception:
        LOGGER.exception("Failed to load integration configuration; using defaults")
        return IntegrationConfig()


def _normalise_quarantine_tokens(candidates: Sequence[str]) -> set[str]:
    return {token.strip().lower() for token in candidates if isinstance(token, str) and token.strip()}


def _build_client() -> OdooClient | None:
    try:
        service = _LazyService()
        return service.client()
    except OdooClientError:
        LOGGER.exception("Odoo authentication error during enrichment")
        return None
    except Exception:
        LOGGER.exception("Unexpected error creating Odoo client for enrichment")
        return None


class _LazyService:
    """Lightweight wrapper to memoize an OdooService client without import cycle."""

    def __init__(self) -> None:
        from services.integration.odoo_service import OdooService

        config = _load_integration_config()
        self._service = OdooService(
            lot_expiry_field=config.inventory.lot_expiry_field,
            logger=LOGGER.getChild("service"),
        )

    def client(self) -> OdooClient:
        return self._service.client()


def _authenticate_client(client: OdooClient) -> None:
    auth = getattr(client, "authenticate", None)
    if callable(auth):
        try:
            auth()
        except Exception:
            LOGGER.debug("Client authentication during enrichment failed", exc_info=True)


def _load_products(client: OdooClient, codes: Sequence[str]) -> dict[str, ProductInfo]:
    unique_codes = sorted({code for code in codes if code})
    if not unique_codes:
        return {}

    product_map: dict[str, ProductInfo] = {}
    try:
        records = client.search_read(
            "product.product",
            domain=[("default_code", "in", unique_codes)],
            fields=["id", "name", "default_code", "categ_id", "product_tmpl_id"],
        )
    except Exception:
        LOGGER.exception("Failed to load product metadata for enrichment")
        records = []

    for record in records:
        code = _normalize_code(record.get("default_code"))
        if not code:
            continue
        product_id = _coerce_int(record.get("id"))
        tmpl_id, _ = _resolve_many2one(record.get("product_tmpl_id"))
        _, category_name = _resolve_many2one(record.get("categ_id"))
        name = _normalize_name(record.get("name"))
        product_map[code] = ProductInfo(
            code=code,
            product_id=product_id,
            template_id=tmpl_id,
            name=name,
            category=_normalize_name(category_name),
        )

    missing = [code for code in unique_codes if code not in product_map]
    if not missing:
        return product_map

    try:
        template_records = client.search_read(
            "product.template",
            domain=[("default_code", "in", missing)],
            fields=["id", "name", "default_code", "categ_id", "product_variant_ids"],
        )
    except Exception:
        LOGGER.exception("Failed to load product templates for enrichment")
        template_records = []

    for record in template_records:
        code = _normalize_code(record.get("default_code"))
        if not code:
            continue
        if code in product_map:
            continue
        template_id = _coerce_int(record.get("id"))
        variant_ids = _resolve_variant_ids(record.get("product_variant_ids"))
        product_id = variant_ids[0] if variant_ids else None
        _, category_name = _resolve_many2one(record.get("categ_id"))
        name = _normalize_name(record.get("name"))
        product_map[code] = ProductInfo(
            code=code,
            product_id=product_id,
            template_id=template_id,
            name=name,
            category=_normalize_name(category_name),
        )

    return product_map


def _load_quants(client: OdooClient, product_ids: Sequence[int]) -> list[QuantRow]:
    ids = sorted({pid for pid in product_ids if pid is not None})
    if not ids:
        return []
    try:
        records = client.search_read(
            "stock.quant",
            domain=[
                ("product_id", "in", ids),
                ("quantity", ">", 0.0),
            ],
            fields=["product_id", "lot_id", "quantity", "location_id"],
        )
    except Exception:
        LOGGER.exception("Failed to load stock quant data for enrichment")
        return []

    rows: list[QuantRow] = []
    for record in records:
        product_id, _ = _resolve_many2one(record.get("product_id"))
        if not product_id:
            continue
        lot_id, lot_name = _resolve_many2one(record.get("lot_id"))
        lot_value = _normalize_lot(lot_name)
        quantity = _coerce_float(record.get("quantity"))
        if quantity is None or quantity <= 0:
            continue
        location_id, _ = _resolve_many2one(record.get("location_id"))
        if not location_id:
            continue
        rows.append(
            QuantRow(
                product_id=product_id,
                lot_name=lot_value,
                quantity=quantity,
                location_id=location_id,
            )
        )
    return rows


def _load_locations(client: OdooClient, location_ids: Iterable[int]) -> dict[int, LocationInfo]:
    pending = {int(loc_id) for loc_id in location_ids if loc_id is not None}
    if not pending:
        return {}

    locations: dict[int, LocationInfo] = {}
    fetched: set[int] = set()

    while pending:
        batch = sorted(pending - fetched)
        if not batch:
            break
        try:
            records = client.search_read(
                "stock.location",
                domain=[("id", "in", batch)],
                fields=["id", "name", "usage", "location_id"],
            )
        except Exception:
            LOGGER.exception("Failed to load location metadata for enrichment (batch size=%s)", len(batch))
            break

        fetched.update(batch)
        new_parents: set[int] = set()
        for record in records:
            location_id = _coerce_int(record.get("id"))
            if location_id is None:
                continue
            parent_id, _ = _resolve_many2one(record.get("location_id"))
            locations[location_id] = LocationInfo(
                location_id=location_id,
                name=_normalize_name(record.get("name")) or f"Location {location_id}",
                usage=_normalize_usage(record.get("usage")),
                parent_id=parent_id,
            )
            if parent_id and parent_id not in locations:
                new_parents.add(parent_id)
        pending = new_parents

    return locations


def _aggregate_stock(
    rows: Sequence[QuantRow],
    product_lookup: Mapping[int, str],
    locations: Mapping[int, LocationInfo],
    quarantine_tokens: set[str],
) -> dict[tuple[str, str | None], dict[str, object]]:
    aggregates: dict[tuple[str, str | None], dict[str, object]] = {}
    for row in rows:
        code = product_lookup.get(row.product_id)
        if not code:
            continue
        if _is_quarantine_location(row.location_id, locations, quarantine_tokens):
            continue
        store = _resolve_store_name(row.location_id, locations)
        key = (code, row.lot_name)
        entry = aggregates.get(key)
        if entry is None:
            entry = {"quantity": 0.0, "stores": set()}
            aggregates[key] = entry
        entry["quantity"] = float(entry["quantity"]) + row.quantity
        if store:
            entry["stores"].add(store)

        if row.lot_name is not None:
            total_key = (code, None)
            total_entry = aggregates.get(total_key)
            if total_entry is None:
                total_entry = {"quantity": 0.0, "stores": set()}
                aggregates[total_key] = total_entry
            total_entry["quantity"] = float(total_entry["quantity"]) + row.quantity
            if store:
                total_entry["stores"].add(store)
    return aggregates


def _is_quarantine_location(
    location_id: int,
    locations: Mapping[int, LocationInfo],
    quarantine_tokens: set[str],
) -> bool:
    visited: set[int] = set()
    current = locations.get(location_id)
    while current:
        name_lower = current.name.lower()
        if "quarantine" in name_lower:
            return True
        if name_lower in quarantine_tokens:
            return True
        parent_id = current.parent_id
        if parent_id is None or parent_id in visited:
            break
        visited.add(parent_id)
        current = locations.get(parent_id)
    return False


def _resolve_store_name(location_id: int, locations: Mapping[int, LocationInfo]) -> str | None:
    visited: set[int] = set()
    current = locations.get(location_id)
    fallback = current.name if current else None
    while current:
        if current.usage == "view":
            return current.name
        parent_id = current.parent_id
        if parent_id is None or parent_id in visited:
            break
        visited.add(parent_id)
        parent = locations.get(parent_id)
        if parent is None:
            break
        fallback = parent.name or fallback
        current = parent
    return fallback


def _resolve_many2one(value: object) -> tuple[int | None, str | None]:
    if isinstance(value, (list, tuple)) and value:
        first = value[0]
        name = value[1] if len(value) > 1 else None
        try:
            identifier = int(first) if first is not None else None
        except (TypeError, ValueError):
            identifier = None
        text = _normalize_name(name)
        return identifier, text
    if isinstance(value, int):
        return value, None
    return None, None


def _resolve_variant_ids(value: object) -> list[int]:
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(entry, int) for entry in value):
            return [int(entry) for entry in value]
        if value and isinstance(value[0], (list, tuple)) and len(value[0]) == 3:
            # Many2many commands: (6, 0, [ids])
            command = value[0]
            if isinstance(command, (list, tuple)) and len(command) == 3:
                candidates = command[2]
                if isinstance(candidates, (list, tuple)):
                    return [int(entry) for entry in candidates if isinstance(entry, int)]
    return []


def _coerce_int(value: object) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_code(value: object) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _normalize_lot(value: object) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _normalize_name(value: object) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _normalize_usage(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip().lower()
    return None


__all__ = ["enrich_decisions"]
