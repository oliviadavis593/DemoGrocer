"""SQLite migration script for development event storage."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from packages.db import create_all, db_session, ensure_db_path, get_db_path

LOGGER = logging.getLogger("foodflow.migrations")

CREATE_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS inventory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    product TEXT NOT NULL,
    lot TEXT,
    qty REAL NOT NULL,
    before_qty REAL NOT NULL,
    after_qty REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'simulator',
    created_at TEXT NOT NULL DEFAULT (DATETIME('now'))
)
"""

CREATE_INTEGRATION_RUNS_TABLE = """
CREATE TABLE IF NOT EXISTS integration_runs (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_sync TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
    updated_at TEXT NOT NULL DEFAULT (DATETIME('now'))
)
"""

CREATE_TS_INDEX = "CREATE INDEX IF NOT EXISTS idx_inventory_events_ts ON inventory_events (ts)"
CREATE_TYPE_TS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_inventory_events_type_ts ON inventory_events (type, ts)"
)


def run(db_path: Path | None = None) -> Path:
    """Execute migrations and return the database path."""

    target_path = ensure_db_path(db_path)
    with db_session(target_path) as conn:
        conn.execute(CREATE_EVENTS_TABLE)
        conn.execute(CREATE_INTEGRATION_RUNS_TABLE)
        conn.execute(CREATE_TS_INDEX)
        conn.execute(CREATE_TYPE_TS_INDEX)
    try:
        create_all(target_path)
    except Exception:
        LOGGER.exception("Failed to create ORM-managed tables via SQLAlchemy metadata")
        raise
    LOGGER.info("Database migrated at %s", target_path)
    return target_path


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Run database migrations for FoodFlow.")
    parser.add_argument(
        "--database",
        type=Path,
        default=get_db_path(),
        help="Path to the SQLite database file (default: %(default)s)",
    )
    args = parser.parse_args()
    run(args.database)


if __name__ == "__main__":
    main()
