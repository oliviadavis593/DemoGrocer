from __future__ import annotations

from datetime import date, datetime, timezone
from typing import List

from services.integration.odoo_service import OdooService
from services.simulator.inventory import InventorySnapshot, QuantRecord
from packages.db.events import InventoryEvent


class DummyClient:
    def __init__(self) -> None:
        self.auth_calls: int = 0
        self.search_calls: List[tuple[str, List[tuple[str, str, object]], List[str]]] = []

    def authenticate(self) -> int:
        self.auth_calls += 1
        return 99

    def search_read(
        self,
        model: str,
        domain,
        fields,
        limit=None,
        order=None,
    ):
        self.search_calls.append((model, domain, fields))
        if model == "stock.quant":
            return [{"id": 1, "location_id": [11, "Cooler"]}]
        if model == "product.product":
            return [{"id": 42, "name": "Fallback Product"}]
        if model == "stock.move":
            return [{"product_id": [42, "Fallback Product"], "quantity_done": 4}]
        return []


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


def test_fetch_inventory_snapshot_groups_locations() -> None:
    class SnapshotRepository(DummyRepository):
        def load_snapshot(self) -> InventorySnapshot:
            snapshot = InventorySnapshot(
                [
                    QuantRecord(
                        id=1,
                        product_id=101,
                        product_name="Spinach",
                        default_code="SPN-1",
                        category="Produce",
                        quantity=5.5,
                        lot_id=300,
                        lot_name="LOT-1",
                        life_date=date(2024, 4, 1),
                    ),
                    QuantRecord(
                        id=2,
                        product_id=101,
                        product_name="Spinach",
                        default_code="SPN-1",
                        category="Produce",
                        quantity=4.5,
                        lot_id=300,
                        lot_name="LOT-1",
                        life_date=date(2024, 4, 1),
                    ),
                ]
            )
            self.snapshots.append(snapshot)
            return snapshot

    client = DummyClient()

    def repo_factory(client_obj: DummyClient) -> SnapshotRepository:
        return SnapshotRepository(client_obj)

    service = OdooService(client_factory=lambda: client, repository_factory=repo_factory)
    snapshot = service.fetch_inventory_snapshot()

    assert len(snapshot) == 1
    record = snapshot[0]
    assert record["product"] == "Spinach"
    assert record["quantity"] == 10.0
    assert record["life_date"] == "2024-04-01"
    assert record["locations"] == ["Cooler"]


def test_fetch_sales_uses_event_store_when_available() -> None:
    class MemoryEventStore:
        def list_events(self, *, event_type, since, limit):
            assert event_type == "sell_down"
            assert limit == 5000
            now = datetime.now(timezone.utc)
            return [
                InventoryEvent(
                    ts=now,
                    type="sell_down",
                    product="Spinach",
                    lot=None,
                    qty=-6.0,
                    before=10.0,
                    after=4.0,
                    source="simulator",
                )
            ]

    client = DummyClient()
    service = OdooService(
        client_factory=lambda: client,
        repository_factory=lambda client_obj: DummyRepository(client_obj),
        event_store_factory=lambda: MemoryEventStore(),
    )

    velocities = service.fetch_sales(window_days=3)

    assert velocities["Spinach"] == 2.0
    # Ensure we did not hit the Odoo fallback when events are available
    assert all(call[0] != "stock.move" for call in client.search_calls)
