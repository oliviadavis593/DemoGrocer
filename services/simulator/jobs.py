"""Simulator job implementations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, List, Sequence

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

    def run(self, context: JobContext) -> Sequence[SimulatorEvent]:  # pragma: no cover - interface
        raise NotImplementedError


class SellDownJob(BaseJob):
    """Reduce inventory quantities to mimic customer purchases."""

    name = "sell_down"

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

    def __init__(self, config: RateConfig, writer, client) -> None:
        self.config = config
        self.writer = writer
        self.client = client

    def run(self, context: JobContext) -> Sequence[SimulatorEvent]:
        events: List[SimulatorEvent] = []
        for quant in context.snapshot.quants():
            rate = self.config.rate_for(quant.category)
            if rate <= 0:
                continue
            before = quant.quantity
            after = round(before + rate, 2)
            if _is_close(before, after):
                continue
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


class DailyExpiryJob(BaseJob):
    """Reduce or remove inventory as items approach expiry."""

    name = "daily_expiry"

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


__all__ = [
    "BaseJob",
    "SellDownJob",
    "ReceivingJob",
    "DailyExpiryJob",
    "JobContext",
]
