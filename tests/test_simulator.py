"""Tests for the simulator service."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, Iterable, List, Optional
from unittest import TestCase
from unittest.mock import patch

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
        if model == "stock.production.lot":
            ids = _extract_ids_from_domain(domain)
            records = [self._lots[i] for i in ids if i in self._lots]
            return [self._select_fields(record, fields) for record in records]
        raise AssertionError(f"Unexpected model {model}")

    def write(self, model: str, record_id: int, values: Dict[str, object]) -> bool:
        assert model == "stock.quant"
        self.write_calls.append({"model": model, "id": record_id, "values": dict(values)})
        self._quants[record_id].update(values)
        return True

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
                "receiving": {"default": 4.0, "category_rates": {"Produce": 8.0}},
                "daily_expiry": {"default": 5, "perishability": {"Dairy": 2}},
            }
        )
        base_path = Path(self.tmpdir.name)
        self.events_path = base_path / "events.jsonl"
        self.state_path = base_path / "state.json"
        self.writer = EventWriter(self.events_path)
        self.state = StateTracker(self.state_path, timedelta(hours=24))

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _service(self) -> SimulatorService:
        return SimulatorService(
            self.client,
            self.config,
            self.writer,
            self.state,
            now_fn=lambda: self.now,
        )

    def test_once_runs_all_jobs(self) -> None:
        service = self._service()
        events = service.run_once(force=True)
        self.assertEqual(len(events), 6)

        # After sell down and receiving the produce item should have decreased, then increased,
        # while the dairy item expires to zero.
        self.assertAlmostEqual(self.client._quants[1]["quantity"], 17.6, places=2)
        self.assertAlmostEqual(self.client._quants[2]["quantity"], 0.0, places=2)

        with self.events_path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
        self.assertEqual(len(lines), 6)
        self.assertTrue(all("\"source\":\"simulator\"" in line for line in lines))

    def test_state_prevents_double_run(self) -> None:
        service = self._service()
        service.run_once(force=True)
        writes_after_first = len(self.client.write_calls)

        events = service.run_once(force=False)
        self.assertEqual(events, [])
        self.assertEqual(len(self.client.write_calls), writes_after_first)

    def test_scheduler_ticks_respect_interval(self) -> None:
        service = self._service()
        scheduler = SimulatorScheduler(service, interval_seconds=1)
        with patch("services.simulator.scheduler.time.sleep", return_value=None):
            scheduler.run(max_ticks=2)

        # First tick runs and records events, second tick should be skipped.
        with self.events_path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
        self.assertEqual(len(lines), 6)

        events = service.run_once(force=False)
        self.assertEqual(events, [])

