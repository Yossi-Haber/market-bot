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


def test_historical_problem_dates_replay_without_type_error():
    for market_date in ("2026-09-15", "2026-09-23"):
        result = product.evaluate_product(market_date, market_date, market_dates=[market_date])
        assert result["days"]
        assert result["days"][0]["market_date"] == market_date
