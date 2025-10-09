"""Tests for the simulator service."""
from __future__ import annotations

from collections import Counter
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
from typing import Dict, Iterable, List, Optional
from unittest import TestCase
from unittest.mock import patch

from packages.db import EventStore
from services.analysis.shrink_triggers import (
    LowMovementConfig,
    OverstockConfig,
    ShrinkTriggerConfig,
    ShrinkTriggerDetector,
)
from services.simulator.config import SimulatorConfig
from services.simulator.events import EventWriter
from services.simulator.scheduler import SimulatorScheduler
from services.simulator.service import SimulatorService
from services.simulator.state import StateTracker


class FakeOdooClient:
    """Minimal in-memory stub for the Odoo client."""

    def __init__(
        self,
        quants: Dict[int, Dict[str, object]],
        products: Dict[int, Dict[str, object]],
        lots: Dict[int, Dict[str, object]],
    ) -> None:
        self._quants = quants
        self._products = products
        self._lots = lots
        self.write_calls: List[Dict[str, object]] = []
        self.create_calls: List[Dict[str, object]] = []

    def authenticate(self) -> int:  # pragma: no cover - compatibility only
        return 1

    def search_read(
        self,
        model: str,
        domain: Iterable[Iterable[object]] | None = None,
        fields: Optional[Iterable[str]] = None,
        **_: object,
    ) -> List[Dict[str, object]]:
        if model == "stock.quant":
            return [self._select_fields(record, fields) for record in self._quants.values()]
        if model == "product.product":
            ids = _extract_ids_from_domain(domain)
            records = [self._products[i] for i in ids if i in self._products]
            return [self._select_fields(record, fields) for record in records]
        if model == "stock.lot":
            records = list(self._lots.values())
            records = [r for r in records if _matches_domain(r, domain)]
            return [self._select_fields(record, fields) for record in records]
        raise AssertionError(f"Unexpected model {model}")

    def write(self, model: str, record_id: int, values: Dict[str, object]) -> bool:
        payload = {"model": model, "id": record_id, "values": dict(values)}
        self.write_calls.append(payload)
        if model == "stock.quant":
            if "lot_id" in values:
                lot_id = int(values["lot_id"])
                lot_name = self._lots[lot_id]["name"]
                self._quants[record_id]["lot_id"] = [lot_id, lot_name]
            if "quantity" in values:
                self._quants[record_id]["quantity"] = values["quantity"]
            return True
        if model == "stock.lot":
            self._lots[record_id].update(values)
            return True
        raise AssertionError(f"Unexpected model {model}")

    def create(self, model: str, values: Dict[str, object]) -> int:
        payload = {"model": model, "values": dict(values)}
        self.create_calls.append(payload)
        if model == "stock.lot":
            new_id = max(self._lots, default=200) + 1
            record = dict(values)
            record["id"] = new_id
            self._lots[new_id] = record
            return new_id
        raise AssertionError(f"Unexpected create model {model}")

    def _select_fields(
        self, record: Dict[str, object], fields: Optional[Iterable[str]]
    ) -> Dict[str, object]:
        if not fields:
            return dict(record)
        output = {"id": record["id"]}
        for field in fields:
            if field == "id":
                continue
            output[field] = record.get(field)
        return output


def _extract_ids_from_domain(domain: Iterable[Iterable[object]] | None) -> List[int]:
    if not domain:
        return list(range(0))
    ids: List[int] = []
    for term in domain:
        if len(term) >= 3 and term[1] == "in":
            value = term[2]
            if isinstance(value, list):
                ids.extend(int(v) for v in value)
    return ids


def _matches_domain(record: Dict[str, object], domain: Iterable[Iterable[object]] | None) -> bool:
    if not domain:
        return True
    for term in domain:
        if len(term) < 3:
            continue
        field, op, value = term[0], term[1], term[2]
        if op == "in":
            if record.get(field) not in value:
                return False
        elif op == "=":
            if record.get(field) != value:
                return False
    return True


