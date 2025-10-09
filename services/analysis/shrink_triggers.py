"""Detect shrink-related triggers such as low movement and overstock."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Mapping, MutableMapping, Optional, Sequence

from packages.db import EventStore, InventoryEvent
from services.simulator.events import SimulatorEvent
from services.simulator.inventory import InventorySnapshot, QuantRecord

try:  # pragma: no cover - optional dependency
    import yaml  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    yaml = None

LOGGER = logging.getLogger(__name__)


@dataclass
class LowMovementConfig:
    """Configuration for detecting low-movement products."""

    units_threshold: float = 12.0
    window_days: int = 7

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, object]]) -> "LowMovementConfig":
        if not data:
            return cls()
        defaults = cls()
        try:
            units_threshold = float(data.get("units_threshold", defaults.units_threshold))
        except (TypeError, ValueError):
            units_threshold = defaults.units_threshold
        try:
            window_days = int(data.get("window_days", defaults.window_days))
        except (TypeError, ValueError):
            window_days = defaults.window_days
        window_days = max(window_days, 1)
        return cls(units_threshold=units_threshold, window_days=window_days)


@dataclass
class OverstockConfig:
    """Configuration for detecting overstock conditions."""

    default_days_of_supply: float = 21.0
    category_thresholds: Dict[str, float] = field(default_factory=dict)
    velocity_window_days: int = 7
    min_daily_velocity: float = 0.25

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, object]]) -> "OverstockConfig":
        if not data:
            return cls()
        defaults = cls()
        try:
            default_days = float(data.get("default_days_of_supply", defaults.default_days_of_supply))
        except (TypeError, ValueError):
            default_days = defaults.default_days_of_supply
        try:
            velocity_window_days = int(data.get("velocity_window_days", defaults.velocity_window_days))
        except (TypeError, ValueError):
            velocity_window_days = defaults.velocity_window_days
        velocity_window_days = max(velocity_window_days, 1)
        try:
            min_daily_velocity = float(data.get("min_daily_velocity", defaults.min_daily_velocity))
        except (TypeError, ValueError):
            min_daily_velocity = defaults.min_daily_velocity
        min_daily_velocity = max(min_daily_velocity, 1e-6)
        thresholds_raw = data.get("category_thresholds", {})
        category_thresholds: Dict[str, float] = {}
        if isinstance(thresholds_raw, Mapping):
            for key, value in thresholds_raw.items():
                try:
                    category_thresholds[str(key)] = float(value)
                except (TypeError, ValueError):
                    continue
        return cls(
            default_days_of_supply=default_days,
            category_thresholds=category_thresholds,
            velocity_window_days=velocity_window_days,
            min_daily_velocity=min_daily_velocity,
        )

    def threshold_for(self, category: str) -> float:
        return self.category_thresholds.get(category, self.default_days_of_supply)

    def velocity_floor(self) -> float:
        return max(self.min_daily_velocity, 1e-6)


@dataclass
class ShrinkTriggerConfig:
    """Top-level configuration for shrink trigger detection."""

    low_movement: LowMovementConfig = field(default_factory=LowMovementConfig)
    overstock: OverstockConfig = field(default_factory=OverstockConfig)
    history_limit: int = 5000

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> "ShrinkTriggerConfig":
        low_cfg = LowMovementConfig.from_mapping(_get_mapping(data, "low_movement"))
        overstock_cfg = OverstockConfig.from_mapping(_get_mapping(data, "overstock"))
        try:
            history_limit = int(data.get("history_limit", cls().history_limit))
        except (TypeError, ValueError):
            history_limit = cls().history_limit
        history_limit = max(history_limit, 100)
        return cls(low_movement=low_cfg, overstock=overstock_cfg, history_limit=history_limit)


def load_config(path: Path) -> ShrinkTriggerConfig:
    """Load shrink trigger configuration from YAML, falling back to defaults."""

    if not path.exists():
        LOGGER.info("Shrink trigger config %s not found; using defaults", path)
        return ShrinkTriggerConfig()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        LOGGER.exception("Failed to read shrink trigger config %s; using defaults", path)
        return ShrinkTriggerConfig()
    if not text.strip():
        return ShrinkTriggerConfig()
    if yaml is not None:
        data = yaml.safe_load(text) or {}
    else:
        data = _parse_simple_yaml(text)
    if not isinstance(data, Mapping):
        raise ValueError("Shrink trigger configuration must be a mapping")
    return ShrinkTriggerConfig.from_mapping(data)


@dataclass
class _SalesWindow:
    """Aggregated sales totals for a product."""

    low_window_units: float = 0.0
    velocity_window_units: float = 0.0


@dataclass
class _ProductInventory:
    """Aggregated inventory view per product."""

    total_qty: float = 0.0
    category: str = "Unknown"
    quants: list[QuantRecord] = field(default_factory=list)

    def representative_lot(self) -> Optional[str]:
        for quant in self.quants:
            if quant.lot_name:
                return quant.lot_name
        return None


class ShrinkTriggerDetector:
    """Evaluate shrink trigger conditions based on sales history and current stock."""

    def __init__(self, store: Optional[EventStore], config: ShrinkTriggerConfig) -> None:
        self.store = store
        self.config = config

    def evaluate(self, now: datetime, snapshot: InventorySnapshot) -> Sequence[SimulatorEvent]:
        if self.store is None:
            LOGGER.debug("No event store available; skipping shrink trigger evaluation")
            return []

        quants = list(snapshot.quants())
        if not quants:
            return []

        low_window_days = max(self.config.low_movement.window_days, 1)
        velocity_window_days = max(self.config.overstock.velocity_window_days, 1)
        combined_window_days = max(low_window_days, velocity_window_days)

        history = self._load_sales_history(now, combined_window_days)
        sales_by_product = self._summarize_sales(history, now, low_window_days, velocity_window_days)
        inventory_by_product = self._summarize_inventory(quants)

        events: list[SimulatorEvent] = []
        for product, inventory in inventory_by_product.items():
            total_qty = inventory.total_qty
            if total_qty <= 0:
                continue
            sales = sales_by_product.get(product, _SalesWindow())
            lot_name = inventory.representative_lot()
            if sales.low_window_units <= self.config.low_movement.units_threshold:
                events.append(
                    SimulatorEvent(
                        ts=now,
                        type="flag_low_movement",
                        product=product,
                        lot=lot_name,
                        qty=0.0,
                        before=total_qty,
                        after=total_qty,
                    )
                )

            if self._is_overstock(sales, inventory, total_qty, velocity_window_days):
                events.append(
                    SimulatorEvent(
                        ts=now,
                        type="flag_overstock",
                        product=product,
                        lot=lot_name,
                        qty=0.0,
                        before=total_qty,
                        after=total_qty,
                    )
                )
        return events

    def _load_sales_history(self, now: datetime, window_days: int) -> Sequence[InventoryEvent]:
        if window_days <= 0:
            window_days = 1
        since = now - timedelta(days=window_days)
        try:
            return self.store.list_events(
                event_type="sell_down",
                since=since,
                limit=self.config.history_limit,
            )
        except Exception:  # pragma: no cover - defensive logging
            LOGGER.exception("Failed to load sales history from event store")
            return []

    def _summarize_sales(
        self,
        history: Sequence[InventoryEvent],
        now: datetime,
        low_window_days: int,
        velocity_window_days: int,
    ) -> Dict[str, _SalesWindow]:
        low_since = now - timedelta(days=low_window_days)
        velocity_since = now - timedelta(days=velocity_window_days)

        summary: Dict[str, _SalesWindow] = {}
        for event in history:
            ts = _ensure_aware(event.ts)
            product = event.product
            if not product:
                continue
            sold_units = max(-event.qty, 0.0)
            if sold_units <= 0:
                continue
            stats = summary.setdefault(product, _SalesWindow())
            if ts >= low_since:
                stats.low_window_units += sold_units
            if ts >= velocity_since:
                stats.velocity_window_units += sold_units
        return summary

    def _summarize_inventory(self, quants: Sequence[QuantRecord]) -> Dict[str, _ProductInventory]:
        inventory: Dict[str, _ProductInventory] = {}
        for quant in quants:
            total = max(float(quant.quantity), 0.0)
            if total <= 0:
                continue
            record = inventory.get(quant.product_name)
            if record is None:
                record = _ProductInventory(category=quant.category)
                inventory[quant.product_name] = record
            record.total_qty += total
            record.quants.append(quant)
        return inventory

    def _is_overstock(
        self,
        sales: _SalesWindow,
        inventory: _ProductInventory,
        total_qty: float,
        velocity_window_days: int,
    ) -> bool:
        velocity_floor = self.config.overstock.velocity_floor()
        threshold = self.config.overstock.threshold_for(inventory.category)
        velocity_window_days = max(velocity_window_days, 1)
        average_daily_sales = sales.velocity_window_units / velocity_window_days if velocity_window_days else 0.0
        effective_velocity = max(average_daily_sales, velocity_floor)
        if effective_velocity <= 0:
            return True
        days_of_supply = total_qty / effective_velocity
        return days_of_supply > threshold


def _get_mapping(data: Mapping[str, object], key: str) -> MutableMapping[str, object]:
    value = data.get(key)
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _parse_simple_yaml(text: str) -> Dict[str, object]:
    """Very small YAML parser for simple nested mappings."""

    root: Dict[str, object] = {}
    stack: list[tuple[int, Dict[str, object]]] = [(-1, root)]
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1] if stack else root

        if not value:
            new_map: Dict[str, object] = {}
            parent[key] = new_map
            stack.append((indent, new_map))
        else:
            parent[key] = _parse_scalar(value)
    return root


def _parse_scalar(value: str) -> object:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        pass
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def _ensure_aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


__all__ = [
    "ShrinkTriggerConfig",
    "ShrinkTriggerDetector",
    "LowMovementConfig",
    "OverstockConfig",
    "load_config",
]
