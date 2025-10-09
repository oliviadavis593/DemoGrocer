"""CLI to quarantine recalled products."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Sequence

from packages.db import EventStore
from packages.odoo_client import OdooClient, OdooClientError
from services.recall import RecallService
from services.simulator.events import EventWriter

LOGGER = logging.getLogger("foodflow.recall")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Quarantine recalled products by code or category.")
    parser.add_argument(
        "--codes",
        help="Comma-separated list of product default codes to quarantine.",
        default="",
    )
    parser.add_argument(
        "--categories",
        help="Comma-separated list of product categories to quarantine.",
        default="",
    )
    return parser.parse_args(argv)


def _split_arg(value: str) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parse_args(argv)
    codes = _split_arg(args.codes)
    categories = _split_arg(args.categories)

    if not codes and not categories:
        LOGGER.error("Provide at least one default code via --codes or category via --categories.")
        return 1

    try:
        client = OdooClient()
        client.authenticate()
    except OdooClientError as exc:
        LOGGER.error("Failed to authenticate with Odoo: %s", exc)
        return 2
    except Exception:
        LOGGER.exception("Unexpected error while creating Odoo client")
        return 3

    events_path = Path("out/events.jsonl")
    event_writer = EventWriter(events_path, store=EventStore())
    service = RecallService(client, event_writer)

    try:
        results = service.recall(default_codes=codes, categories=categories)
    except Exception:
        LOGGER.exception("Failed to quarantine recalled inventory")
        return 4

    if not results:
        LOGGER.info("No matching inventory found for recall. Nothing quarantined.")
        return 0

    for result in results:
        LOGGER.info(
            "Quarantined %.2f units of %s (code=%s, lot=%s) from %s → %s",
            result.quantity,
            result.product,
            result.default_code or "-",
            result.lot or "-",
            result.source_location,
            result.destination_location,
        )
    LOGGER.info("Recorded %d recall events.", len(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
