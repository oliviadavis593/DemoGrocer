"""Simple scheduler for running simulator jobs on an interval."""
from __future__ import annotations

import logging
import time
from typing import Optional

from .service import SimulatorService


logger = logging.getLogger(__name__)


class SimulatorScheduler:
    """Run the simulator service on a fixed cadence."""

    def __init__(self, service: SimulatorService, interval_seconds: int) -> None:
        self.service = service
        self.interval_seconds = max(1, int(interval_seconds))

    def run(self, max_ticks: Optional[int] = None) -> None:
        ticks = 0
        while True:
            ticks += 1
            logger.info("Simulator tick %s", ticks)
            self.service.run_once(force=False)
            if max_ticks is not None and ticks >= max_ticks:
                break
            time.sleep(self.interval_seconds)


__all__ = ["SimulatorScheduler"]
