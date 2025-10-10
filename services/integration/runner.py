"""Command-line interface for the integration service."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from packages.odoo_client import OdooClientError

from services.integration.config import DEFAULT_CONFIG_PATH, IntegrationConfig, load_config
from services.integration.odoo_service import OdooService
from services.integration.shrink_detector import detect_flags


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


def _resolve_summary_limit(args: argparse.Namespace, config: IntegrationConfig) -> int:
    summary_limit = config.inventory.summary_limit
    if getattr(args, "summary_limit", None) is not None:
        summary_limit = max(0, args.summary_limit)
    return summary_limit


def _build_service(config: IntegrationConfig, logger: logging.Logger) -> OdooService:
    return OdooService(lot_expiry_field=config.inventory.lot_expiry_field, logger=logger)


def cmd_sync(args: argparse.Namespace) -> int:
    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    config = _load_config(config_path)
    summary_limit = _resolve_summary_limit(args, config)

    _configure_logging(config.log_level)
    logger = logging.getLogger("foodflow.integration.runner")

    service = _build_service(config, logger.getChild("service"))
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


def cmd_snapshot(args: argparse.Namespace) -> int:
    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    config = _load_config(config_path)
    summary_limit = _resolve_summary_limit(args, config)

    _configure_logging(config.log_level)
    logger = logging.getLogger("foodflow.integration.runner")

    service = _build_service(config, logger.getChild("service"))
    try:
        rows = service.fetch_inventory_snapshot()
    except OdooClientError:
        logger.exception("Integration snapshot failed due to Odoo authentication error")
        return 1
    except Exception:
        logger.exception("Integration snapshot failed with an unexpected error")
        return 1

    total = len(rows)
    print(f"Inventory rows: {total}")
    sample = rows[:summary_limit] if summary_limit else []
    for row in sample:
        product = str(row.get("product") or "")
        lot = row.get("lot") or "-"
        quantity = row.get("quantity")
        try:
            quantity_display = f"{float(quantity):.2f}"
        except (TypeError, ValueError):
            quantity_display = str(quantity)
        locations = ", ".join(row.get("locations") or []) or "-"
        life_date = row.get("life_date") or "-"
        print(f"- {product} lot={lot} qty={quantity_display} locations={locations} expiry={life_date}")
    return 0


def cmd_detect(args: argparse.Namespace) -> int:
    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    config = _load_config(config_path)

    _configure_logging(config.log_level)
    logger = logging.getLogger("foodflow.integration.runner")

    service = _build_service(config, logger.getChild("service"))
    try:
        flags = detect_flags(
            service,
            near_expiry_days=args.days,
            low_movement_window_days=args.movement_window,
            low_movement_min_units=args.min_units,
            overstock_window_days=args.overstock_window,
            overstock_target_days=args.target_days,
        )
    except OdooClientError:
        logger.exception("Integration detect failed due to Odoo authentication error")
        return 1
    except Exception:
        logger.exception("Integration detect failed with an unexpected error")
        return 1

    print(json.dumps(flags, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FoodFlow integration service runner")
    parser.add_argument("command", choices=["sync", "snapshot", "detect"], help="Command to execute")
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
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Near-expiry threshold in days for the detect command (default: %(default)s)",
    )
    parser.add_argument(
        "--movement-window",
        type=int,
        default=7,
        help="Sales history window (days) for low-movement detection (default: %(default)s)",
    )
    parser.add_argument(
        "--min-units",
        type=float,
        default=12.0,
        help="Minimum units sold within the movement window before flagging low movement (default: %(default)s)",
    )
    parser.add_argument(
        "--overstock-window",
        type=int,
        default=7,
        help="Sales history window (days) for overstock detection (default: %(default)s)",
    )
    parser.add_argument(
        "--target-days",
        type=float,
        default=21.0,
        help="Target days of supply threshold for overstock detection (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    if args.command == "sync":
        return cmd_sync(args)
    if args.command == "snapshot":
        return cmd_snapshot(args)
    if args.command == "detect":
        return cmd_detect(args)
    parser.error(f"Unknown command {args.command}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
