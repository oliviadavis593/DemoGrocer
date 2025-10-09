from __future__ import annotations

from datetime import date
from typing import List

from services.integration.odoo_service import OdooService
from services.simulator.inventory import InventorySnapshot, QuantRecord


class DummyClient:
    def __init__(self) -> None:
        self.auth_calls: int = 0

    def authenticate(self) -> int:
        self.auth_calls += 1
        return 99


class DummyRepository:
    def __init__(self, client: DummyClient) -> None:
        self.client = client
        self.lot_field: str | None = None
        self.snapshots: List[InventorySnapshot] = []

    def set_lot_expiry_field(self, value: str | None) -> None:
        self.lot_field = value

    def load_snapshot(self) -> InventorySnapshot:
        quant = QuantRecord(
            id=1,
            product_id=10,
            product_name="Apples",
            default_code="APL-001",
            category="Produce",
            quantity=12.5,
            lot_id=7,
            lot_name="LOT-7",
            life_date=date(2024, 5, 1),
        )
        snapshot = InventorySnapshot([quant])
        self.snapshots.append(snapshot)
        return snapshot


def test_sync_returns_summary_and_configures_repository() -> None:
    client = DummyClient()
    repositories: list[DummyRepository] = []

    def repo_factory(client_obj: DummyClient) -> DummyRepository:
        repo = DummyRepository(client_obj)
        repositories.append(repo)
        return repo

    service = OdooService(
        client_factory=lambda: client,
        repository_factory=repo_factory,
        lot_expiry_field="life_date",
    )

    result = service.sync(summary_limit=2)

    assert result.total_quants == 1
    assert len(result.sample) == 1
    assert result.sample[0]["product"] == "Apples"
    assert result.sample[0]["life_date"] == "2024-05-01"
    assert client.auth_calls == 1
    assert repositories and repositories[0].lot_field == "life_date"
    assert repositories[0].client is client


def test_inventory_repository_is_cached() -> None:
    client = DummyClient()
    created: list[DummyRepository] = []

    def repo_factory(client_obj: DummyClient) -> DummyRepository:
        repo = DummyRepository(client_obj)
        created.append(repo)
        return repo

    service = OdooService(client_factory=lambda: client, repository_factory=repo_factory)

    repo1 = service.inventory_repository()
    repo2 = service.inventory_repository()

    assert repo1 is repo2
    assert client.auth_calls == 1
    assert len(created) == 1
