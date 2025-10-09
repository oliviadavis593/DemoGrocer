from __future__ import annotations

from pathlib import Path

from services.integration import load_config


def test_load_config_defaults_when_missing(tmp_path: Path) -> None:
    config_path = tmp_path / "missing.yaml"
    config = load_config(config_path)

    assert config.log_level == "INFO"
    assert config.inventory.summary_limit == 5
    assert config.inventory.lot_expiry_field is None


def test_load_config_parses_values(tmp_path: Path) -> None:
    config_text = """
    log_level: debug
    inventory:
      summary_limit: 3
      lot_expiry_field: life_date
    """
    config_path = tmp_path / "integration.yaml"
    config_path.write_text(config_text, encoding="utf-8")

    config = load_config(config_path)

    assert config.log_level == "DEBUG"
    assert config.inventory.summary_limit == 3
    assert config.inventory.lot_expiry_field == "life_date"
