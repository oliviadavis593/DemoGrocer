"""Simulator job implementations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, List, Optional, Sequence, Tuple

from .config import PerishabilityConfig, RateConfig
from .events import SimulatorEvent
from .inventory import InventorySnapshot


@dataclass
class JobContext:
    """Context provided to each job run."""

    now: datetime
    snapshot: InventorySnapshot


class BaseJob:
    """Base class for simulator jobs."""

    name: str
    minimum_interval: Optional[timedelta] = None

    def run(self, context: JobContext) -> Sequence[SimulatorEvent]:  # pragma: no cover - interface
        raise NotImplementedError


class SellDownJob(BaseJob):
    """Reduce inventory quantities to mimic customer purchases."""

    name = "sell_down"
    minimum_interval: Optional[timedelta] = None

    def __init__(self, config: RateConfig, writer, client) -> None:
        self.config = config
        self.writer = writer
        self.client = client

    def run(self, context: JobContext) -> Sequence[SimulatorEvent]:
        events: List[SimulatorEvent] = []
        for quant in context.snapshot.quants():
            rate = self.config.rate_for(quant.category)
            before = quant.quantity
            if before <= 0 or rate <= 0:
                continue
            delta = before * rate
            after = max(before - delta, 0.0)
            if _is_close(before, after):
                continue
            after = round(after, 2)
            self.client.write("stock.quant", quant.id, {"quantity": after})
            context.snapshot.update_quantity(quant.id, after)
            events.append(
                SimulatorEvent(
                    ts=context.now,
                    type=self.name,
                    product=quant.product_name,
                    lot=quant.lot_name,
                    qty=after - before,
                    before=before,
                    after=after,
                )
            )
        self.writer.write(events)
        return events


class ReceivingJob(BaseJob):
    """Increase inventory quantities to simulate inbound receipts."""

    name = "receiving"

    def __init__(
        self,
        config: RateConfig,
        perishability: PerishabilityConfig,
        writer,
        client,
    ) -> None:
        self.config = config
        self.perishability = perishability
        self.writer = writer
        self.client = client

    def run(self, context: JobContext) -> Sequence[SimulatorEvent]:
        events: List[SimulatorEvent] = []
        today = context.now.date()
        for quant in context.snapshot.quants():
            rate = self.config.rate_for(quant.category)
            if rate <= 0:
                continue
            before = quant.quantity
            after = round(before + rate, 2)
            if _is_close(before, after):
                continue
            lot_id, lot_name, life_date = self._ensure_lot(context, quant, today)
            values = {"quantity": after}
            if lot_id is not None:
                values["lot_id"] = lot_id
            self.client.write("stock.quant", quant.id, values)
            context.snapshot.update_quant(
                quant.id,
                quantity=after,
                lot_id=lot_id,
                lot_name=lot_name,
                life_date=life_date,
            )
            events.append(
                SimulatorEvent(
                    ts=context.now,
                    type=self.name,
                    product=quant.product_name,
                    lot=lot_name or quant.lot_name,
                    qty=after - before,
                    before=before,
                    after=after,
                )
            )
        self.writer.write(events)
        return events

    def _ensure_lot(self, context: JobContext, quant, today: date) -> Tuple[Optional[int], Optional[str], Optional[date]]:
        """Return lot metadata, creating a new lot when the current one is unusable."""

        needs_new_lot = (
            quant.lot_id is None
            or quant.quantity <= 0
            or (quant.life_date is not None and quant.life_date <= today)
        )
        if not needs_new_lot:
            return quant.lot_id, quant.lot_name, quant.life_date

        life_window = max(self.perishability.window_for(quant.category), 1)
        life_date = today + timedelta(days=life_window)
        lot_name = self._generate_lot_name(quant.product_name, quant.product_id, context.now)
        existing = self._find_existing_lot(lot_name)
        if existing:
            lot_id, existing_date = existing
            return lot_id, lot_name, existing_date or life_date

        lot_id = self.client.create(
            "stock.lot",
            {
                "name": lot_name,
                "product_id": quant.product_id,
                "expiration_date": life_date.isoformat(),
            },
        )
        return lot_id, lot_name, life_date

    def _find_existing_lot(self, lot_name: str) -> Optional[Tuple[int, Optional[date]]]:
        records = self.client.search_read(
            "stock.lot",
            domain=[["name", "=", lot_name]],
            fields=["id", "expiration_date"],
        )
        if not records:
            return None
        record = records[0]
        lot_id = int(record["id"])
        life_date = _parse_date(record.get("expiration_date"))
        return lot_id, life_date

    def _generate_lot_name(self, product_name: str, product_id: int, now: datetime) -> str:
        slug = "".join(ch for ch in product_name.upper() if ch.isalnum())
        slug = slug[:12] or f"P{product_id}"
        return f"SIM-{slug}-{now.strftime('%Y%m%d%H%M%S')}"


class DailyExpiryJob(BaseJob):
    """Reduce or remove inventory as items approach expiry."""

    name = "daily_expiry"
    minimum_interval: Optional[timedelta] = timedelta(hours=24)

    def __init__(self, config: PerishabilityConfig, writer, client) -> None:
        self.config = config
        self.writer = writer
        self.client = client

    def run(self, context: JobContext) -> Sequence[SimulatorEvent]:
        events: List[SimulatorEvent] = []
        today = context.now.date()
        for quant in context.snapshot.quants():
            life_date = quant.life_date
            if life_date is None:
                continue
            window = max(self.config.window_for(quant.category), 1)
            before = quant.quantity
            if before <= 0:
                continue
            days_until = (life_date - today).days
            if days_until < 0:
                after = 0.0
            elif days_until < window:
                reduction_fraction = (window - days_until) / window
                after = max(before * (1 - reduction_fraction), 0.0)
            else:
                continue
            after = round(after, 2)
            if _is_close(before, after):
                continue
            self.client.write("stock.quant", quant.id, {"quantity": after})
            context.snapshot.update_quantity(quant.id, after)
            # Note: expiration_state field not available in this Odoo instance
            # if quant.lot_id is not None:
            #     lot_updates = {}
            #     if after <= 0:
            #         lot_updates["expiration_state"] = "expired"
            #     elif days_until < window:
            #         lot_updates["expiration_state"] = "near_expiry"
            #     if lot_updates:
            #         self.client.write("stock.lot", quant.lot_id, lot_updates)
            events.append(
                SimulatorEvent(
                    ts=context.now,
                    type=self.name,
                    product=quant.product_name,
                    lot=quant.lot_name,
                    qty=after - before,
                    before=before,
                    after=after,
                )
            )
        self.writer.write(events)
        return events


def _is_close(a: float, b: float, tolerance: float = 0.01) -> bool:
    return abs(a - b) < tolerance


def _parse_date(value: object) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None
    return None


__all__ = [
    "BaseJob",
    "SellDownJob",
    "ReceivingJob",
    "DailyExpiryJob",
    "JobContext",
]
