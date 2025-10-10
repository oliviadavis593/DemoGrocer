"""Reusable wrapper around the Odoo client for integration workflows."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from packages.db import EventStore
from packages.odoo_client import OdooClient
from services.simulator.inventory import InventoryRepository, InventorySnapshot, QuantRecord

RepositoryFactory = Callable[[OdooClient], InventoryRepository]
EventStoreFactory = Callable[[], EventStore]


@dataclass
class IntegrationCycleResult:
    """Summary of a single integration sync execution."""

    timestamp: datetime
    total_quants: int
    sample: List[Dict[str, object]]


class OdooService:
    """Authenticate against Odoo and provide inventory helpers."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], OdooClient] | None = None,
        repository_factory: RepositoryFactory | None = None,
        event_store_factory: EventStoreFactory | None = None,
        lot_expiry_field: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._client_factory = client_factory or OdooClient
        self._repository_factory = repository_factory
        self._event_store_factory = event_store_factory
        self._lot_expiry_field = lot_expiry_field
        self._logger = logger or logging.getLogger("foodflow.integration")
        self._client: Optional[OdooClient] = None
        self._repository: Optional[InventoryRepository] = None
        self._event_store: Optional[EventStore] = None
        self._authenticated = False

    def client(self) -> OdooClient:
        """Return an authenticated Odoo client, creating it on demand."""

        if self._client is None:
            self._client = self._client_factory()
        if not self._authenticated:
            uid = self._client.authenticate()
            self._authenticated = True
            self._logger.info("Authenticated Odoo client (uid=%s)", uid)
        return self._client

    def inventory_repository(self) -> InventoryRepository:
        """Return an inventory repository bound to the authenticated client."""

        if self._repository is None:
            client = self.client()
            if self._repository_factory is not None:
                repository = self._repository_factory(client)
            else:
                repository = InventoryRepository(client)
            if self._lot_expiry_field and hasattr(repository, "set_lot_expiry_field"):
                repository.set_lot_expiry_field(self._lot_expiry_field)
            self._repository = repository
        return self._repository

    def event_store(self) -> EventStore:
        """Return a cached event store instance."""

        if self._event_store is None:
            factory = self._event_store_factory or EventStore
            self._event_store = factory()
        return self._event_store

    def fetch_snapshot(self) -> InventorySnapshot:
        """Load a fresh inventory snapshot from Odoo."""

        repository = self.inventory_repository()
        snapshot = repository.load_snapshot()
        self._logger.debug("Loaded inventory snapshot with %d quants", len(list(snapshot.quants())))
        return snapshot

    def sync(self, *, summary_limit: int = 5) -> IntegrationCycleResult:
        """Run a single integration sync cycle and return a summary."""

        snapshot = self.fetch_snapshot()
        quants = list(snapshot.quants())
        now = datetime.now(timezone.utc)
        sample = [_serialize_quant(record) for record in quants[:summary_limit]]
        self._logger.info(
            "Integration cycle complete: %d quants fetched at %s",
            len(quants),
            now.isoformat(),
        )
        for entry in sample:
            self._logger.info(
                "Sample quant id=%s product=%s lot=%s qty=%.2f category=%s",
                entry.get("id"),
                entry.get("product"),
                entry.get("lot") or "-",
                entry.get("quantity"),
                entry.get("category"),
            )
        return IntegrationCycleResult(timestamp=now, total_quants=len(quants), sample=sample)

    def fetch_inventory_snapshot(self) -> List[Dict[str, object]]:
        """Return an aggregated inventory snapshot grouped by product and lot."""

        snapshot = self.fetch_snapshot()
        quants = list(snapshot.quants())
        if not quants:
            return []

        client = self.client()
        quant_ids = [quant.id for quant in quants]
        location_map: Dict[int, str] = {}
        try:
            location_records = client.search_read(
                "stock.quant",
                domain=[("id", "in", quant_ids)],
                fields=["id", "location_id"],
            )
        except Exception:
            self._logger.exception("Failed to resolve locations for inventory snapshot")
            location_records = []

        for record in location_records:
            quant_id = _coerce_int(record.get("id"))
            if quant_id is None:
                continue
            _, location_name = _resolve_many2one(record.get("location_id"))
            if location_name:
                location_map[quant_id] = location_name

        aggregated: Dict[Tuple[str, Optional[str]], Dict[str, object]] = {}
        location_sets: Dict[Tuple[str, Optional[str]], Set[str]] = {}

        for quant in quants:
            key = (quant.product_name, quant.lot_name)
            entry = aggregated.get(key)
            if entry is None:
                entry = {
                    "product": quant.product_name,
                    "lot": quant.lot_name,
                    "quantity": 0.0,
                    "locations": [],
                    "life_date": quant.life_date.isoformat() if quant.life_date else None,
                    "default_code": quant.default_code,
                }
                aggregated[key] = entry
                location_sets[key] = set()
            entry["quantity"] = float(entry["quantity"]) + max(float(quant.quantity), 0.0)
            if entry["life_date"] is None and quant.life_date:
                entry["life_date"] = quant.life_date.isoformat()
            location_name = location_map.get(quant.id)
            if location_name:
                location_sets[key].add(location_name)

        results: List[Dict[str, object]] = []
        for key, entry in aggregated.items():
            locations = sorted(location_sets.get(key, set()))
            entry["quantity"] = round(float(entry["quantity"]), 4)
            entry["locations"] = locations
            results.append(entry)
        results.sort(key=lambda item: (item.get("product") or "", item.get("lot") or ""))
        return results

    def fetch_sales(self, window_days: int) -> Dict[str, float]:
        """Return average daily sales velocity per product for the given window."""

        window_days = max(int(window_days or 0), 1)
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=window_days)

        totals: Dict[str, float] = {}
        try:
            events = self.event_store().list_events(event_type="sell_down", since=since, limit=5000)
        except Exception:
            self._logger.exception("Failed to load sales history from event store")
            events = []
        for event in events:
            product = str(event.product or "").strip()
            if not product:
                continue
            units = max(-float(event.qty), 0.0)
            if units <= 0:
                continue
            totals[product] = totals.get(product, 0.0) + units

        if totals:
            return {product: round(quantity / window_days, 4) for product, quantity in totals.items()}

        client = self.client()
        since_iso = since.astimezone(timezone.utc).isoformat()
        try:
            records = client.search_read(
                "stock.move",
                domain=[("state", "=", "done"), ("date", ">=", since_iso)],
                fields=["product_id", "quantity_done"],
            )
        except Exception:
            self._logger.exception("Failed to load sales data from Odoo")
            return {}

        product_totals: Dict[int, float] = {}
        for record in records:
            product_id, _ = _resolve_many2one(record.get("product_id"))
            if not product_id:
                continue
            quantity_raw = record.get("quantity_done")
            quantity = _coerce_float(quantity_raw)
            if quantity is None or quantity <= 0:
                continue
            product_totals[product_id] = product_totals.get(product_id, 0.0) + quantity

        if not product_totals:
            return {}

        product_names = self._map_product_names(product_totals.keys())
        velocities: Dict[str, float] = {}
        for product_id, total_units in product_totals.items():
            name = product_names.get(product_id, f"Product {product_id}")
            velocities[name] = round(total_units / window_days, 4)
        return velocities

    def _map_product_names(self, product_ids: Iterable[int]) -> Dict[int, str]:
        ids = sorted({int(pid) for pid in product_ids if pid is not None})
        if not ids:
            return {}
        client = self.client()
        try:
            records = client.search_read(
                "product.product",
                domain=[("id", "in", ids)],
                fields=["id", "name"],
            )
        except Exception:
            self._logger.exception("Failed to resolve product names for sales lookup")
            return {}
        names: Dict[int, str] = {}
        for record in records:
            product_id = _coerce_int(record.get("id"))
            if product_id is None:
                continue
            name = str(record.get("name") or f"Product {product_id}")
            names[product_id] = name
        return names


def _serialize_quant(record: QuantRecord) -> Dict[str, object]:
    return {
        "id": record.id,
        "product_id": record.product_id,
        "product": record.product_name,
        "default_code": record.default_code,
        "category": record.category,
        "quantity": record.quantity,
        "lot_id": record.lot_id,
        "lot": record.lot_name,
        "life_date": record.life_date.isoformat() if record.life_date else None,
    }


def _resolve_many2one(value: object) -> Tuple[Optional[int], Optional[str]]:
    if value in (None, False):
        return None, None
    if isinstance(value, int):
        return value, None
    if isinstance(value, str):
        return None, value
    if isinstance(value, Sequence) and value:
        raw_id = value[0]
        raw_name = value[1] if len(value) > 1 else None
        return _coerce_int(raw_id), str(raw_name) if raw_name is not None else None
    return None, None


def _coerce_int(value: object) -> Optional[int]:
    try:
        if isinstance(value, bool):
            return int(value)
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: object) -> Optional[float]:
    try:
        if isinstance(value, bool):
            return float(value)
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = ["IntegrationCycleResult", "OdooService"]
