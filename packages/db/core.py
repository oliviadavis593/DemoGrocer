"""Core database utilities."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = Path(os.getenv("FOODFLOW_DB_PATH", "out/foodflow.db"))


def get_db_path() -> Path:
    """Return the configured database path."""

    return DEFAULT_DB_PATH


def ensure_db_path(path: Path | None = None) -> Path:
    """Ensure the database directory exists and return the absolute path."""

    db_path = Path(path or get_db_path())
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Create a sqlite3 connection with sensible defaults."""

    db_path = ensure_db_path(path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Context manager that commits on success and closes the connection."""

    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


__all__ = ["connect", "db_session", "ensure_db_path", "get_db_path", "DEFAULT_DB_PATH"]
