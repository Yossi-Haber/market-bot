import json
import logging

import movers
from config import Instrument


def _universe():
    return [
        Instrument("AAA", "Alpha Corp", "movers", "Twelve Data", "AAA"),
        Instrument("BBB", "Beta Corp", "movers", "Twelve Data", "BBB"),
    ]


def _item(symbol, change_pct):
    return {
        "symbol": symbol,
        "company_name": f"{symbol} Corp",
        "close": 101.0,
        "previous_close": 100.0,
        "change": 1.0,
        "change_pct": change_pct,
        "latest_bar_datetime": "2026-09-25T16:00:00Z",
        "source": "Twelve Data",
        "verified": True,
    }


def _write_cache(tmp_path, monkeypatch, *, market_date="2026-09-25", universe=None, **overrides):
    universe = universe or _universe()
    monkeypatch.setattr(movers, "load_universe", lambda limit=movers.MOVERS_UNIVERSE_LIMIT: universe)
    payload = {
        "market_date": market_date,
        "cache_status": "MISS",
        "collection_status": "SUCCESS",
        "schema_version": movers.MOVERS_CACHE_SCHEMA_VERSION,
        "algorithm_version": movers.MOVERS_ALGORITHM_VERSION,
        "universe_hash": movers.universe_fingerprint(universe),
        "symbols_requested": len(universe),
        "symbols_valid": 2,
        "prefilter_enabled": False,
        "candidate_pool_size": 0,
        "discovery_source": None,
        "fallback_used": True,
        "top_gainers": [_item("AAA", 2.0)],
        "top_losers": [_item("BBB", -2.0)],
    }
    payload.update(overrides)
    path = tmp_path / "movers_snapshot.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, payload


def test_same_market_date_is_cache_hit(monkeypatch, tmp_path):
    path, payload = _write_cache(tmp_path, monkeypatch)
    assert movers.load_valid_movers_cache("2026-09-25", path=path) == payload


def test_next_market_date_is_cache_miss(monkeypatch, tmp_path):
    path, _ = _write_cache(tmp_path, monkeypatch)
    assert movers.load_valid_movers_cache("2026-09-26", path=path) is None


def test_universe_change_invalidates_cache(monkeypatch, tmp_path):
    path, _ = _write_cache(tmp_path, monkeypatch)
    changed = _universe() + [Instrument("CCC", "Gamma Corp", "movers", "Twelve Data", "CCC")]
    monkeypatch.setattr(movers, "load_universe", lambda limit=movers.MOVERS_UNIVERSE_LIMIT: changed)
    assert movers.load_valid_movers_cache("2026-09-25", path=path) is None


def test_corrupt_cache_is_miss(monkeypatch, tmp_path):
    monkeypatch.setattr(movers, "load_universe", lambda limit=movers.MOVERS_UNIVERSE_LIMIT: _universe())
    path = tmp_path / "movers_snapshot.json"
    path.write_text("{not json", encoding="utf-8")
    assert movers.load_valid_movers_cache("2026-09-25", path=path) is None


def test_incomplete_cache_is_miss(monkeypatch, tmp_path):
    path, _ = _write_cache(tmp_path, monkeypatch, top_losers=[])
    assert movers.load_valid_movers_cache("2026-09-25", path=path) is None


def test_cached_movers_are_reused_without_api_request(monkeypatch, tmp_path):
    path, payload = _write_cache(tmp_path, monkeypatch)
    monkeypatch.setattr(movers, "OUTPUT_DIR", tmp_path)

    class NoRequestClient:
        def fetch_stock_batch(self, *args, **kwargs):
            raise AssertionError("mover API request should not occur on cache hit")

    result = movers.run_movers_snapshot(NoRequestClient(), logging.getLogger("test-movers-cache"), "2026-09-25")
    assert result == path
    reused = json.loads(path.read_text(encoding="utf-8"))
    assert reused["top_gainers"] == payload["top_gainers"]
    assert reused["top_losers"] == payload["top_losers"]
    assert reused["cache_status"] == "HIT"


def test_refresh_flag_bypasses_valid_cache(monkeypatch, tmp_path):
    path, _ = _write_cache(tmp_path, monkeypatch)
    monkeypatch.setattr(movers, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(movers, "run_shadow_benchmark", lambda *args, **kwargs: None)
    requested = []

    class RefreshClient:
        request_count = successful_api_requests = retry_requests = 0

        class Limiter:
            daily_requests = rate_limit_waits = 0

        rate_limiter = Limiter()

        def fetch_stock_batch(self, batch, logger, market_date=None):
            requested.extend(batch)
            return {
                item.symbol: {
                    **_item(item.symbol, 1.0 if item.symbol == "AAA" else -1.0),
                    "open": 100.0,
                    "high": 102.0,
                    "low": 99.0,
                    "volume": 1000,
                }
                for item in batch
            }

    result = movers.run_movers_snapshot(RefreshClient(), logging.getLogger("test-movers-cache"), "2026-09-25", force_refresh=True)
    assert result == path
    assert requested == _universe()
