"""Inventory simulator service."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

_EXPORTS = {
    "SimulatorConfig": ("services.simulator.config", "SimulatorConfig"),
    "load_config": ("services.simulator.config", "load_config"),
    "SimulatorService": ("services.simulator.service", "SimulatorService"),
    "SimulatorScheduler": ("services.simulator.scheduler", "SimulatorScheduler"),
}

__all__ = list(_EXPORTS.keys())

if TYPE_CHECKING:  # pragma: no cover - import side effects only for typing
    from .config import SimulatorConfig, load_config
    from .scheduler import SimulatorScheduler
    from .service import SimulatorService


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if not target:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value
    return value
