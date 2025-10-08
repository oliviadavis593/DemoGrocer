"""CLI entry-point for running the FoodFlow reporting web server."""
from __future__ import annotations

import argparse
import logging
import os
from typing import Sequence

import uvicorn

from .app import create_app

LOGGER = logging.getLogger("foodflow.web")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the FoodFlow reporting web server")
    parser.add_argument(
        "--host",
        default=os.getenv("FOODFLOW_WEB_HOST", "0.0.0.0"),
        help="Host interface to bind (default: %(default)s)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("FOODFLOW_WEB_PORT", "8000")),
        help="Port to listen on (default: %(default)s)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Run the ASGI server hosting the web application."""

    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    app = create_app()
    LOGGER.info("Starting FoodFlow web server on http://%s:%s", args.host, args.port)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
