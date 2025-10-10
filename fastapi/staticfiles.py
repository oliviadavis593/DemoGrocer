"""Minimal StaticFiles stub matching FastAPI signature for tests."""
from __future__ import annotations

from pathlib import Path
from typing import Any


class StaticFiles:
    """Placeholder ASGI app for mounted static directories in tests."""

    def __init__(self, *, directory: str | Path, html: bool = False) -> None:
        self.directory = Path(directory)
        self.html = html

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:  # pragma: no cover - not used in tests
        raise RuntimeError("StaticFiles stub does not handle ASGI requests in tests")
