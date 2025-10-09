"""Command-line interface for the integration service."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from packages.odoo_client import OdooClientError

from .config import DEFAULT_CONFIG_PATH, IntegrationConfig, load_config
from .odoo_service import OdooService


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")


def _load_config(path: Path) -> IntegrationConfig:
    try:
        return load_config(path)
    except FileNotFoundError:
        logging.getLogger("foodflow.integration.runner").warning(
            "Configuration file %s not found; using defaults", path
        )
        return IntegrationConfig()


def cmd_sync(args: argparse.Namespace) -> int:
    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    config = _load_config(config_path)
    summary_limit = config.inventory.summary_limit
    if args.summary_limit is not None:
        summary_limit = max(0, args.summary_limit)

    _configure_logging(config.log_level)
    logger = logging.getLogger("foodflow.integration.runner")

    service = OdooService(lot_expiry_field=config.inventory.lot_expiry_field)
    try:
        result = service.sync(summary_limit=summary_limit)
    except OdooClientError:
        logger.exception("Integration sync failed due to Odoo authentication error")
        return 1
    except Exception:
        logger.exception("Integration sync failed with an unexpected error")
        return 1

    logger.info("Integration sync processed %d quants", result.total_quants)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FoodFlow integration service runner")
    parser.add_argument("command", choices=["sync"], help="Command to execute")
    parser.add_argument(
        "-c",
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to integration config file (default: %(default)s)",
    )
    parser.add_argument(
        "--summary-limit",
        type=int,
        help="Override number of inventory quants to include in sync log sample",
    )
    args = parser.parse_args(argv)

    if args.command == "sync":
        return cmd_sync(args)
    parser.error(f"Unknown command {args.command}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
