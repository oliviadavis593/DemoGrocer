"""High level simulator orchestration."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Sequence

from packages.odoo_client import OdooClient

from .config import SimulatorConfig
from .events import EventWriter, SimulatorEvent
from .inventory import InventoryRepository
from .jobs import DailyExpiryJob, JobContext, ReceivingJob, SellDownJob
from .state import StateTracker


class SimulatorService:
    """Run simulator jobs while preventing excessive application."""

    def __init__(
        self,
        client: OdooClient,
        config: SimulatorConfig,
        event_writer: EventWriter,
        state_tracker: StateTracker,
        now_fn=None,
    ) -> None:
        self.client = client
        self.config = config
        self.event_writer = event_writer
        self.state_tracker = state_tracker
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.inventory = InventoryRepository(client)

    def run_once(self, force: bool = False) -> Sequence[SimulatorEvent]:
        now = self.now_fn()
        snapshot = self.inventory.load_snapshot()
        context = JobContext(now=now, snapshot=snapshot)
        jobs = self._build_jobs()
        all_events: List[SimulatorEvent] = []
        for job in jobs:
            if not self.state_tracker.should_run(job.name, now, force=force):
                continue
            events = job.run(context)
            if events:
                all_events.extend(events)
                self.state_tracker.record(job.name, now)
        return all_events

    def _build_jobs(self) -> Sequence:
        return [
            SellDownJob(self.config.sell_down, self.event_writer, self.client),
            ReceivingJob(self.config.receiving, self.event_writer, self.client),
            DailyExpiryJob(self.config.daily_expiry, self.event_writer, self.client),
        ]


__all__ = ["SimulatorService"]
