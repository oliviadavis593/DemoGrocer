"""Integration service utilities for FoodFlow."""

from .config import (
    DEFAULT_CONFIG_PATH,
    IntegrationConfig,
    IntegrationInventoryConfig,
    load_config,
)
from .enricher import enrich_decisions
from .fixtures import InventoryFixture, fixtures_as_dicts, fixtures_to_snapshot, load_inventory_fixtures
from .movements import MovementEvent, generate_fake_movements, movements_as_dicts
from .odoo_service import IntegrationCycleResult, OdooService
from .shrink_detector import detect_flags, flag_low_movement, flag_near_expiry, flag_overstock

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "IntegrationConfig",
    "IntegrationInventoryConfig",
    "IntegrationCycleResult",
    "OdooService",
    "enrich_decisions",
    "InventoryFixture",
    "MovementEvent",
    "fixtures_as_dicts",
    "fixtures_to_snapshot",
    "generate_fake_movements",
    "detect_flags",
    "flag_low_movement",
    "flag_near_expiry",
    "flag_overstock",
    "load_inventory_fixtures",
    "movements_as_dicts",
    "load_config",
]
