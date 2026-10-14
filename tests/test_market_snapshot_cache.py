import json
from datetime import datetime, timezone

import app
from config import MARKET_SYMBOLS
from market_date import select_as_of_bars


def _market_item(category, symbol, *, as_of=True):
    item = {
        "symbol": symbol,
        "source": "Twelve Data" if category != "macro" else "FRED",
        "verified": True,
        "validation_passed": True,
        "canonical_market_date": "2026-09-25",
        "selected_observation_date": "2026-09-25" if as_of else "2026-09-24",
        "freshness": "current" if as_of else "lagging",
        "latest_bar_datetime": "2026-09-25T16:00:00Z",
        "close": 100.0,
        "previous_close": 99.0,
        "change": 1.0,
        "change_pct": 1.010101,
    }
    if category == "macro":
        item["source"] = "FRED"
    return item


def _snapshot(*, date="2026-09-25", metadata=None, omit=None, macro_metadata=True):
    market = {}
    for category, group in MARKET_SYMBOLS.items():
        market[category] = {}
        for symbol in group:
            item = _market_item(category, symbol)
            if category == "macro" and not macro_metadata:
                item.pop("selected_observation_date")
            market[category][symbol] = item
    if omit:
        category, symbol = omit
        del market[category][symbol]
    return {
        "market_date": date,
        "market_date_resolution": {"source": "EXPLICIT", "agreement": "explicit"},
        "cache_metadata": {
            "canonical_market_date": "2026-09-25",
            "schema_version": app.MARKET_CACHE_SCHEMA_VERSION,
            "collector_version": app.MARKET_COLLECTOR_VERSION,
            "collection_status": "SUCCESS",
            "validation_status": "PASSED",
            "validation_passed": True,
            "cache_status": "MISS",
            "symbols_requested": sum(len(group) for group in market.values()),
            "required_symbols": [symbol for group in MARKET_SYMBOLS.values() for symbol in group],
            "market_date_resolution": {"source": "EXPLICIT", "agreement": "explicit"},
            **(metadata or {}),
        },
        "market": market,
        "summary": {"failed": 0},
    }


def _write_snapshot(tmp_path, payload):
    path = tmp_path / "market_snapshot.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_same_explicit_market_date_is_hit(tmp_path):
    path = _write_snapshot(tmp_path, _snapshot())
    assert app.load_valid_market_cache("2026-09-25", path=path) is not None


def test_different_date_is_miss(tmp_path):
    path = _write_snapshot(tmp_path, _snapshot())
    assert app.load_valid_market_cache("2026-09-26", path=path) is None


def test_invalid_snapshot_is_miss(tmp_path):
    path = _write_snapshot(tmp_path, _snapshot(metadata={"validation_passed": False}))
    assert app.load_valid_market_cache("2026-09-25", path=path) is None


def test_missing_instrument_is_miss(tmp_path):
    path = _write_snapshot(tmp_path, _snapshot(omit=("core", "SPY")))
    assert app.load_valid_market_cache("2026-09-25", path=path) is None


def test_stale_schema_is_miss(tmp_path):
    path = _write_snapshot(tmp_path, _snapshot(metadata={"schema_version": 999}))
    assert app.load_valid_market_cache("2026-09-25", path=path) is None


def test_macro_as_of_metadata_is_required(tmp_path):
    path = _write_snapshot(tmp_path, _snapshot(macro_metadata=False))
    assert app.load_valid_market_cache("2026-09-25", path=path) is None
    diagnostic = app.validate_market_snapshot_cache(json.loads(path.read_text(encoding="utf-8")), "2026-09-25")
    assert "MISSING_AS_OF_METADATA:GOLD" in diagnostic["reasons"]


def test_gold_weekend_observation_cannot_advance_report_date():
    item = {
        "symbol": "GOLD",
        "completed_bars": [
            {"datetime": "2026-09-27T00:00:00Z", "close": 110.0},
            {"datetime": "2026-09-25T00:00:00Z", "close": 100.0},
            {"datetime": "2026-09-24T00:00:00Z", "close": 99.0},
        ],
        "close": 100.0,
        "previous_close": 99.0,
    }
    selected = select_as_of_bars(item, "2026-09-25")
    assert selected["latest_bar_datetime"].startswith("2026-09-25")
    assert selected["close"] == 100.0


def test_refresh_market_flag_is_parsed():
    import run_daily

    assert run_daily.parse_args(["--refresh-market"]).refresh_market is True


