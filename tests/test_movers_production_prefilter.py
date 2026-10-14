import json
import logging

import movers
from config import Instrument
from run_daily import parse_args


MARKET_DATE = "2026-09-25"
GAINERS = ["MCHP", "CVS", "PYPL", "HUM", "BKNG"]
LOSERS = ["PANW", "META", "EQIX", "OXY", "CRM"]
DISCOVERY_FILLER_LOSERS = {f"S{index:03d}" for index in range(15)}


def _universe():
    symbols = GAINERS + LOSERS + [f"S{index:03d}" for index in range(110)]
    return [Instrument(symbol, f"Company {symbol}", "movers", "Twelve Data", symbol) for symbol in symbols]


def _discovery(universe, *, discovered=None, date=MARKET_DATE):
    discovered = len(universe) if discovered is None else discovered
    rows = []
    for index, instrument in enumerate(universe[:discovered]):
        if instrument.symbol in GAINERS:
            change = 100 - GAINERS.index(instrument.symbol)
        elif instrument.symbol in LOSERS:
            change = -100 + LOSERS.index(instrument.symbol)
        elif instrument.symbol in DISCOVERY_FILLER_LOSERS:
            change = -20 - index / 100
        else:
            change = 10 - index / 100
        rows.append({"symbol": instrument.symbol, "discovery_change_pct": change, "observation_date": date})
    return rows, {"symbols_requested": len(universe), "symbols_discovered": discovered, "http_requests": len(universe), "runtime_seconds": 1.0, "critical_failure": False}


def _result(instrument, change):
    return {
        "symbol": instrument.symbol,
        "close": 100.0 + change,
        "previous_close": 100.0,
        "change": change,
        "change_pct": change,
        "open": 100.0,
        "high": max(101.0, 100.0 + change),
        "low": min(99.0, 100.0 + change),
        "volume": 1000,
        "latest_bar_datetime": f"{MARKET_DATE}T16:00:00Z",
        "source": "Twelve Data",
        "verified": True,
    }


class FakeClient:
    def __init__(self, *, fail_candidate=False):
        self.request_count = 0
        self.successful_api_requests = 0
        self.retry_requests = 0
        self.batches = []
        self.fail_candidate = fail_candidate
        self.rate_limiter = type("Limiter", (), {"daily_requests": 0, "rate_limit_waits": 0, "credits_left": None, "_requests_in_minute": 0})()

    def fetch_stock_batch(self, batch, logger, market_date=None):
        self.batches.append([item.symbol for item in batch])
        self.request_count += 1
        self.successful_api_requests += len(batch)
        self.rate_limiter.daily_requests += len(batch)
        if self.fail_candidate and len(self.batches) <= 5:
            return {}
        return {item.symbol: _result(item, 10 - GAINERS.index(item.symbol) if item.symbol in GAINERS else -10 + LOSERS.index(item.symbol) if item.symbol in LOSERS else -1.0 if item.symbol in DISCOVERY_FILLER_LOSERS else 1.0) for item in batch}


