import json
import logging
import csv
from datetime import date, datetime, time, timezone

import pytest

from config import Instrument
from movers import PREFILTER_MIN_DISCOVERED, PREFILTER_SIDE_SIZE, _prefilter_candidate_pool, load_universe
from movers_prefilter import (
    _historical_params,
    _parse_historical_observations,
    build_benchmark,
    collect_discovery_changes,
    collect_historical_discovery_diagnostics,
    run_shadow_benchmark,
    write_benchmark,
    yahoo_provider_symbol,
)
from analyze_movers_prefilter import analyze_history


def _universe(count=50):
    return [Instrument(f"S{index:03d}", f"Symbol {index}", "movers") for index in range(count)]


def _true_results():
    return [
        {"symbol": f"S{index:03d}", "change_pct": 20.0 - index}
        for index in range(5)
    ] + [
        {"symbol": f"S{index:03d}", "change_pct": -20.0 - index}
        for index in range(5, 10)
    ]


def _discovery_results():
    values = []
    for index in range(50):
        if index < 5:
            change = 20.0 - index
        elif 45 <= index < 50:
            change = -20.0 - (index - 45)
        else:
            change = 10.0 - index / 10
        values.append({"symbol": f"S{index:03d}", "discovery_change_pct": change, "discovery_close": 100.0, "discovery_previous_close": 99.0, "observation_date": "2026-09-25"})
    return values


def test_perfect_30_recall_and_pool_sizes():
    benchmark = build_benchmark("2026-09-25", _universe(), _true_results(), _discovery_results(), {"http_requests": 50})
    assert benchmark["recall"] == {"30": 1.0, "40": 1.0, "50": 1.0}
    assert benchmark["gainers_recall"]["30"] == 1.0
    assert benchmark["losers_recall"]["30"] == 1.0
    assert benchmark["candidate_pools"]["30"]["size"] == 30


def test_missed_gainer_and_loser_are_reflected():
    discovery = [item for item in _discovery_results() if item["symbol"] not in {"S000", "S005"}]
    benchmark = build_benchmark("2026-09-25", _universe(), _true_results(), discovery, {"http_requests": 48})
    assert benchmark["gainers_recall"]["30"] < 1.0
    assert benchmark["losers_recall"]["30"] < 1.0


def test_duplicate_candidates_are_deduplicated():
    discovery = _discovery_results() + [_discovery_results()[0], _discovery_results()[-1]]
    benchmark = build_benchmark("2026-09-25", _universe(), _true_results(), discovery, {"http_requests": 50})
    candidates = benchmark["candidate_pools"]["30"]["symbols"]
    assert len(candidates) == len(set(candidates))


def test_rank_delta_and_percentage_error_are_recorded():
    benchmark = build_benchmark("2026-09-25", _universe(), _true_results(), _discovery_results(), {"http_requests": 50})
    row = next(item for item in benchmark["rank_error"] if item["symbol"] == "S000")
    assert {"discovery_rank", "true_rank", "rank_delta", "percentage_point_error"}.issubset(row)


def test_discovery_failure_does_not_block_production(monkeypatch, tmp_path):
    monkeypatch.setattr("movers_prefilter.collect_discovery_changes", lambda *args, **kwargs: ([], {"http_requests": 0}))
    result = run_shadow_benchmark(_universe(), _true_results(), "2026-09-25", logging.getLogger("test-prefilter"), tmp_path)
    assert result is None
    assert not (tmp_path / "movers_prefilter_benchmark.json").exists()


def test_benchmark_history_writing_and_analyzer(tmp_path):
    benchmark = build_benchmark("2026-09-25", _universe(), _true_results(), _discovery_results(), {"http_requests": 50})
    current, history = write_benchmark(benchmark, tmp_path)
    assert current.exists() and history.exists()
    analysis = analyze_history(tmp_path / "history")
    assert analysis["files_read"] == 1
    assert analysis["pools"]["30"]["perfect_days"] == 1


