from __future__ import annotations

from typing import Any, Dict, List, Sequence

import pytest

from services.integration.config import IntegrationConfig, IntegrationInventoryConfig
from services.integration.enricher import enrich_decisions


class FakeClient:
    def __init__(self) -> None:
        self._authenticated = False
        self.locations: Dict[int, Dict[str, Any]] = {
            200: {"id": 200, "name": "Store A - Full Assortment", "usage": "view", "location_id": False},
            201: {"id": 201, "name": "Sellable Shelf", "usage": "internal", "location_id": [200, "Store A - Full Assortment"]},
            202: {"id": 202, "name": "Quarantine", "usage": "internal", "location_id": [200, "Store A - Full Assortment"]},
        }

    def authenticate(self) -> int:
        self._authenticated = True
        return 1

    def search_read(
        self,
        model: str,
        domain: Sequence[Any],
        fields: Sequence[str] | None = None,
        limit: int | None = None,
        order: str | None = None,
    ) -> List[Dict[str, Any]]:
        if model == "product.product":
            return [
                {
                    "id": 101,
                    "name": "Gala Apples",
                    "default_code": "FF101",
                    "categ_id": [10, "Produce"],
                    "product_tmpl_id": [301, "Gala Apples"],
                }
            ]
        if model == "product.template":
            return []
        if model == "stock.quant":
            return [
                {
                    "product_id": [101, "Gala Apples"],
                    "lot_id": [401, "LOT-1"],
                    "quantity": 3.5,
                    "location_id": [201, "Sellable Shelf"],
                },
                {
                    "product_id": [101, "Gala Apples"],
                    "lot_id": [402, "LOT-2"],
                    "quantity": 1.0,
                    "location_id": [202, "Quarantine"],
                },
            ]
        if model == "stock.location":
            ids: set[int] = set()
            for clause in domain:
                if isinstance(clause, (list, tuple)) and len(clause) == 3 and clause[0] == "id" and clause[1] == "in":
                    ids.update(int(value) for value in clause[2])
            return [dict(self.locations[loc_id]) for loc_id in ids if loc_id in self.locations]
        raise AssertionError(f"Unexpected model {model}")


def test_enrich_decisions_adds_product_and_stock_details() -> None:
    decisions = [
        {"default_code": "FF101", "lot": "LOT-1", "reason": "near_expiry"},
        {"default_code": "FF101", "reason": "near_expiry"},
        {"default_code": "UNKNOWN", "reason": "near_expiry"},
    ]
    config = IntegrationConfig(
        inventory=IntegrationInventoryConfig(quarantine_locations=("isolated storage",))
    )

    results = enrich_decisions(decisions, client=FakeClient(), config=config, allow_remote=False)

    assert len(results) == 3

    first = results[0]
    assert first["product_name"] == "Gala Apples"
    assert first["category"] == "Produce"
    assert pytest.approx(first["qty"]) == 3.5
    assert first["stores"] == ["Store A - Full Assortment"]
    assert first["store"] == "Store A - Full Assortment"

    second = results[1]
    assert second["product_name"] == "Gala Apples"
    assert pytest.approx(second["qty"]) == 3.5
    assert second["stores"] == ["Store A - Full Assortment"]

    third = results[2]
    assert third["product_name"] == "—"
    assert third["qty"] == 0.0
    assert third["stores"] == []
