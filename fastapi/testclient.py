"""Simplified test client for the FastAPI stub."""
from __future__ import annotations

from typing import Any, Mapping, Optional

from .app import FastAPI


class TestClient:
    __test__ = False

    def __init__(self, app: FastAPI) -> None:
        self.app = app

    def _prepare(self, path: str, params: Optional[Mapping[str, Any]]) -> tuple[str, dict[str, Any]]:
        query: dict[str, Any] = {}
        if params:
            query.update(params)
        if "?" in path:
            from urllib.parse import urlparse, parse_qs

            parsed = urlparse(path)
            path = parsed.path
            derived = parse_qs(parsed.query)
            for key, values in derived.items():
                if not values:
                    continue
                query[key] = values[-1]
        return path, query

    def get(self, path: str, params: Optional[Mapping[str, Any]] = None):
        clean_path, query = self._prepare(path, params)
        return self.app._handle_request("GET", clean_path, query)

    def post(
        self,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json: Any = None,
    ):
        clean_path, query = self._prepare(path, params)
        return self.app._handle_request("POST", clean_path, query, json)