def _run(monkeypatch, tmp_path, client, *, enabled=True):
    universe = _universe()
    monkeypatch.setattr(movers, "load_universe", lambda limit=movers.MOVERS_UNIVERSE_LIMIT: universe)
    monkeypatch.setattr(movers, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(movers, "run_shadow_benchmark", lambda *args, **kwargs: None)
    monkeypatch.setattr(movers, "collect_discovery_changes", lambda universe, market_date, logger: _discovery(universe))
    path = movers.run_movers_snapshot(client, logging.getLogger("prefilter-test"), MARKET_DATE, force_refresh=True, prefilter_enabled=enabled)
    return json.loads(path.read_text(encoding="utf-8"))


def test_valid_discovery_uses_40_symbols_and_twelve_data_ranking(monkeypatch, tmp_path):
    snapshot = _run(monkeypatch, tmp_path, FakeClient())
    full_snapshot = _run(monkeypatch, tmp_path / "full", FakeClient(), enabled=False)
    assert snapshot["prefilter"]["candidate_pool_size"] == 40
    assert snapshot["symbols_requested"] == 40
    assert len(snapshot["batches"]) == 5
    assert [item["symbol"] for item in snapshot["top_gainers"]] == GAINERS
    assert [item["symbol"] for item in snapshot["top_losers"]] == LOSERS
    assert [item["symbol"] for item in snapshot["top_gainers"]] == [item["symbol"] for item in full_snapshot["top_gainers"]]
    assert [item["symbol"] for item in snapshot["top_losers"]] == [item["symbol"] for item in full_snapshot["top_losers"]]


def test_low_discovery_coverage_falls_back_to_full_universe(monkeypatch, tmp_path):
    universe = _universe()
    monkeypatch.setattr(movers, "load_universe", lambda limit=movers.MOVERS_UNIVERSE_LIMIT: universe)
    monkeypatch.setattr(movers, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(movers, "run_shadow_benchmark", lambda *args, **kwargs: None)
    monkeypatch.setattr(movers, "collect_discovery_changes", lambda *args, **kwargs: _discovery(universe, discovered=114))
    client = FakeClient()
    path = movers.run_movers_snapshot(client, logging.getLogger("prefilter-test"), MARKET_DATE, force_refresh=True, prefilter_enabled=True)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert len(client.batches) == 15
    assert snapshot["fallback_used"] is True
    assert snapshot["prefilter"]["fallback_reason"] == "DISCOVERY_COVERAGE_LOW"
    assert snapshot["symbols_requested"] == 120


def test_118_discovered_symbols_is_allowed(monkeypatch, tmp_path):
    universe = _universe()
    rows, metrics = _discovery(universe, discovered=118)
    pool, reason = movers._prefilter_candidate_pool(universe, rows, metrics, MARKET_DATE)
    assert reason is None
    assert len(pool) == 40


def test_discovery_date_mismatch_falls_back():
    universe = _universe()
    rows, metrics = _discovery(universe, date="2026-09-24")
    pool, reason = movers._prefilter_candidate_pool(universe, rows, metrics, MARKET_DATE)
    assert pool is None
    assert reason == "DISCOVERY_POSITIVE_SIDE_LOW"


def test_insufficient_discovery_side_falls_back():
    universe = _universe()
    rows, metrics = _discovery(universe)
    rows = [row for row in rows if row["discovery_change_pct"] >= 0]
    pool, reason = movers._prefilter_candidate_pool(universe, rows, metrics, MARKET_DATE)
    assert pool is None
    assert reason == "DISCOVERY_NEGATIVE_SIDE_LOW"


def test_duplicate_candidate_symbols_fail_unique_pool_size_gate():
    universe = _universe()
    rows, metrics = _discovery(universe)
    rows.extend([dict(rows[0]), dict(rows[1])])
    pool, reason = movers._prefilter_candidate_pool(universe, rows, metrics, MARKET_DATE)
    assert pool is None
    assert reason == "CANDIDATE_POOL_SIZE_INVALID"


def test_candidate_completeness_failure_falls_back_once(monkeypatch, tmp_path):
    snapshot = _run(monkeypatch, tmp_path, FakeClient(fail_candidate=True))
    assert snapshot["fallback_used"] is True
    assert snapshot["prefilter"]["fallback_reason"] == "CANDIDATE_TWELVE_DATA_INCOMPLETE"
    assert snapshot["symbols_requested"] == 120


def test_disable_prefilter_forces_full_universe(monkeypatch, tmp_path):
    snapshot = _run(monkeypatch, tmp_path, FakeClient(), enabled=False)
    assert snapshot["prefilter_enabled"] is False
    assert snapshot["symbols_requested"] == 120


def test_cli_override_is_explicit():
    args = parse_args(["--refresh-movers", "--disable-movers-prefilter"])
    assert args.refresh_movers is True
    assert args.disable_movers_prefilter is True