def test_full_cache_input_contains_all_configured_instruments(tmp_path):
    path = _write_snapshot(tmp_path, _snapshot())
    cached = app.load_valid_market_cache("2026-09-25", path=path)
    assert cached["market"] == _snapshot()["market"]


def test_real_writer_then_reader_round_trip(tmp_path, monkeypatch):
    payload = _snapshot()
    payload["cache_metadata"] = app.build_market_cache_metadata(
        "2026-09-25",
        payload["market_date_resolution"],
        [item for group in payload["market"].values() for item in group.values()],
        cache_status="MISS",
    )
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    app.write_snapshot(payload, datetime.now(timezone.utc))
    assert app.load_valid_market_cache("2026-09-25", path=tmp_path / "market_snapshot.json") == payload


def test_two_explicit_runs_second_run_skips_all_market_collectors(tmp_path, monkeypatch):
    calls = []

    class FakeBase:
        def __init__(self, *args, **kwargs):
            self.request_count = 0
            self.successful_api_requests = 0
            self.retry_requests = 0
            self.rate_limiter = type("Limiter", (), {"rate_limit_waits": 0})()

    class FakeTwelve(FakeBase):
        def fetch_instrument(self, instrument, logger):
            calls.append(instrument.symbol)
            self.request_count += 1
            self.successful_api_requests += 1
            bars = [
                {"datetime": "2026-09-25T16:00:00Z", "close": 100.0},
                {"datetime": "2026-09-24T16:00:00Z", "close": 99.0},
            ]
            return {
                "symbol": instrument.symbol,
                "display_name": instrument.display_name,
                "source": "Twelve Data",
                "verified": True,
                "data_status": "completed_daily_bar",
                "close": 100.0,
                "previous_close": 99.0,
                "change": 1.0,
                "change_pct": 1.010101,
                "latest_bar_datetime": bars[0]["datetime"],
                "open": 99.5,
                "high": 101.0,
                "low": 98.5,
                "volume": 1000,
                "completed_bars": bars,
            }

    class FakeFred(FakeBase):
        def fetch_instrument(self, instrument, logger):
            self.request_count += 1
            self.successful_api_requests += 1
            return {"symbol": instrument.symbol, "source": "FRED", "verified": True, "close": 5.0, "previous_close": 4.9, "change": 0.1, "change_bps": 10.0, "latest_observation_date": "2026-09-25", "previous_observation_date": "2026-09-24"}

    class FakeCboe(FakeBase):
        def fetch_vix(self, instrument, logger):
            self.request_count += 1
            self.successful_api_requests += 1
            return {"symbol": instrument.symbol, "source": "CBOE", "verified": True, "close": 20.0, "previous_close": 19.0, "change_pct": 5.0, "latest_observation_date": "2026-09-25", "previous_observation_date": "2026-09-24", "open": 19.5, "high": 20.5, "low": 19.0}

    class FakeYahoo(FakeBase):
        def fetch_instrument(self, instrument, logger):
            self.request_count += 1
            self.successful_api_requests += 1
            return {"symbol": instrument.symbol, "source": "Yahoo Finance / ICE Futures delayed", "verified": True, "close": 100.0, "previous_close": 99.0, "change_pct": 1.0, "latest_observation_date": "2026-09-25", "previous_observation_date": "2026-09-24", "open": 99.5, "high": 101.0, "low": 98.0}

    movers_path = tmp_path / "movers_snapshot.json"
    movers_path.write_text(json.dumps({"cache_status": "HIT", "symbols_requested": 120}), encoding="utf-8")
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(app, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(app, "LOG_FILE", tmp_path / "logs" / "market_bot.log")
    monkeypatch.setattr(app, "TwelveDataClient", FakeTwelve)
    monkeypatch.setattr(app, "FredClient", FakeFred)
    monkeypatch.setattr(app, "CboeClient", FakeCboe)
    monkeypatch.setattr(app, "YahooClient", FakeYahoo)
    monkeypatch.setattr(app, "run_movers_snapshot", lambda *args, **kwargs: movers_path)
    monkeypatch.setattr(app, "run_catalyst_snapshot", lambda *args, **kwargs: None)

    assert app.main(market_date_override="2026-09-25") == 0
    first_calls = list(calls)
    first = json.loads((tmp_path / "market_snapshot.json").read_text(encoding="utf-8"))
    assert first_calls
    assert app.main(market_date_override="2026-09-25") == 0
    second = json.loads((tmp_path / "market_snapshot.json").read_text(encoding="utf-8"))
    assert calls == first_calls
    assert second["market"] == first["market"]
    assert second["market_date"] == first["market_date"] == "2026-09-25"
