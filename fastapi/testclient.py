"""Simplified test client for the FastAPI stub."""
from __future__ import annotations

from typing import Any, Mapping, Optional

from .app import FastAPI


class TestClient:
    __test__ = False

    def __init__(self, app: FastAPI) -> None:
        self.app = app

    def get(self, path: str, params: Optional[Mapping[str, Any]] = None):
        return self.app._handle_request("GET", path, params)
