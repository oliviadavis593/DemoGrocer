"""Reusable wrapper around the Odoo client for integration workflows."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from packages.odoo_client import OdooClient
from services.simulator.inventory import InventoryRepository, InventorySnapshot, QuantRecord

RepositoryFactory = Callable[[OdooClient], InventoryRepository]


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
        lot_expiry_field: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._client_factory = client_factory or OdooClient
        self._repository_factory = repository_factory
        self._lot_expiry_field = lot_expiry_field
        self._logger = logger or logging.getLogger("foodflow.integration")
        self._client: Optional[OdooClient] = None
        self._repository: Optional[InventoryRepository] = None
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


__all__ = ["IntegrationCycleResult", "OdooService"]
