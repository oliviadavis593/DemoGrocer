from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from fastapi.testclient import TestClient

from services.integration.config import IntegrationConfig
from services.integration.schedule import (
    DetectionArgs,
    DetectionRunner,
    FlaggedStore,
    create_app,
)


def test_flagged_store_persists_and_returns_copy(tmp_path) -> None:
    path = Path(tmp_path) / "flagged.json"
    store = FlaggedStore(path)

    payload = [{"reason": "near_expiry", "lot": "LOT-1"}]
    store.update(payload)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == payload

    snapshot = store.current()
    snapshot[0]["reason"] = "mutated"
    # Ensure cached state is not affected by caller mutations
    assert store.current()[0]["reason"] == "near_expiry"


def test_flagged_store_ignores_invalid_existing_file(tmp_path) -> None:
    path = Path(tmp_path) / "flagged.json"
    path.write_text("not valid json", encoding="utf-8")

    store = FlaggedStore(path)

    assert store.current() == []


def test_detection_runner_updates_store_with_decisions(tmp_path) -> None:
    path = Path(tmp_path) / "flagged.json"
    store = FlaggedStore(path)
    args = DetectionArgs(
        near_expiry_days=5,
        low_movement_window_days=7,
        low_movement_min_units=3.0,
        overstock_window_days=9,
        overstock_target_days=30.0,
    )

    decision_payload = {"outcome": "MARKDOWN"}
    mapper = SimpleNamespace(
        map_flags=lambda flags: [SimpleNamespace(to_dict=lambda: decision_payload)]
    )

    with patch("services.integration.schedule.load_config", return_value=IntegrationConfig()):
        with patch("services.integration.schedule.detect_flags", return_value=[{"reason": "near_expiry"}]):
            with patch("services.integration.schedule.DecisionMapper.from_path", return_value=mapper):
                runner = DetectionRunner(
                    store=store,
                    config_path=Path("config.yaml"),
                    policy_path=Path("policy.yaml"),
                    detection_args=args,
                )
                runner.execute()

    assert store.current() == [decision_payload]


def test_create_app_returns_flagged_payload(tmp_path) -> None:
    path = Path(tmp_path) / "flagged.json"
    store = FlaggedStore(path)
    payload = [{"reason": "low_movement"}]
    store.update(payload)

    app = create_app(store)
    client = TestClient(app)
    response = client.get("/flagged")

    assert response.status_code == 200
    assert response.json() == payload


def test_create_app_never_raises_from_store_errors() -> None:
    class FailingStore:
        def current(self) -> list[dict[str, object]]:
            raise RuntimeError("boom")

    app = create_app(cast(FlaggedStore, FailingStore()))
    client = TestClient(app)
    response = client.get("/flagged")

    assert response.status_code == 200
    assert response.json() == []
