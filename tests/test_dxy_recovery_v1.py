import json
import logging
from datetime import datetime, timezone

from collectors.yahoo import YahooClient
from evaluation.dxy_recovery_v1 import (
    _existing_valid,
    _patch_snapshot,
    atomic_write,
    build_dxy_records,
    recover,
    validate_dxy_record,
)


def _row(day, close, *, timestamp=None, timezone_name="America/New_York", **extra):
    open_value = close - 0.1 if close is not None else None
    high_value = close + 0.2 if close is not None else None
    low_value = close - 0.2 if close is not None else None
    return {
        "market_date": day,
        "source_timestamp_utc": timestamp or f"{day}T04:00:00+00:00",
        "source_timezone": timezone_name,
        "open": open_value,
        "high": high_value,
        "low": low_value,
        "close": close,
        "volume": 0,
        **extra,
    }


def test_range_rows_calculate_previous_valid_session_over_weekend():
    values = build_dxy_records(
        [_row("2026-09-24", 100), _row("2026-09-25", 101), _row("2026-09-28", 102)],
        ["2026-09-28"],
    )["2026-09-28"]
    assert values["status"] == "AVAILABLE_VALID"
    assert values["record"]["previous_observation_date"] == "2026-09-25"
    assert values["record"]["change_pct"] == round(((102 / 101) - 1) * 100, 6)


def test_holiday_gap_uses_previous_returned_valid_bar_not_calendar_day():
    values = build_dxy_records(
        [_row("2026-09-04", 100), _row("2026-09-08", 99)],
        ["2026-09-08"],
    )["2026-09-08"]
    assert values["status"] == "AVAILABLE_VALID"
    assert values["record"]["previous_observation_date"] == "2026-09-04"


def test_missing_previous_session_is_explicit():
    value = build_dxy_records([_row("2026-08-17", 99)], ["2026-08-17"])["2026-08-17"]
    assert value["status"] == "MISSING_PREVIOUS_SESSION"


def test_invalid_yahoo_row_is_not_promoted():
    value = build_dxy_records([_row("2026-09-24", 100), _row("2026-09-25", None)], ["2026-09-25"])["2026-09-25"]
    assert value["status"] == "INVALID_VALUE"


def test_future_session_never_selected_for_historical_date():
    values = build_dxy_records(
        [_row("2026-09-24", 100), _row("2026-09-25", 101), _row("2026-09-28", 999)],
        ["2026-09-25"],
    )
    assert values["2026-09-25"]["record"]["market_date"] == "2026-09-25"
    assert values["2026-09-25"]["record"]["previous_close"] == 100


def test_yahoo_timezone_conversion_uses_exchange_local_market_date(monkeypatch):
    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            # 03:59 UTC is still the previous NY calendar date; 04:00 UTC is
            # midnight in New York during daylight-saving time.
            timestamps = [
                int(datetime(2026, 8, 17, 3, 59, tzinfo=timezone.utc).timestamp()),
                int(datetime(2026, 8, 17, 4, 0, tzinfo=timezone.utc).timestamp()),
            ]
            return {"chart": {"result": [{
                "meta": {"exchangeTimezoneName": "America/New_York"},
                "timestamp": timestamps,
                "indicators": {"quote": [{"open": [1, 2], "high": [2, 3], "low": [0.5, 1.5], "close": [1, 2], "volume": [0, 0]}]},
            }], "error": None}}

    monkeypatch.setattr("collectors.yahoo.requests.get", lambda *args, **kwargs: Response())
    client = YahooClient()
    from config import MARKET_SYMBOLS
    result = client.fetch_history_range(MARKET_SYMBOLS["macro"]["DXY"], "2026-08-16", "2026-08-17", logging.getLogger("test"))
    assert result["status"] == "AVAILABLE_VALID"
    assert result["rows"][0]["market_date"] == "2026-08-16"
    assert result["rows"][1]["market_date"] == "2026-08-17"
    assert result["rows"][1]["source_timestamp_utc"] == "2026-08-17T04:00:00+00:00"