class SimulatorServiceTests(TestCase):
    def setUp(self) -> None:
        self.tmpdir = TemporaryDirectory()
        self.now = datetime(2024, 1, 10, 12, 0, tzinfo=timezone.utc)
        self.client = FakeOdooClient(
            quants={
                1: {
                    "id": 1,
                    "product_id": [101, "Gala Apples"],
                    "quantity": 100.0,
                    "lot_id": [201, "LOT-201"],
                },
                2: {
                    "id": 2,
                    "product_id": [102, "Whole Milk"],
                    "quantity": 50.0,
                    "lot_id": [202, "LOT-202"],
                },
            },
            products={
                101: {"id": 101, "name": "Gala Apples", "categ_id": [301, "Produce"]},
                102: {"id": 102, "name": "Whole Milk", "categ_id": [302, "Dairy"]},
            },
            lots={
                201: {
                    "id": 201,
                    "name": "LOT-201",
                    "life_date": (self.now.date() + timedelta(days=1)).isoformat(),
                },
                202: {
                    "id": 202,
                    "name": "LOT-202",
                    "life_date": (self.now.date() - timedelta(days=1)).isoformat(),
                },
            },
        )
        self.config = SimulatorConfig.from_mapping(
            {
                "sell_down": {"default": 0.1, "category_rates": {"Produce": 0.2}},
                "returns": {"default": 1.0},
                "receiving": {"default": 4.0, "category_rates": {"Produce": 8.0}},
                "shrink": {"default": 0.1, "category_rates": {"Produce": 0.2}},
                "daily_expiry": {"default": 5, "perishability": {"Dairy": 2}},
            }
        )
        base_path = Path(self.tmpdir.name)
        self.events_path = base_path / "events.jsonl"
        self.state_path = base_path / "state.json"
        self.db_path = base_path / "events.db"
        self.writer = EventWriter(self.events_path, store=EventStore(self.db_path))
        self.state = StateTracker(self.state_path, timedelta(hours=24))
        shrink_config = ShrinkTriggerConfig(
            low_movement=LowMovementConfig(units_threshold=40.0, window_days=7),
            overstock=OverstockConfig(
                default_days_of_supply=6.0,
                category_thresholds={"Produce": 8.0},
                velocity_window_days=7,
                min_daily_velocity=0.1,
            ),
        )
        self.shrink_detector = ShrinkTriggerDetector(self.writer.store, shrink_config)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _service(self) -> SimulatorService:
        return SimulatorService(
            self.client,
            self.config,
            self.writer,
            self.state,
            now_fn=lambda: self.now,
            rng=Random(0),
            shrink_detector=self.shrink_detector,
        )

    def test_once_runs_all_jobs(self) -> None:
        service = self._service()
        events = service.run_once(force=True)
        self.assertEqual(len(events), 13)

        # Sell down reduces stock, returns add back a little, expiry & shrink trim, receiving replenishes.
        self.assertAlmostEqual(self.client._quants[1]["quantity"], 22.1, places=2)
        self.assertAlmostEqual(self.client._quants[2]["quantity"], 4.0, places=2)

        # The dairy item was expired and should receive a fresh lot.
        dairy_lot = self.client._quants[2]["lot_id"][0]
        self.assertIn(dairy_lot, self.client._lots)
        self.assertNotEqual(self.client._lots[dairy_lot]["name"], "LOT-202")
        self.assertTrue(any(call["model"] == "stock.lot" for call in self.client.create_calls))

        counts = Counter(event.type for event in events)
        self.assertGreaterEqual(counts["returns"], 1)
        self.assertGreaterEqual(counts["shrink"], 1)
        self.assertEqual(counts["flag_low_movement"], 2)
        self.assertEqual(counts["flag_overstock"], 2)

    def test_returns_never_exceed_total_sold(self) -> None:
        service = self._service()
        events = service.run_once(force=True)

        totals: Dict[str, Dict[str, float]] = {}
        for event in events:
            if event.type not in {"sell_down", "returns"}:
                continue
            product_totals = totals.setdefault(event.product, {"sold": 0.0, "returned": 0.0})
            if event.type == "sell_down":
                product_totals["sold"] += max(-event.qty, 0.0)
            elif event.type == "returns":
                product_totals["returned"] += max(event.qty, 0.0)

        for product, summary in totals.items():
            self.assertLessEqual(summary["returned"], summary["sold"] + 1e-6, product)

        # File log should also reflect returns events for later runs.
        with self.events_path.open("r", encoding="utf-8") as handle:
            history = [json.loads(line) for line in handle if line.strip()]
        self.assertTrue(any(entry["type"] == "returns" for entry in history))

    def test_shrink_never_makes_negative_quantities(self) -> None:
        service = self._service()
        service.run_once(force=True)

        for quant in self.client._quants.values():
            self.assertGreaterEqual(quant["quantity"], 0.0)

        with self.events_path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
        self.assertEqual(len(lines), 13)
        self.assertTrue(all("\"source\":\"simulator\"" in line for line in lines))

    def test_daily_job_respects_interval(self) -> None:
        service = self._service()
        service.run_once(force=True)
        writes_after_first = list(self.client.write_calls)

        events = service.run_once(force=False)
        # Daily expiry should be skipped, leaving only sell down + receiving job events (plus analysis flags).
        self.assertEqual(len(events), 8)
        counts = Counter(event.type for event in events)
        self.assertEqual(counts["flag_low_movement"], 2)
        self.assertEqual(counts["flag_overstock"], 2)
        self.assertEqual(
            [call["model"] for call in self.client.write_calls[len(writes_after_first) :]],
            ["stock.quant", "stock.quant", "stock.quant", "stock.quant"],
        )

    def test_scheduler_ticks_respect_interval(self) -> None:
        service = self._service()
        scheduler = SimulatorScheduler(service, interval_seconds=1)
        with patch("services.simulator.scheduler.time.sleep", return_value=None):
            scheduler.run(max_ticks=2)

        # First tick runs all jobs; second tick should skip daily expiry only.
        with self.events_path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
        self.assertEqual(len(lines), 21)

        events = service.run_once(force=False)
        self.assertEqual(len(events), 8)
        counts = Counter(event.type for event in events)
        self.assertEqual(counts["flag_low_movement"], 2)
        self.assertEqual(counts["flag_overstock"], 2)
