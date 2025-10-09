"""High level simulator orchestration."""
from __future__ import annotations

from datetime import datetime, timezone
from random import Random
from typing import List, Optional, Sequence

from packages.odoo_client import OdooClient

from services.analysis.shrink_triggers import ShrinkTriggerDetector

from .config import SimulatorConfig
from .events import EventWriter, SimulatorEvent
from .inventory import InventoryRepository
from .jobs import DailyExpiryJob, JobContext, ReceivingJob, ReturnsJob, SellDownJob, ShrinkJob
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
        rng: Optional[Random] = None,
        shrink_detector: Optional[ShrinkTriggerDetector] = None,
    ) -> None:
        self.client = client
        self.config = config
        self.event_writer = event_writer
        self.state_tracker = state_tracker
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.inventory = InventoryRepository(client)
        self.rng = rng
        self.shrink_detector = shrink_detector

    def run_once(self, force: bool = False) -> Sequence[SimulatorEvent]:
        now = self.now_fn()
        snapshot = self.inventory.load_snapshot()
        context = JobContext(now=now, snapshot=snapshot)
        jobs = self._build_jobs()
        all_events: List[SimulatorEvent] = []
        for job in jobs:
            if job.minimum_interval is not None:
                if not self.state_tracker.should_run(
                    job.name, now, force=force, minimum_interval=job.minimum_interval
                ):
                    continue
                events = job.run(context)
                self.state_tracker.record(job.name, now)
            else:
                events = job.run(context)
            if events:
                all_events.extend(events)
        if self.shrink_detector is not None:
            analysis_events = self.shrink_detector.evaluate(now, snapshot)
            if analysis_events:
                self.event_writer.write(analysis_events)
                all_events.extend(analysis_events)
        return all_events

    def _build_jobs(self) -> Sequence:
        return [
            SellDownJob(self.config.sell_down, self.event_writer, self.client),
            ReturnsJob(self.config.returns, self.event_writer, self.client, rng=self.rng),
            DailyExpiryJob(self.config.daily_expiry, self.event_writer, self.client),
            ShrinkJob(self.config.shrink, self.event_writer, self.client, rng=self.rng),
            ReceivingJob(
                self.config.receiving,
                self.config.daily_expiry,
                self.event_writer,
                self.client,
            ),
        ]


__all__ = ["SimulatorService"]