def test_production_flag_is_explicitly_unchanged():
    benchmark = build_benchmark("2026-09-25", _universe(), _true_results(), _discovery_results(), {"http_requests": 50})
    assert benchmark["production_unchanged"] is True


def test_production_universe_matches_versioned_csv_order():
    from movers import UNIVERSE_PATH

    with UNIVERSE_PATH.open("r", encoding="utf-8", newline="") as handle:
        source_rows = list(csv.DictReader(handle))
    loaded = load_universe()
    assert len(source_rows) == 125
    assert len(loaded) == 120
    assert [(item.symbol, item.display_name) for item in loaded] == [
        (row["symbol"].strip().upper(), row["company_name"].strip()) for row in source_rows[:120]
    ]


def _chart_payload(dates, closes, timezone_name="America/New_York"):
    timestamps = [
        int(datetime.combine(date.fromisoformat(value), time(4), timezone.utc).timestamp())
        for value in dates
    ]
    return {"chart": {"result": [{
        "meta": {"exchangeTimezoneName": timezone_name},
        "timestamp": timestamps,
        "indicators": {"quote": [{"close": closes}]},
    }]}}


class _Response:
    status_code = 200
    def __init__(self, payload):
        self.payload = payload
    def raise_for_status(self):
        return None
    def json(self):
        return self.payload


def test_historical_stock_query_uses_bounded_target_and_lookback():
    params = _historical_params("2026-08-20")
    assert params["period1"] == int(datetime.combine(date(2026, 7, 30), time.min, timezone.utc).timestamp())
    assert params["period2"] == int(datetime.combine(date(2026, 8, 21), time.min, timezone.utc).timestamp())
    assert params["interval"] == "1d"


def test_weekend_gap_uses_previous_valid_session_not_previous_calendar_day():
    rows, tz = _parse_historical_observations(
        _chart_payload(["2026-09-25", "2026-09-28"], [90, 99]), "2026-09-28"
    )
    assert tz == "America/New_York"
    assert rows[0] == {"date": "2026-09-28", "close": 99.0}
    assert rows[1] == {"date": "2026-09-25", "close": 90.0}


def test_holiday_gap_uses_prior_returned_session():
    rows, _ = _parse_historical_observations(
        _chart_payload(["2026-07-02", "2026-07-06"], [80, 88]), "2026-07-06"
    )
    assert [row["date"] for row in rows] == ["2026-07-06", "2026-07-02"]


def test_exchange_timezone_normalization_controls_market_date():
    payload = _chart_payload(["2026-08-19", "2026-08-20"], [10, 11])
    payload["chart"]["result"][0]["timestamp"][1] = int(
        datetime(2026, 8, 20, 2, tzinfo=timezone.utc).timestamp()
    )
    rows, _ = _parse_historical_observations(payload, "2026-08-19")
    assert all(row["date"] == "2026-08-19" for row in rows)


def test_no_future_bar_is_used_even_if_returned():
    rows, _ = _parse_historical_observations(
        _chart_payload(["2026-08-20", "2026-08-21", "2026-08-24"], [10, 100, 1000]),
        "2026-08-20",
    )
    assert rows == [{"date": "2026-08-20", "close": 10.0}]


def test_brk_dot_b_maps_only_at_yahoo_boundary():
    assert yahoo_provider_symbol("BRK.B") == "BRK-B"
    assert yahoo_provider_symbol("BRK-B") == "BRK-B"


def test_historical_discovery_calculates_valid_move_and_passes_exact_dates():
    calls = []
    payload = _chart_payload(["2026-08-19", "2026-08-20"], [100, 105])
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return _Response(payload)
    rows, metrics = collect_discovery_changes(
        [Instrument("AAA", "A", "movers")], "2026-08-20", logging.getLogger("hist-prefilter"),
        request_get=get, historical=True,
    )
    assert rows[0]["observation_date"] == "2026-08-20"
    assert rows[0]["discovery_change_pct"] == pytest.approx(5.0)
    assert metrics["symbols_discovered"] == 1
    assert calls[0][1]["params"]["period2"] == _historical_params("2026-08-20")["period2"]


