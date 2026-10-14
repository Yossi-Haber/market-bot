import logging

from collectors.fred import FredClient
from collectors.market import TwelveDataClient
from collectors.yahoo import YahooClient
from config import Instrument


def _bars():
    return [
        {"datetime": "2026-09-29T16:00:00Z", "close": "110"},
        {"datetime": "2026-09-24T16:00:00Z", "close": "100"},
        {"datetime": "2026-09-23T16:00:00Z", "close": "99"},
    ]


def test_twelve_data_selects_exact_historical_date_and_previous_session():
    client = TwelveDataClient("key", rate_limiter=type("Limiter", (), {})())
    result = client._stock_result_from_payload(
        Instrument("SPY", "SPY", "core"), {"values": _bars()}, {"symbol": "SPY", "source": "Twelve Data"}, market_date="2026-09-24"
    )
    assert result["verified"] is True
    assert result["selected_source_date"] == "2026-09-24"
    assert result["previous_source_date"] == "2026-09-23"


def test_twelve_data_historical_request_uses_next_calendar_day(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {"SPY": {"values": _bars()}}

        def raise_for_status(self):
            return None

    def fake_get(url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("collectors.market.requests.get", fake_get)
    limiter = type("Limiter", (), {"before_request": lambda self, logger, credits_required=1: False, "record_response": lambda *args: None})()
    result = TwelveDataClient("key", rate_limiter=limiter).fetch_stock_batch(
        [Instrument("SPY", "SPY", "movers", provider_symbol="SPY")], logging.getLogger("test"), market_date="2026-09-25"
    )
    assert captured["params"]["end_date"] == "2026-09-26"
    assert result["SPY"]["error"] == "MOVER_DATE_MISMATCH"


def test_yahoo_historical_selection_ignores_newer_observation():
    # Use date-shaped rows through the parser's normal timestamp path.
    import datetime as dt
    from datetime import timezone

    def stamp(value):
        return int(dt.datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())

    observations = YahooClient._observations(
        [stamp("2026-09-29T16:00:00"), stamp("2026-09-24T16:00:00"), stamp("2026-09-23T16:00:00")],
        {"close": [110, 100, 99]},
        market_date="2026-09-24",
    )
    assert [row["date"].isoformat() for row in observations[:2]] == ["2026-09-24", "2026-09-23"]


def test_fred_asof_selection_ignores_future_observations():
    valid = FredClient._valid_observations([
        {"date": "2026-09-29", "value": "5.2"},
        {"date": "2026-09-24", "value": "5.1"},
        {"date": "2026-09-23", "value": "5.0"},
    ])
    selected = [item for item in valid if item[0].isoformat() <= "2026-09-24"]
    assert selected[0][0].isoformat() == "2026-09-24"
