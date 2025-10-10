"""Core data structures for shrink decision recommendations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class Decision:
    """Final recommendation for handling a flagged inventory record."""

    default_code: Optional[str]
    lot: Optional[str]
    reason: str
    outcome: str
    suggested_qty: Optional[float] = None
    notes: Optional[str] = None
    price_markdown_pct: Optional[float] = None

    def to_dict(self) -> Dict[str, object]:
        """Return a compact JSON-serialisable representation."""

        payload: Dict[str, object] = {
            "default_code": self.default_code,
            "lot": self.lot,
            "reason": self.reason,
            "outcome": self.outcome,
            "suggested_qty": self.suggested_qty,
            "notes": self.notes,
            "price_markdown_pct": self.price_markdown_pct,
        }
        return {key: value for key, value in payload.items() if value is not None}


__all__ = ["Decision"]
