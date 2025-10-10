"""Periodic shrink detection scheduler with HTTP access to flagged decisions."""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterable, List, Sequence

from fastapi import FastAPI
from fastapi.responses import JSONResponse

try:  # pragma: no cover - uvicorn is optional during unit tests
    import uvicorn  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    uvicorn = None  # type: ignore

from packages.decision.policy import DEFAULT_POLICY_PATH, DecisionMapper
from services.integration.config import DEFAULT_CONFIG_PATH, IntegrationConfig, load_config
from services.integration.odoo_service import OdooService
from services.integration.shrink_detector import detect_flags


DEFAULT_FLAGGED_PATH = Path("out/flagged.json")
DEFAULT_INTERVAL_MINUTES = 10
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000

LOGGER = logging.getLogger("foodflow.integration.schedule")


class FlaggedStore:
    """Thread-safe cache and persistence layer for decision payloads."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._records: list[dict[str, object]] = []
        self._load_existing()

    def _load_existing(self) -> None:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._records = []
            return
        except OSError:
            LOGGER.warning("Failed to read %s; starting with empty cache", self._path)
            self._records = []
            return
        try:
            data = json.loads(text)
            if isinstance(data, list):
                self._records = [dict(item) for item in data if isinstance(item, dict)]
            else:
                self._records = []
        except json.JSONDecodeError:
            LOGGER.warning("Flagged file %s contains invalid JSON; ignoring existing contents", self._path)
            self._records = []

    @property
    def path(self) -> Path:
        return self._path

    def current(self) -> list[dict[str, object]]:
        with self._lock:
            return copy.deepcopy(self._records)

    def update(self, records: Iterable[dict[str, object]]) -> None:
        payload = [dict(record) for record in records]
        json_payload = json.dumps(payload, indent=2, sort_keys=True)
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)

        with self._lock:
            tmp_handle = None
            tmp_path = None
            try:
                tmp_handle = tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=str(directory),
                    delete=False,
                )
                tmp_path = Path(tmp_handle.name)
                tmp_handle.write(json_payload)
                tmp_handle.write("\n")
                tmp_handle.close()
                tmp_path.replace(self._path)
                self._records = payload
                LOGGER.info("Wrote %d flagged decisions to %s", len(payload), self._path)
            except Exception:
                LOGGER.exception("Failed to persist flagged decisions to %s", self._path)
            finally:
                if tmp_handle and not tmp_handle.closed:
                    tmp_handle.close()
                if tmp_path and tmp_path.exists():
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        LOGGER.debug("Unable to remove temporary file %s", tmp_path, exc_info=True)


class DetectionRunner:
    """Execute shrink detection and persist decision payloads."""

    def __init__(
        self,
        *,
        store: FlaggedStore,
        config_path: Path,
        policy_path: Path,
        detection_args: "DetectionArgs",
    ) -> None:
        self._store = store
        self._config_path = config_path
        self._policy_path = policy_path
        self._detection_args = detection_args

    def execute(self) -> None:
        try:
            config = load_config(self._config_path)
        except Exception:
            LOGGER.exception("Failed to load integration configuration from %s", self._config_path)
            return

        service = OdooService(
            lot_expiry_field=config.inventory.lot_expiry_field,
            logger=logging.getLogger("foodflow.integration.schedule.odoo"),
        )
        try:
            flags = detect_flags(
                service,
                near_expiry_days=self._detection_args.near_expiry_days,
                low_movement_window_days=self._detection_args.low_movement_window_days,
                low_movement_min_units=self._detection_args.low_movement_min_units,
                overstock_window_days=self._detection_args.overstock_window_days,
                overstock_target_days=self._detection_args.overstock_target_days,
            )
        except Exception:
            LOGGER.exception("Shrink detection failed; retaining previous flagged state")
            return

        try:
            mapper = DecisionMapper.from_path(self._policy_path)
            decisions = mapper.map_flags(flags)
        except Exception:
            LOGGER.exception("Failed to map flags to decisions using %s", self._policy_path)
            return

        payload = [decision.to_dict() for decision in decisions]
        self._store.update(payload)


class Scheduler(threading.Thread):
    """Background worker that triggers detection on a fixed cadence."""

    def __init__(self, runner: DetectionRunner, interval_seconds: int, stop_event: threading.Event) -> None:
        super().__init__(daemon=True)
        self._runner = runner
        self._interval_seconds = max(1, int(interval_seconds))
        self._stop_event = stop_event

    def run(self) -> None:
        LOGGER.info("Scheduler thread started with interval=%ss", self._interval_seconds)
        while not self._stop_event.is_set():
            start = time.monotonic()
            self._runner.execute()
            elapsed = time.monotonic() - start
            remaining = max(self._interval_seconds - elapsed, 0)
            if self._stop_event.wait(remaining):
                break
        LOGGER.info("Scheduler thread stopped")


class DetectionArgs:
    """Capture shrink detection thresholds supplied by the CLI."""

    def __init__(
        self,
        *,
        near_expiry_days: int,
        low_movement_window_days: int,
        low_movement_min_units: float,
        overstock_window_days: int,
        overstock_target_days: float,
    ) -> None:
        self.near_expiry_days = near_expiry_days
        self.low_movement_window_days = low_movement_window_days
        self.low_movement_min_units = low_movement_min_units
        self.overstock_window_days = overstock_window_days
        self.overstock_target_days = overstock_target_days


def create_app(store: FlaggedStore) -> FastAPI:
    """Construct a lightweight API for flagged decisions."""

    app = FastAPI(title="FoodFlow Integration Scheduler")

    @app.get("/health", response_class=JSONResponse)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/flagged", response_class=JSONResponse)
    def flagged() -> List[dict[str, object]]:
        try:
            return store.current()
        except Exception:
            LOGGER.exception("Failed to read flagged decisions")
            return []

    return app


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FoodFlow integration scheduler")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def _add_common_arguments(cmd: argparse.ArgumentParser) -> None:
        cmd.add_argument(
            "--config",
            default=str(DEFAULT_CONFIG_PATH),
            help="Path to integration config YAML (default: %(default)s)",
        )
        cmd.add_argument(
            "--policy",
            default=str(DEFAULT_POLICY_PATH),
            help="Path to decision policy YAML (default: %(default)s)",
        )
        cmd.add_argument(
            "--output",
            default=str(DEFAULT_FLAGGED_PATH),
            help="Path to flagged decisions JSON file (default: %(default)s)",
        )
        cmd.add_argument(
            "--log-level",
            default=os.getenv("INTEGRATION_SCHEDULE_LOG_LEVEL", "INFO"),
            help="Logging level (default: %(default)s)",
        )
        cmd.add_argument(
            "--days",
            type=int,
            default=7,
            help="Near-expiry threshold in days (default: %(default)s)",
        )
        cmd.add_argument(
            "--movement-window",
            type=int,
            default=7,
            help="Low-movement sales window in days (default: %(default)s)",
        )
        cmd.add_argument(
            "--min-units",
            type=float,
            default=12.0,
            help="Minimum units sold before flagging low movement (default: %(default)s)",
        )
        cmd.add_argument(
            "--overstock-window",
            type=int,
            default=7,
            help="Sales window in days for overstock detection (default: %(default)s)",
        )
        cmd.add_argument(
            "--target-days",
            type=float,
            default=21.0,
            help="Target days of supply threshold for overstock detection (default: %(default)s)",
        )

    once_parser = subparsers.add_parser("once", help="Run detection once and update flagged file")
    _add_common_arguments(once_parser)

    start_parser = subparsers.add_parser("start", help="Start scheduler and HTTP API")
    _add_common_arguments(start_parser)
    start_parser.add_argument(
        "--interval",
        type=int,
        default=_default_interval_minutes(),
        help="Detection interval in minutes (default: %(default)s)",
    )
    start_parser.add_argument(
        "--host",
        default=os.getenv("INTEGRATION_SCHEDULE_HOST", DEFAULT_HOST),
        help="HTTP server host (default: %(default)s)",
    )
    start_parser.add_argument(
        "--port",
        type=int,
        default=_default_port(),
        help="HTTP server port (default: %(default)s)",
    )

    return parser.parse_args(argv)


def _default_interval_minutes() -> int:
    env_value = os.getenv("INTEGRATION_SCHEDULE_INTERVAL_MINUTES")
    if not env_value:
        return DEFAULT_INTERVAL_MINUTES
    try:
        return max(1, int(env_value))
    except ValueError:
        LOGGER.warning("Invalid INTEGRATION_SCHEDULE_INTERVAL_MINUTES=%s; using default", env_value)
        return DEFAULT_INTERVAL_MINUTES


def _default_port() -> int:
    env_value = os.getenv("INTEGRATION_SCHEDULE_PORT")
    if not env_value:
        return DEFAULT_PORT
    try:
        return int(env_value)
    except ValueError:
        LOGGER.warning("Invalid INTEGRATION_SCHEDULE_PORT=%s; using default", env_value)
        return DEFAULT_PORT


def _configure_logging(level: str) -> None:
    resolved = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=resolved, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def _build_detection_args(args: argparse.Namespace) -> DetectionArgs:
    return DetectionArgs(
        near_expiry_days=max(0, int(args.days)),
        low_movement_window_days=max(1, int(args.movement_window)),
        low_movement_min_units=max(0.0, float(args.min_units)),
        overstock_window_days=max(1, int(args.overstock_window)),
        overstock_target_days=max(0.0, float(args.target_days)),
    )


def cmd_once(args: argparse.Namespace) -> int:
    _configure_logging(args.log_level)
    LOGGER.info("Running single detection cycle")
    store = FlaggedStore(Path(args.output))
    runner = DetectionRunner(
        store=store,
        config_path=Path(args.config),
        policy_path=Path(args.policy),
        detection_args=_build_detection_args(args),
    )
    runner.execute()
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    if uvicorn is None:
        LOGGER.error("uvicorn is required to run the HTTP server; install with `pip install uvicorn`")
        return 1

    _configure_logging(args.log_level)
    interval_seconds = max(1, int(args.interval) * 60)
    LOGGER.info(
        "Starting integration scheduler (interval=%s minutes, host=%s, port=%s)",
        args.interval,
        args.host,
        args.port,
    )
    store = FlaggedStore(Path(args.output))
    runner = DetectionRunner(
        store=store,
        config_path=Path(args.config),
        policy_path=Path(args.policy),
        detection_args=_build_detection_args(args),
    )
    runner.execute()

    stop_event = threading.Event()
    scheduler = Scheduler(runner, interval_seconds=interval_seconds, stop_event=stop_event)
    scheduler.start()

    app = create_app(store)

    def _handle_signal(signum, frame) -> None:  # pragma: no cover - signal handling is hard to test
        LOGGER.info("Received signal %s; shutting down scheduler", signum)
        stop_event.set()

    try:
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)
    except Exception:
        LOGGER.debug("Signal handlers not installed (likely on unsupported platform)")

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())
    except KeyboardInterrupt:  # pragma: no cover - interactive behaviour
        LOGGER.info("Scheduler interrupted by user")
    finally:
        stop_event.set()
        scheduler.join(timeout=5)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "once":
        return cmd_once(args)
    if args.command == "start":
        return cmd_start(args)
    LOGGER.error("Unknown command %s", args.command)
    return 1


if __name__ == "__main__":
    sys.exit(main())
