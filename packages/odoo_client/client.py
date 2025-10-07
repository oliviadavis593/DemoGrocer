"""Minimal XML-RPC client for Odoo."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Union
import xmlrpc.client


class OdooClientError(RuntimeError):
    """Raised when the XML-RPC client encounters an error."""


@dataclass
class OdooClientConfig:
    """Configuration for connecting to an Odoo instance."""

    url: str
    database: str
    username: str
    password: str

    @classmethod
    def from_env(cls) -> "OdooClientConfig":
        """Create a configuration by reading environment variables."""
        missing: List[str] = []
        env_map = {
            "url": os.getenv("ODOO_URL"),
            "database": os.getenv("ODOO_DB"),
            "username": os.getenv("ODOO_USERNAME"),
            "password": os.getenv("ODOO_PASSWORD"),
        }
        for key, value in env_map.items():
            if not value:
                missing.append(f"ODOO_{key.upper()}")
        if missing:
            raise OdooClientError(
                "Missing required environment variables: " + ", ".join(sorted(missing))
            )
        return cls(
            url=env_map["url"],
            database=env_map["database"],
            username=env_map["username"],
            password=env_map["password"],
        )


class OdooClient:
    """Small helper around the Odoo XML-RPC API."""

    def __init__(
        self,
        url: Optional[str] = None,
        database: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        config = self._build_config(url, database, username, password)
        self.url = config.url.rstrip("/")
        self.database = config.database
        self.username = config.username
        self.password = config.password
        self._uid: Optional[int] = None
        self._common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self._object = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")

    @staticmethod
    def _build_config(
        url: Optional[str],
        database: Optional[str],
        username: Optional[str],
        password: Optional[str],
    ) -> OdooClientConfig:
        if all([url, database, username, password]):
            return OdooClientConfig(url=url, database=database, username=username, password=password)
        if any([url, database, username, password]):
            missing = [
                name
                for name, value in {
                    "url": url,
                    "database": database,
                    "username": username,
                    "password": password,
                }.items()
                if not value
            ]
            raise OdooClientError(
                "Incomplete credentials supplied. Missing: " + ", ".join(sorted(missing))
            )
        return OdooClientConfig.from_env()

    # Public API -----------------------------------------------------------------
    def authenticate(self) -> int:
        """Authenticate with the Odoo server and return the user id."""
        uid = self._common.authenticate(self.database, self.username, self.password, {})
        if not uid:
            raise OdooClientError("Authentication with Odoo failed. Check credentials.")
        self._uid = int(uid)
        return self._uid

    # XML-RPC wrappers ------------------------------------------------------------
    def search_read(
        self,
        model: str,
        domain: Sequence[Any],
        fields: Optional[Sequence[str]] = None,
        limit: Optional[int] = None,
        order: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        self._ensure_authenticated()
        kwargs: Dict[str, Any] = {}
        if fields is not None:
            kwargs["fields"] = list(fields)
        if limit is not None:
            kwargs["limit"] = int(limit)
        if order is not None:
            kwargs["order"] = order
        return self._object.execute_kw(
            self.database,
            self._uid,
            self.password,
            model,
            "search_read",
            [list(domain)],
            kwargs,
        )

    def create(self, model: str, values: Dict[str, Any]) -> int:
        self._ensure_authenticated()
        record_id = self._object.execute_kw(
            self.database,
            self._uid,
            self.password,
            model,
            "create",
            [values],
        )
        return int(record_id)

    def write(self, model: str, ids: Union[int, Sequence[int]], values: Dict[str, Any]) -> bool:
        self._ensure_authenticated()
        record_ids: List[int]
        if isinstance(ids, int):
            record_ids = [ids]
        else:
            record_ids = [int(i) for i in ids]
        return bool(
            self._object.execute_kw(
                self.database,
                self._uid,
                self.password,
                model,
                "write",
                [record_ids, values],
            )
        )

    # Internal helpers ------------------------------------------------------------
    def _ensure_authenticated(self) -> None:
        if self._uid is None:
            raise OdooClientError(
                "Client is not authenticated. Call authenticate() before making requests."
            )


__all__ = ["OdooClient", "OdooClientError", "OdooClientConfig"]