def test_range_request_uses_exclusive_next_day_bound_and_exchange_metadata(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"chart": {"result": [{"meta": {"exchangeTimezoneName": "America/New_York"}, "timestamp": [], "indicators": {"quote": [{}]}}], "error": None}}

    def fake_get(url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("collectors.yahoo.requests.get", fake_get)
    from config import MARKET_SYMBOLS
    result = YahooClient().fetch_history_range(MARKET_SYMBOLS["macro"]["DXY"], "2026-08-14", "2026-09-28", logging.getLogger("test"))
    assert result["status"] == "AVAILABLE_VALID"
    assert captured["params"]["period2"] - captured["params"]["period1"] == 46 * 86400
    assert captured["timeout"] == (5, 15)


def test_canonical_schema_passes_and_wrong_or_invalid_fields_are_classified():
    record = build_dxy_records([_row("2026-09-24", 100), _row("2026-09-25", 101)], ["2026-09-25"])["2026-09-25"]["record"]
    assert validate_dxy_record(record, "2026-09-25", expected_previous_date="2026-09-24") == ("AVAILABLE_VALID", None)
    assert validate_dxy_record({**record, "market_date": "2026-09-24"}, "2026-09-25")[0] == "INVALID_DATE"
    assert validate_dxy_record({**record, "close": "bad"}, "2026-09-25")[0] == "INVALID_VALUE"
    assert validate_dxy_record({**record, "source": "FRED"}, "2026-09-25")[0] == "INVALID_SCHEMA"
    assert validate_dxy_record({**record, "previous_observation_date": "2026-09-23"}, "2026-09-25", expected_previous_date="2026-09-24")[0] == "MISSING_PREVIOUS_SESSION"


def test_valid_existing_cache_is_preserved_byte_for_byte(tmp_path):
    date = "2026-09-25"
    record = build_dxy_records([_row("2026-09-24", 100), _row(date, 101)], [date])[date]["record"]
    # Match the valid established cache contract, which does not yet carry raw
    # Yahoo timestamp provenance.
    record.pop("source_timezone")
    record.pop("source_timestamp_utc")
    record.pop("acquired_at_utc")
    record.pop("integrity")
    snapshot = {"market_date": date, "market": {"macro": {"DXY": record}}, "cache_metadata": {}}
    path = tmp_path / "market_snapshot.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    before = path.read_bytes()
    assert _existing_valid(record, date)
    assert _patch_snapshot(path, date, {**record, "close": 200}) is False
    assert path.read_bytes() == before


def test_cache_write_is_atomic_and_leaves_valid_json(tmp_path):
    path = tmp_path / "nested" / "record.json"
    atomic_write(path, '{"ok": true}\n')
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
    assert list(path.parent.iterdir()) == [path]


def test_patch_updates_only_dxy_and_top_level_validation_metadata(tmp_path):
    day = "2026-09-25"
    record = build_dxy_records([_row("2026-09-24", 100), _row(day, 101)], [day])[day]["record"]
    existing_other = {"symbol": "SPY", "verified": True, "validation_passed": True, "selected_source_date": day}
    path = tmp_path / "market_snapshot.json"
    path.write_text(json.dumps({
        "market_date": day, "market": {"core": {"SPY": existing_other}, "macro": {"DXY": {"symbol": "DXY", "verified": False, "validation_passed": False}}},
        "cache_metadata": {}, "summary": {"failed": 1}, "historical_integrity": {"requested_market_date": day, "future_data_leak_detected": False, "asof_macro_series": []},
    }), encoding="utf-8")
    assert _patch_snapshot(path, day, record)
    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["market"]["core"]["SPY"] == existing_other
    assert updated["market"]["macro"]["DXY"]["source"] == "Yahoo Finance / ICE Futures delayed"


def test_dxy_recovery_has_zero_other_provider_requests_and_emits_reports(tmp_path):
    day = "2026-09-25"

    class FakeYahoo:
        request_count = 1

        def fetch_history_range(self, instrument, start, end, logger):
            assert instrument.provider == "Yahoo Finance"
            return {"status": "AVAILABLE_VALID", "rows": [_row("2026-09-24", 100), _row(day, 101)]}

    stage = tmp_path / "stage"
    snapshot = {
        "market_date": day,
        "market": {"core": {"SPY": {"symbol": "SPY", "verified": True, "validation_passed": True, "selected_source_date": day}}, "macro": {"DXY": {"symbol": "DXY", "verified": False, "validation_passed": False}}},
        "cache_metadata": {}, "summary": {}, "historical_integrity": {"requested_market_date": day, "future_data_leak_detected": False, "asof_macro_series": []},
    }
    snapshot_path = stage / "history" / day / "market_snapshot.json"
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    summary = recover(stage, [day], FakeYahoo(), logging.getLogger("test"))
    assert summary["valid_dates"] == 1
    assert summary["twelve_data_credits"] == summary["finnhub_requests"] == summary["fmp_requests"] == 0
    assert summary["request"]["requests"] == 1
    assert (stage / "dxy_recovery_v1" / "dxy_coverage.csv").exists()
    assert (stage / "dxy_recovery_v1" / "dxy_diagnosis.md").exists()
