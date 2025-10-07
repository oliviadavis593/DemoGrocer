"""Configuration loading for the inventory simulator."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, MutableMapping, Optional

try:  # pragma: no cover - optional dependency
    import yaml  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    yaml = None


@dataclass
class RateConfig:
    """Configuration representing a quantity delta per product category."""

    default: float = 0.0
    category_rates: Dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, object]]) -> "RateConfig":
        if not data:
            return cls()
        default = float(data.get("default", 0.0))
        raw_rates = data.get("category_rates", {})
        category_rates: Dict[str, float] = {}
        if isinstance(raw_rates, Mapping):
            for key, value in raw_rates.items():
                try:
                    category_rates[str(key)] = float(value)
                except (TypeError, ValueError):
                    continue
        return cls(default=default, category_rates=category_rates)

    def rate_for(self, category: str) -> float:
        return self.category_rates.get(category, self.default)


@dataclass
class PerishabilityConfig:
    """Configuration describing how aggressively items expire per category."""

    default_days: int = 7
    category_days: Dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, object]]) -> "PerishabilityConfig":
        if not data:
            return cls()
        default_days = int(data.get("default", 7))
        raw_mapping = data.get("category_days") or data.get("perishability") or {}
        category_days: Dict[str, int] = {}
        if isinstance(raw_mapping, Mapping):
            for key, value in raw_mapping.items():
                try:
                    category_days[str(key)] = int(value)
                except (TypeError, ValueError):
                    continue
        return cls(default_days=default_days, category_days=category_days)

    def window_for(self, category: str) -> int:
        return self.category_days.get(category, self.default_days)


@dataclass
class SimulatorConfig:
    """Top-level configuration for the simulator service."""

    sell_down: RateConfig = field(default_factory=RateConfig)
    receiving: RateConfig = field(default_factory=RateConfig)
    daily_expiry: PerishabilityConfig = field(default_factory=PerishabilityConfig)

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> "SimulatorConfig":
        return cls(
            sell_down=RateConfig.from_mapping(_get_mapping(data, "sell_down")),
            receiving=RateConfig.from_mapping(_get_mapping(data, "receiving")),
            daily_expiry=PerishabilityConfig.from_mapping(
                _get_mapping(data, "daily_expiry")
            ),
        )


def load_config(path: Path) -> SimulatorConfig:
    """Load the simulator configuration from YAML."""

    with path.open("r", encoding="utf-8") as handle:
        text = handle.read()
    if yaml is not None:
        data = yaml.safe_load(text) or {}
    else:
        data = _parse_simple_yaml(text)
    if not isinstance(data, Mapping):
        raise ValueError("Simulator configuration must be a mapping")
    return SimulatorConfig.from_mapping(data)


def _get_mapping(data: Mapping[str, object], key: str) -> MutableMapping[str, object]:
    value = data.get(key)
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _parse_simple_yaml(text: str) -> Dict[str, object]:
    """Fallback YAML parser for simple key/value structures."""

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
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


__all__ = ["SimulatorConfig", "RateConfig", "PerishabilityConfig", "load_config"]
