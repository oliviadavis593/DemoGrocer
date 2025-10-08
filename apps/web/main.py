"""CLI entry-point for running the FoodFlow reporting web server."""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv
import uvicorn

from packages.odoo_client import OdooClient, OdooClientError
from services.simulator.inventory import InventoryRepository

from .app import create_app

LOGGER = logging.getLogger("foodflow.web")
ROOT = Path(__file__).resolve().parents[2]


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    host_default = os.getenv("FOODFLOW_WEB_HOST", "0.0.0.0")
    port_default = int(os.getenv("PORT") or os.getenv("FOODFLOW_WEB_PORT", "8000"))
    parser = argparse.ArgumentParser(description="Run the FoodFlow reporting web server")
    parser.add_argument(
        "--host",
        default=host_default,
        help="Host interface to bind (default: %(default)s)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=port_default,
        help="Port to listen on (default: %(default)s)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Run the ASGI server hosting the web application."""

    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    args = _parse_args(argv)

    odoo_client = _init_odoo_client()
    repository_factory = (
        (lambda: InventoryRepository(odoo_client)) if odoo_client is not None else (lambda: None)
    )
    odoo_provider = lambda: odoo_client

    app = create_app(
        repository_factory=repository_factory,
        odoo_client_provider=odoo_provider,
        logger=LOGGER,
    )
    LOGGER.info("Starting FoodFlow web server on http://%s:%s", args.host, args.port)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def _init_odoo_client() -> OdooClient | None:
    try:
        client = OdooClient()
        client.authenticate()
        LOGGER.info("Authenticated with Odoo at %s", client.url)
        return client
    except OdooClientError as exc:
        LOGGER.error("Failed to authenticate with Odoo: %s", exc, exc_info=True)
        return None
    except Exception:
        LOGGER.exception("Unexpected error while creating Odoo client")
        return None


if __name__ == "__main__":
    main()
