"""Configuration helpers for the integration service."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, MutableMapping, Optional

try:  # pragma: no cover - optional dependency
    import yaml  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    yaml = None


DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")


@dataclass
class IntegrationInventoryConfig:
    """Inventory-related settings for the integration cycle."""

    summary_limit: int = 5
    lot_expiry_field: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, object]]) -> "IntegrationInventoryConfig":
        if not data:
            return cls()
        raw_limit = data.get("summary_limit", cls.summary_limit)
        try:
            summary_limit = max(0, int(raw_limit))
        except (TypeError, ValueError):
            summary_limit = cls.summary_limit

        raw_field = data.get("lot_expiry_field")
        lot_expiry_field: Optional[str]
        if raw_field in (None, "", "null", "None"):
            lot_expiry_field = None
        else:
            lot_expiry_field = str(raw_field)
        return cls(summary_limit=summary_limit, lot_expiry_field=lot_expiry_field)


@dataclass
class IntegrationConfig:
    """Top-level configuration for the integration runner."""

    log_level: str = "INFO"
    inventory: IntegrationInventoryConfig = field(default_factory=IntegrationInventoryConfig)

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> "IntegrationConfig":
        log_level_value = data.get("log_level", cls.log_level)
        log_level = str(log_level_value).strip() or cls.log_level
        inventory = IntegrationInventoryConfig.from_mapping(_get_mapping(data, "inventory"))
        return cls(log_level=log_level.upper(), inventory=inventory)


def load_config(path: Path | None = None) -> IntegrationConfig:
    """Load integration configuration from YAML."""

    config_path = path or DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return IntegrationConfig()
    text = config_path.read_text(encoding="utf-8")
    if yaml is not None:
        data = yaml.safe_load(text) or {}
    else:
        data = _parse_simple_yaml(text)
    if not isinstance(data, Mapping):
        raise ValueError("Integration configuration must be a mapping")
    return IntegrationConfig.from_mapping(data)


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


__all__ = ["DEFAULT_CONFIG_PATH", "IntegrationConfig", "IntegrationInventoryConfig", "load_config"]
