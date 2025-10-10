"""Integration service utilities for FoodFlow."""

from .config import (
    DEFAULT_CONFIG_PATH,
    IntegrationConfig,
    IntegrationInventoryConfig,
    load_config,
)
from .enricher import enrich_decisions
from .odoo_service import IntegrationCycleResult, OdooService
from .shrink_detector import detect_flags, flag_low_movement, flag_near_expiry, flag_overstock

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "IntegrationConfig",
    "IntegrationInventoryConfig",
    "IntegrationCycleResult",
    "OdooService",
    "enrich_decisions",
    "detect_flags",
    "flag_low_movement",
    "flag_near_expiry",
    "flag_overstock",
    "load_config",
]
