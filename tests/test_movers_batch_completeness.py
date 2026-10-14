import json
import logging

import pytest
from collectors.market import TwelveDataClient

import movers
from config import Instrument


def _universe():
    return [Instrument(f"S{index:03d}", f"Symbol {index}", "movers", "Twelve Data", f"S{index:03d}") for index in range(120)]


def _result(instrument, change_pct=0.5):
    return {
        "symbol": instrument.symbol,
        "close": 100.0 + change_pct,
        "previous_close": 100.0,
        "change": change_pct,
        "change_pct": change_pct,
        "open": 100.0,
        "high": 101.0 + change_pct,
        "low": 99.0,
        "volume": 1000,
        "latest_bar_datetime": "2026-09-25T16:00:00Z",
        "source": "Twelve Data",
        "verified": True,
    }


class PassingLimiter:
    daily_requests = 0
    rate_limit_waits = 0


class QuotaAfterFirstLimiter(PassingLimiter):
    def __init__(self):
        self.calls = 0

    def before_request(self, logger, credits_required=1):
        self.calls += 1
        if self.calls > 1:
            self.rate_limit_waits += 1
            logger.info("test quota wait/reset")
        self.daily_requests += credits_required


class CompleteBatchClient:
    def __init__(self):
        self.rate_limiter = QuotaAfterFirstLimiter()
        self.request_count = 0
        self.successful_api_requests = 0
        self.retry_requests = 0
        self.batches = []

    def fetch_stock_batch(self, batch, logger, market_date=None):
        self.batches.append([item.symbol for item in batch])
        self.rate_limiter.before_request(logger, credits_required=len(batch))
        self.request_count += 1
        self.successful_api_requests += len(batch)
        return {item.symbol: _result(item) for item in batch}


def test_all_15_batches_survive_quota_wait_and_preserve_order(monkeypatch, tmp_path):
    universe = _universe()
    client = CompleteBatchClient()
    monkeypatch.setattr(movers, "load_universe", lambda limit=movers.MOVERS_UNIVERSE_LIMIT: universe)
    monkeypatch.setattr(movers, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(movers, "run_shadow_benchmark", lambda *args, **kwargs: None)

    path = movers.run_movers_snapshot(client, logging.getLogger("batch-test"), "2026-09-25", force_refresh=True)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert len(client.batches) == 15
    assert client.batches[0] == [f"S{index:03d}" for index in range(8)]
    assert client.batches[-1] == [f"S{index:03d}" for index in range(112, 120)]
    assert client.rate_limiter.rate_limit_waits == 14
    assert snapshot["collection_status"] == "SUCCESS"
    assert snapshot["symbols_requested"] == 120
    assert snapshot["symbols_returned"] == 120
    assert snapshot["symbols_valid"] == 120
    assert len(snapshot["batches"]) == 15
    assert all(batch["state"] == "SUCCESS" for batch in snapshot["batches"])


def test_fifteen_rate_limited_batches_each_perform_http(monkeypatch, tmp_path, caplog):
    universe = _universe()

    class Response:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            payload = {}
            for symbol in current_symbols:
                payload[symbol] = {"values": [{"datetime": "2026-09-25T16:00:00Z", "close": "101", "open": "100", "high": "102", "low": "99", "volume": "1000"}, {"datetime": "2026-09-24T16:00:00Z", "close": "100"}]}
            return payload

    class Limiter:
        def __init__(self):
            self.calls = 0
            self.waits = 0
            self.daily_requests = 0
            self.rate_limit_waits = 0

        def before_request(self, logger, credits_required=1):
            self.calls += 1
            self.daily_requests += credits_required
            if self.calls > 1:
                self.rate_limit_waits += 1
                logger.info("WAIT")
                return True
            return False

        def record_response(self, response, logger):
            return None

    current_symbols = []
    http_calls = []

    def fake_get(url, params, **kwargs):
        current_symbols[:] = params["symbol"].split(",")
        http_calls.append(list(current_symbols))
        return Response()

    monkeypatch.setattr("collectors.market.requests.get", fake_get)
    monkeypatch.setattr(movers, "load_universe", lambda limit=movers.MOVERS_UNIVERSE_LIMIT: universe)
    monkeypatch.setattr(movers, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(movers, "run_shadow_benchmark", lambda *args, **kwargs: None)
    client = TwelveDataClient("test-key", rate_limiter=Limiter())
    with caplog.at_level(logging.INFO):
        movers.run_movers_snapshot(client, logging.getLogger("batch-http-test"), "2026-09-25", force_refresh=True)
    assert len(http_calls) == 15
    assert client.request_count == 15
    assert sum(len(batch) for batch in http_calls) == 120
    assert caplog.text.count("WAIT_COMPLETE") == 14
    assert caplog.text.count("HTTP_ATTEMPT 1/2") == 15
    assert caplog.text.count("HTTP_RESPONSE") == 15
    assert "EMPTY_RESULT" not in caplog.text


def test_later_batch_symbols_can_reach_true_top_five(monkeypatch, tmp_path):
    universe = _universe()
    special = {"S080": 5.35, "S081": 4.79, "S082": 4.63, "S083": 4.63, "S084": 4.15}
    client = CompleteBatchClient()
    monkeypatch.setattr(movers, "load_universe", lambda limit=movers.MOVERS_UNIVERSE_LIMIT: universe)
    monkeypatch.setattr(movers, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(movers, "run_shadow_benchmark", lambda *args, **kwargs: None)
    original = client.fetch_stock_batch

    def fetch(batch, logger, market_date=None):
        result = original(batch, logger, market_date=market_date)
        for item in batch:
            if item.symbol in special:
                result[item.symbol] = _result(item, special[item.symbol])
        return result

    client.fetch_stock_batch = fetch
    path = movers.run_movers_snapshot(client, logging.getLogger("batch-test"), "2026-09-25", force_refresh=True)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert [item["symbol"] for item in snapshot["top_gainers"][:5]] == ["S080", "S081", "S082", "S083", "S084"]


def test_incomplete_ground_truth_blocks_ranking_and_shadow(monkeypatch, tmp_path):
    universe = _universe()
    client = CompleteBatchClient()
    shadow_calls = []
    monkeypatch.setattr(movers, "load_universe", lambda limit=movers.MOVERS_UNIVERSE_LIMIT: universe)
    monkeypatch.setattr(movers, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(movers, "run_shadow_benchmark", lambda *args, **kwargs: shadow_calls.append(True))

    def partial(batch, logger, market_date=None):
        client.batches.append([item.symbol for item in batch])
        client.rate_limiter.before_request(logger, credits_required=len(batch))
        client.request_count += 1
        return {item.symbol: _result(item) for item in batch} if len(client.batches) <= 2 else {}

    client.fetch_stock_batch = partial
    with pytest.raises(movers.MoversCollectionFailure, match="INCOMPLETE_TWELVE_DATA_GROUND_TRUTH"):
        movers.run_movers_snapshot(client, logging.getLogger("batch-test"), "2026-09-25", force_refresh=True)
    snapshot = json.loads((tmp_path / "movers_snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["collection_status"] == "FAILED"
    assert snapshot["top_gainers"] == []
    assert snapshot["top_losers"] == []
    assert shadow_calls == []
