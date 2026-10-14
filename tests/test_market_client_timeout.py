import logging

import requests

from collectors.market import REQUEST_DEADLINE_SECONDS, REQUEST_TIMEOUT, TwelveDataClient
from config import Instrument


class FakeRateLimiter:
    def __init__(self):
        self.before_calls = 0
        self.record_calls = 0
        self.rate_limit_waits = 0
        self.daily_requests = 0

    def before_request(self, logger, credits_required=1):
        self.before_calls += 1

    def record_response(self, response, logger):
        self.record_calls += 1


def _instrument():
    return Instrument("XLY", "Consumer Discretionary", "sectors")


def test_hanging_http_call_retries_once_then_fails(monkeypatch, caplog):
    limiter = FakeRateLimiter()
    calls = []

    def hanging_get(*args, **kwargs):
        calls.append(kwargs)
        raise requests.ReadTimeout("simulated read hang")

    monkeypatch.setattr("collectors.market.requests.get", hanging_get)
    client = TwelveDataClient("test-key", rate_limiter=limiter)
    with caplog.at_level(logging.INFO):
        result = client.fetch_instrument(_instrument(), logging.getLogger("timeout-test"))

    assert result["verified"] is False
    assert result["error"] == "READ_TIMEOUT"
    assert len(calls) == 2
    assert all(call["timeout"] == REQUEST_TIMEOUT for call in calls)
    assert limiter.before_calls == 2
    assert client.retry_requests == 1
    assert "HTTP attempt 1/2" in caplog.text
    assert "HTTP attempt 2/2" in caplog.text
    assert "reason=READ_TIMEOUT" in caplog.text


def test_connect_timeout_is_classified_separately(monkeypatch):
    monkeypatch.setattr("collectors.market.requests.get", lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectTimeout("connect")))
    client = TwelveDataClient("test-key", rate_limiter=FakeRateLimiter())
    result = client.fetch_instrument(_instrument(), logging.getLogger("timeout-test"))
    assert result["error"] == "CONNECT_TIMEOUT"
    assert client.request_count == 2


def test_timeout_then_success_retries_once(monkeypatch):
    responses = [requests.ReadTimeout("first"), type("Response", (), {"status_code": 200, "headers": {}, "raise_for_status": lambda self: None, "json": lambda self: {"values": [{"datetime": "2026-09-25T16:00:00Z", "close": "101", "open": "100", "high": "102", "low": "99", "volume": "1000"}, {"datetime": "2026-09-24T16:00:00Z", "close": "100"}]}})()]
    monkeypatch.setattr("collectors.market.requests.get", lambda *args, **kwargs: (_ for _ in ()).throw(responses.pop(0)) if isinstance(responses[0], Exception) else responses.pop(0))
    client = TwelveDataClient("test-key", rate_limiter=FakeRateLimiter())
    result = client.fetch_instrument(_instrument(), logging.getLogger("timeout-test"))
    assert result["verified"] is True
    assert client.retry_requests == 1


def test_batch_timeout_also_retries_once(monkeypatch):
    calls = []

    def hanging_get(*args, **kwargs):
        calls.append(kwargs)
        raise requests.Timeout("batch hang")

    monkeypatch.setattr("collectors.market.requests.get", hanging_get)
    client = TwelveDataClient("test-key", rate_limiter=FakeRateLimiter())
    result = client.fetch_stock_batch([_instrument()], logging.getLogger("timeout-test"))
    assert result["XLY"]["error"] == "READ_TIMEOUT"
    assert len(calls) == 2
    assert client.retry_requests == 1


def test_request_deadline_is_finite():
    assert REQUEST_DEADLINE_SECONDS <= 60
    assert REQUEST_TIMEOUT == (5, 20)


def test_historical_batch_selects_requested_bar_and_previous_session(monkeypatch):
    request_params = []

    class Response:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "SAMPLE": {
                    "values": [
                        {"datetime": "2026-09-28T16:00:00Z", "close": "110"},
                        {"datetime": "2026-09-25T16:00:00Z", "close": "105"},
                        {"datetime": "2026-09-24T16:00:00Z", "close": "100"},
                    ]
                }
            }

    def fake_get(*args, **kwargs):
        request_params.append(kwargs["params"])
        return Response()

    monkeypatch.setattr("collectors.market.requests.get", fake_get)
    instrument = Instrument("SAMPLE", "Sample", "movers", "Twelve Data", "SAMPLE")
    client = TwelveDataClient("test-key", rate_limiter=FakeRateLimiter())
    result = client.fetch_stock_batch([instrument], logging.getLogger("historical-test"), market_date="2026-09-25")
    assert result["SAMPLE"]["verified"] is True
    assert result["SAMPLE"]["latest_bar_datetime"].startswith("2026-09-25")
    assert result["SAMPLE"]["previous_close"] == 100.0
    assert result["SAMPLE"]["close"] == 105.0
    assert request_params[0]["end_date"] == "2026-09-26"


def test_historical_end_date_handles_weekend_and_rollover():
    assert TwelveDataClient._historical_end_date("2026-09-25") == "2026-09-26"
    assert TwelveDataClient._historical_end_date("2026-12-31") == "2027-01-01"
    assert TwelveDataClient._historical_end_date("2026-02-28") == "2026-03-01"
