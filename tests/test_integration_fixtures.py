from __future__ import annotations

from datetime import date

from services.integration import (
    fixtures_as_dicts,
    fixtures_to_snapshot,
    generate_fake_movements,
    load_inventory_fixtures,
    movements_as_dicts,
)


def _select_fixtures():
    fixtures = load_inventory_fixtures(base_date=date(2025, 1, 1))
    assert fixtures, "Expected catalog-backed fixtures to be available"
    return fixtures


def test_inventory_fixtures_include_supplier_and_shelf_life() -> None:
    fixtures = _select_fixtures()
    gala = next(item for item in fixtures if item.default_code == "FF101")

    assert gala.supplier == "River Valley Farms Cooperative"
    assert gala.perishable is True
    assert gala.backroom_qty + gala.sales_floor_qty == gala.stock_on_hand
    assert gala.life_date == date(2025, 1, 6)

    serialized = fixtures_as_dicts([gala])[0]
    assert serialized["life_date"] == "2025-01-06"
    assert serialized["stock_on_hand"] == gala.stock_on_hand


def test_fixtures_convert_to_snapshot_with_expected_quantities() -> None:
    fixtures = _select_fixtures()[:3]
    snapshot = fixtures_to_snapshot(fixtures)
    quants = list(snapshot.quants())

    assert len(quants) == len(fixtures)
    first_quant = quants[0]
    first_fixture = fixtures[0]

    assert first_quant.product_name == first_fixture.product
    assert first_quant.quantity == first_fixture.stock_on_hand
    assert first_quant.lot_name == first_fixture.lot_name


def test_generate_fake_movements_is_deterministic_and_sorted() -> None:
    fixtures = _select_fixtures()
    perishables = [item for item in fixtures if item.perishable][:3]
    low_demand = [item for item in fixtures if item.demand_profile == "low"][:2]
    selected = perishables + low_demand

    events_a = generate_fake_movements(
        selected,
        days=5,
        start_date=date(2025, 1, 1),
        seed=99,
    )
    events_b = generate_fake_movements(
        selected,
        days=5,
        start_date=date(2025, 1, 1),
        seed=99,
    )

    assert events_a == events_b
    assert all(events_a[index].ts <= events_a[index + 1].ts for index in range(len(events_a) - 1))

    types = {event.type for event in events_a}
    assert types.issubset({"sale", "expiry_adjustment", "markdown_clearance", "receiving"})
    assert any(event.type == "expiry_adjustment" for event in events_a)
    assert any(event.type == "markdown_clearance" for event in events_a)

    serialized = movements_as_dicts(events_a[:2])
    assert serialized[0]["ts"].endswith("Z") or serialized[0]["ts"].endswith("+00:00")
    assert "supplier" in serialized[0]
    assert round(serialized[0]["qty"], 4) == serialized[0]["qty"]
