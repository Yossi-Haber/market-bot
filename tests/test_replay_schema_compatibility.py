import json
import shutil
from pathlib import Path

import pytest
import evaluate_market_drivers_product as product
from drivers.runtime import _scope_analysis


def test_additive_historical_driver_fields_are_normalized_and_preserved():
    driver = {
        "driver_id": "RISK_OFF",
        "driver_type": "MARKET_DRIVER",
        "scope": "US_MARKET",
        "direction": "down",
        "strength": 0.8,
        "related_symbols": [],
        "hypothesis_status": "EVIDENCE_ELIGIBLE",
        "warnings": ["historical-warning"],
    }
    result = _scope_analysis({"main_market_driver": driver, "sector_drivers": [], "stock_catalysts": []}, [], [])
    assert result["main_market_driver"]["hypothesis_status"] == "EVIDENCE_ELIGIBLE"
    assert result["main_market_driver"]["warnings"] == ["historical-warning"]


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "replay_schema_v1"
HISTORICAL_DATES = ("2026-09-15", "2026-09-23")


def test_sanitized_schema_fixtures_replay_without_type_error(tmp_path, monkeypatch):
    output_dir = tmp_path / "output"
    for market_date in HISTORICAL_DATES:
        destination = output_dir / "history" / market_date
        destination.mkdir(parents=True)
        for name in ("market_snapshot.json", "movers_snapshot.json"):
            target = destination / name
            shutil.copyfile(FIXTURE_ROOT / name, target)
            payload = json.loads(target.read_text(encoding="utf-8"))
            payload["market_date"] = market_date
            target.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(product, "LABELS_PATH", tmp_path / "no-private-labels.csv")
    monkeypatch.setattr(product.replay, "CACHE_DIR", tmp_path / "cache")
    for market_date in HISTORICAL_DATES:
        result = product.evaluate_product(market_date, market_date, market_dates=[market_date], output_dir=output_dir)
        assert result["days"]
        assert result["days"][0]["market_date"] == market_date


@pytest.mark.local_historical_replay
def test_local_historical_problem_dates_replay_without_type_error():
    required = [
        Path("output/history") / market_date / f"{prefix}.json"
        for market_date in HISTORICAL_DATES
        for prefix in ("market_snapshot", "movers_snapshot")
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        pytest.skip("requires excluded local historical replay inputs: " + ", ".join(missing))
    for market_date in HISTORICAL_DATES:
        result = product.evaluate_product(market_date, market_date, market_dates=[market_date])
        assert result["days"]
        assert result["days"][0]["market_date"] == market_date
