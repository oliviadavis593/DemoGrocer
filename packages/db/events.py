"""Inventory event persistence helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from .core import db_session


@dataclass
class InventoryEvent:
    """Representation of an inventory event row."""

    ts: datetime
    type: str
    product: str
    lot: str | None
    qty: float
    before: float
    after: float
    source: str = "simulator"

    def as_db_params(self) -> Sequence[object]:
        ts_value = self.ts.astimezone(timezone.utc).isoformat()
        return (ts_value, self.type, self.product, self.lot, self.qty, self.before, self.after, self.source)

    @classmethod
    def from_row(cls, row) -> "InventoryEvent":
        ts_raw = row["ts"]
        ts = datetime.fromisoformat(ts_raw) if isinstance(ts_raw, str) else datetime.fromtimestamp(0, tz=timezone.utc)
        lot_value = row["lot"] if row["lot"] not in ("", None) else None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        return cls(
            ts=ts,
            type=row["type"],
            product=row["product"],
            lot=lot_value,
            qty=float(row["qty"]),
            before=float(row["before_qty"]),
            after=float(row["after_qty"]),
            source=row["source"] or "simulator",
        )


class EventStore:
    """Read and write inventory events."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path

    def add_events(self, events: Iterable[InventoryEvent]) -> int:
        payload = [event.as_db_params() for event in events]
        if not payload:
            return 0
        with db_session(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO inventory_events (ts, type, product, lot, qty, before_qty, after_qty, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            return conn.total_changes

    def list_events(
        self,
        *,
        event_type: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[InventoryEvent]:
        query = ["SELECT ts, type, product, lot, qty, before_qty, after_qty, source FROM inventory_events"]
        clauses = []
        params: List[object] = []
        if event_type:
            clauses.append("type = ?")
            params.append(event_type)
        if since:
            clauses.append("ts >= ?")
            params.append(since.astimezone(timezone.utc).isoformat())
        if clauses:
            query.append("WHERE " + " AND ".join(clauses))
        query.append("ORDER BY ts DESC")
        query.append("LIMIT ?")
        params.append(int(limit))
        sql = " ".join(query)
        with db_session(self.db_path) as conn:
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
        return [InventoryEvent.from_row(row) for row in rows]

    def metrics_summary(self) -> dict[str, object]:
        with db_session(self.db_path) as conn:
            totals_row = conn.execute("SELECT COUNT(*) AS total FROM inventory_events").fetchone()
            by_type_cursor = conn.execute(
                "SELECT type, COUNT(*) AS count FROM inventory_events GROUP BY type ORDER BY type"
            )
            by_type = {row["type"]: row["count"] for row in by_type_cursor.fetchall()}
        total = totals_row["total"] if totals_row is not None else 0
        return {"total_events": total, "events_by_type": by_type}

    def record_integration_sync(self, timestamp: datetime) -> None:
        """Persist the timestamp of the latest integration sync."""

        ts_value = timestamp.astimezone(timezone.utc).isoformat()
        updated_value = datetime.now(timezone.utc).isoformat()
        with db_session(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO integration_runs (id, last_sync, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET last_sync=excluded.last_sync, updated_at=excluded.updated_at
                """,
                (ts_value, updated_value),
            )

    def get_last_integration_sync(self) -> datetime | None:
        """Return the timestamp of the most recent integration sync if recorded."""

        with db_session(self.db_path) as conn:
            row = conn.execute("SELECT last_sync FROM integration_runs WHERE id = 1").fetchone()
        if row is None:
            return None
        raw_value = row["last_sync"]
        if not isinstance(raw_value, str):
            return None
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)


__all__ = ["EventStore", "InventoryEvent"]