def test_malformed_yahoo_row_is_rejected_as_parse_failure():
    with pytest.raises(ValueError, match="YAHOO_RESPONSE_FORMAT_CHANGED"):
        _parse_historical_observations({"chart": {"result": []}}, "2026-08-20")


def test_full_valid_universe_ranks_top20_and_bottom20_without_fallback():
    universe = load_universe()
    discovery = [
        {"symbol": instrument.symbol, "discovery_change_pct": 100 - index if index < 60 else -1 - index,
         "observation_date": "2026-08-20"}
        for index, instrument in enumerate(universe)
    ]
    selected, reason = _prefilter_candidate_pool(
        universe, discovery,
        {"symbols_requested": len(universe), "symbols_discovered": len(universe), "critical_failure": False},
        "2026-08-20",
    )
    assert reason is None
    assert len(selected) == PREFILTER_SIDE_SIZE * 2
    assert [item.symbol for item in selected[:20]] == [row["symbol"] for row in discovery[:20]]
    assert len({item.symbol for item in selected}) == 40


def test_coverage_gate_threshold_is_not_lowered():
    assert PREFILTER_MIN_DISCOVERED == 115
    universe = load_universe()
    discovery = [{"symbol": item.symbol, "discovery_change_pct": 1 if i < 60 else -1, "observation_date": "2026-08-20"} for i, item in enumerate(universe[:114])]
    selected, reason = _prefilter_candidate_pool(
        universe, discovery, {"symbols_requested": 120, "symbols_discovered": 114, "critical_failure": False}, "2026-08-20"
    )
    assert selected is None
    assert reason == "DISCOVERY_COVERAGE_LOW"


def test_historical_healthy_prefilter_has_no_fallback():
    universe = load_universe()
    discovery = [{"symbol": item.symbol, "discovery_change_pct": 100 - i if i < 60 else -1 - i, "observation_date": "2026-08-20"} for i, item in enumerate(universe)]
    selected, reason = _prefilter_candidate_pool(
        universe, discovery, {"symbols_requested": 120, "symbols_discovered": 120, "critical_failure": False}, "2026-08-20"
    )
    assert reason is None and selected and len(selected) <= 40


def test_live_prefilter_retains_range_query_while_historical_uses_bounds():
    payload = _chart_payload(["2026-08-19", "2026-08-20"], [100, 101])
    seen = []
    def get(url, **kwargs):
        seen.append(kwargs["params"])
        return _Response(payload)
    instrument = [Instrument("AAA", "A", "movers")]
    collect_discovery_changes(instrument, "2026-08-20", logging.getLogger("live-compat"), request_get=get)
    collect_discovery_changes(instrument, "2026-08-20", logging.getLogger("hist-compat"), request_get=get, historical=True)
    assert seen[0] == {"range": "10d", "interval": "1d", "events": "history"}
    assert "period1" in seen[1] and "period2" in seen[1]


def test_yahoo_diagnostics_do_not_call_twelve_data():
    calls = []
    payload = _chart_payload(["2026-08-19", "2026-08-20", "2026-08-21"], [99, 100, 101])
    rows, diagnostics, request_count = collect_historical_discovery_diagnostics(
        [Instrument("AAA", "A", "movers")], ["2026-08-20", "2026-08-21"],
        logging.getLogger("yahoo-only"), request_get=lambda url, **kwargs: (calls.append(url) or _Response(payload)),
    )
    assert request_count == 1 and len(calls) == 1
    assert rows["2026-08-20"][0]["source"] == "Yahoo"
    assert rows["2026-08-21"][0]["source"] == "Yahoo"
    assert diagnostics[0]["status"] == "PASS"
