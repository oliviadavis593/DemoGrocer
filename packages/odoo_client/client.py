"""Minimal XML-RPC client for Odoo."""
from __future__ import annotations

import os
import sys
import pathlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Union
import xmlrpc.client
# ``python-dotenv`` is an optional dependency when running unit tests. The
# simulator and web application can operate without it, so gracefully fall back
# when the package is not installed.
try:  # pragma: no cover - optional helper
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - optional helper
    def load_dotenv(*_: object, **__: object) -> bool:
        return False

# Load environment variables from .env file
ROOT = pathlib.Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
# Make "packages/..." importable
sys.path.insert(0, str(ROOT))


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
            "database": os.getenv("ODOO_DB") or os.getenv("ODOO_DATABASE"),
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
        proxy_options = {"allow_none": True}
        self._common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", **proxy_options)
        self._object = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", **proxy_options)

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

    def create(
        self,
        model: str,
        values: Dict[str, Any],
        *,
        context: Optional[Dict[str, Any]] = None,
    ) -> int:
        self._ensure_authenticated()
        kwargs: Dict[str, Any] = {}
        if context:
            kwargs["context"] = context
        record_id = self._object.execute_kw(
            self.database,
            self._uid,
            self.password,
            model,
            "create",
            [values],
            kwargs,
        )
        return int(record_id)

    def write(
        self,
        model: str,
        ids: Union[int, Sequence[int]],
        values: Dict[str, Any],
        *,
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        self._ensure_authenticated()
        record_ids: List[int]
        if isinstance(ids, int):
            record_ids = [ids]
        else:
            record_ids = [int(i) for i in ids]
        kwargs: Dict[str, Any] = {}
        if context:
            kwargs["context"] = context
        return bool(
            self._object.execute_kw(
                self.database,
                self._uid,
                self.password,
                model,
                "write",
                [record_ids, values],
                kwargs,
            )
        )

    def call(
        self,
        model: str,
        method: str,
        args: Optional[Sequence[Any]] = None,
        *,
        context: Optional[Dict[str, Any]] = None,
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> Any:
        self._ensure_authenticated()
        call_args: List[Any] = list(args or [])
        call_kwargs: Dict[str, Any] = dict(kwargs or {})
        if context:
            call_kwargs["context"] = context
        return self._object.execute_kw(
            self.database,
            self._uid,
            self.password,
            model,
            method,
            call_args,
            call_kwargs,
        )

    # Internal helpers ------------------------------------------------------------
    def _ensure_authenticated(self) -> None:
        if self._uid is None:
            raise OdooClientError(
                "Client is not authenticated. Call authenticate() before making requests."
            )


__all__ = ["OdooClient", "OdooClientError", "OdooClientConfig"]
