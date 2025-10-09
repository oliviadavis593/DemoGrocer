"""Command-line entry point for the simulator."""
from __future__ import annotations

import argparse
import logging
import os
from datetime import timedelta
from pathlib import Path

from packages.odoo_client import OdooClient

from services.analysis.shrink_triggers import ShrinkTriggerDetector, load_config as load_shrink_trigger_config

from .config import load_config
from .events import EventWriter
from .scheduler import SimulatorScheduler
from .service import SimulatorService
from .state import StateTracker


DEFAULT_CONFIG_PATH = Path("config/simulator.yaml")
DEFAULT_SHRINK_TRIGGER_CONFIG_PATH = Path("config/shrink_triggers.yaml")
DEFAULT_EVENTS_PATH = Path("out/events.jsonl")
DEFAULT_STATE_PATH = Path("out/simulator_state.json")
DEFAULT_INTERVAL_SECONDS = 60 * 60  # hourly by default


def build_service() -> SimulatorService:
    client = OdooClient()
    client.authenticate()
    config = load_config(DEFAULT_CONFIG_PATH)
    event_writer = EventWriter(DEFAULT_EVENTS_PATH)
    shrink_config = load_shrink_trigger_config(DEFAULT_SHRINK_TRIGGER_CONFIG_PATH)
    shrink_detector = ShrinkTriggerDetector(getattr(event_writer, "store", None), shrink_config)
    state_tracker = StateTracker(DEFAULT_STATE_PATH, timedelta(hours=24))
    return SimulatorService(
        client,
        config,
        event_writer,
        state_tracker,
        shrink_detector=shrink_detector,
    )


def cmd_once(args: argparse.Namespace) -> None:
    service = build_service()
    events = service.run_once(force=True)
    logging.info("Simulator once run emitted %d events", len(events))


def cmd_start(args: argparse.Namespace) -> None:
    service = build_service()
    interval = int(os.getenv("SIMULATOR_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS))
    scheduler = SimulatorScheduler(service, interval_seconds=interval)
    try:
        scheduler.run()
    except KeyboardInterrupt:
        logging.info("Simulator stopped by user")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="FoodFlow inventory simulator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("once", help="Run simulator jobs once")
    subparsers.add_parser("start", help="Start the simulator scheduler")

    args = parser.parse_args()
    if args.command == "once":
        cmd_once(args)
    elif args.command == "start":
        cmd_start(args)


if __name__ == "__main__":
    main()
