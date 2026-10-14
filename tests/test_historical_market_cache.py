import json
from datetime import datetime
from zoneinfo import ZoneInfo

import build_historical_market_cache as builder
import evaluate_market_drivers as evaluator


def test_weekend_is_non_trading_day(tmp_path, monkeypatch):
    assert builder.trading_day_status("2026-09-26") == "NON_TRADING_DAY"
    monkeypatch.setattr(builder, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(builder, "MANIFEST_PATH", tmp_path / "history" / "historical_cache_manifest.json")
    manifest = builder.build(["2026-09-26"], plan=True, max_days=10)
    assert manifest["dates"][0]["status"] == "NON_TRADING_DAY"


def test_max_days_counts_only_trading_days():
    try:
        builder.build(["2026-09-21", "2026-09-22"], plan=True, max_days=1)
    except ValueError as exc:
        assert str(exc) == "MAX_DAYS_EXCEEDED:2>1"
    else:
        raise AssertionError("max-days guard did not fire")


def test_plan_mode_does_not_call_collection(monkeypatch, tmp_path):
    monkeypatch.setattr(builder, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(builder, "MANIFEST_PATH", tmp_path / "history" / "historical_cache_manifest.json")
    monkeypatch.setattr(builder.app, "main", lambda **kwargs: (_ for _ in ()).throw(AssertionError("network collection")))
    manifest = builder.build(["2026-09-28"], plan=True)
    assert manifest["dates"][0]["status"] in {"PLAN_READY", "PLAN_NEEDS_BUILD"}
    assert (tmp_path / "history" / "historical_cache_manifest.json").exists()


def test_existing_dated_cache_is_reused_in_plan(tmp_path, monkeypatch):
    history = tmp_path / "history"
    day = history / "2026-09-28"
    day.mkdir(parents=True)
    market = {"market_date": "2026-09-28", "market": {}}
    movers = {"market_date": "2026-09-28", "top_gainers": [], "top_losers": []}
    (day / "market_snapshot.json").write_text(json.dumps(market), encoding="utf-8")
    (day / "movers_snapshot.json").write_text(json.dumps(movers), encoding="utf-8")
    monkeypatch.setattr(builder, "HISTORY_DIR", history)
    monkeypatch.setattr(builder, "MANIFEST_PATH", history / "historical_cache_manifest.json")
    monkeypatch.setattr(builder.app, "validate_market_snapshot_cache", lambda snapshot, market_date: {"valid": True})
    monkeypatch.setattr(builder.movers, "load_valid_movers_cache", lambda market_date, path=None: movers)
    inspection = builder.inspect_date("2026-09-28")
    assert inspection["market"] == "HIT"
    assert inspection["movers"] == "HIT"


def test_build_writes_date_scoped_snapshots_and_manifest(tmp_path, monkeypatch):
    history = tmp_path / "history"
    monkeypatch.setattr(builder, "HISTORY_DIR", history)
    monkeypatch.setattr(builder, "MANIFEST_PATH", history / "historical_cache_manifest.json")

    def fake_app(**kwargs):
        builder.app.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        integrity = {"requested_market_date": kwargs["market_date_override"], "future_data_leak_detected": False, "integrity_status": "PASS"}
        (builder.app.OUTPUT_DIR / "market_snapshot.json").write_text(json.dumps({"market_date": kwargs["market_date_override"], "market": {}, "historical_integrity": integrity, "total_http_requests": 0, "providers": {}}), encoding="utf-8")
        (builder.app.OUTPUT_DIR / "movers_snapshot.json").write_text(json.dumps({"market_date": kwargs["market_date_override"], "historical_integrity": integrity, "top_gainers": [], "top_losers": [], "request_statistics": {}}), encoding="utf-8")
        return 0

    def fake_runtime(market_date, output_dir, **kwargs):
        path = output_dir / "market_driver_snapshot.json"
        path.write_text(json.dumps({"market_date": market_date, "metrics": {"fmp_requests": 0}}), encoding="utf-8")
        return path

    monkeypatch.setattr(builder.app, "main", fake_app)
    monkeypatch.setattr(builder, "run_market_driver_pipeline", fake_runtime)
    result = builder.build(["2026-09-28"], max_days=10)
    entry = result["dates"][0]
    assert entry["status"] == "READY"
    assert (history / "2026-09-28" / "market_driver_snapshot.json").exists()
    assert json.loads((history / "historical_cache_manifest.json").read_text(encoding="utf-8"))["dates"][0]["market_date"] == "2026-09-28"


def test_refresh_flags_are_forwarded_without_changing_default(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(builder, "_history_path", lambda value: tmp_path / value)
    monkeypatch.setattr(builder.app, "main", lambda **kwargs: calls.append(kwargs) or 1)
    result = builder.build(["2026-09-28"], refresh_market=True, refresh_movers=True, refresh_news=True, refresh_verification=True)
    assert result["dates"][0]["status"] == "FAILED"
    # Collection was attempted with the explicitly requested flags.
    assert calls[0]["refresh_market"] is True
    assert calls[0]["refresh_movers"] is True


def test_evaluator_discovers_date_scoped_history(tmp_path):
    day = tmp_path / "history" / "2026-09-28"
    day.mkdir(parents=True)
    for name in ("market_snapshot.json", "movers_snapshot.json", "market_driver_snapshot.json"):
        (day / name).write_text(json.dumps({"market_date": "2026-09-28"}), encoding="utf-8")
    assert evaluator._dated_snapshot(tmp_path, "market_snapshot", "2026-09-28")["market_date"] == "2026-09-28"
    assert evaluator._dated_snapshot(tmp_path, "market_driver_snapshot", "2026-09-28")["market_date"] == "2026-09-28"


def test_missing_movers_defers_dependent_components(monkeypatch):
    monkeypatch.setattr(builder, "_snapshot_path", lambda prefix, market_date: None)
    monkeypatch.setattr(builder.app, "validate_market_snapshot_cache", lambda snapshot, market_date: {"valid": True})
    inspection = builder.inspect_date("2026-09-25")
    assert inspection["finnhub"] == "DEFERRED"
    assert inspection["verification"] == "DEFERRED"
    assert inspection["deferred"] == "news,verification,market_driver_snapshot"


def test_finnhub_coverage_states_are_dependency_aware(monkeypatch):
    movers_snapshot = {"market_date": "2026-09-25", "top_gainers": [{"symbol": f"S{i}"} for i in range(10)], "top_losers": []}
    monkeypatch.setattr(builder, "_snapshot_path", lambda prefix, market_date: object() if prefix in {"market_snapshot", "movers_snapshot"} else None)
    monkeypatch.setattr(builder, "_load", lambda path: {"market_date": "2026-09-25", "market": {}})
    monkeypatch.setattr(builder.app, "validate_market_snapshot_cache", lambda snapshot, market_date: {"valid": True})
    monkeypatch.setattr(builder.movers, "load_valid_movers_cache", lambda market_date, path=None: movers_snapshot)
    monkeypatch.setattr(builder, "_verification_ready", lambda market_date: False)
    for hits, expected in ((0, "0/10 MISS"), (5, "5/10 PARTIAL"), (10, "10/10 HIT")):
        monkeypatch.setattr(builder, "_finnhub_cache_status", lambda market_date, symbols, hits=hits: (hits, len(symbols)))
        assert builder.inspect_date("2026-09-25")["finnhub"] == expected


def test_plan_ready_is_explicit_and_counts_as_ready(monkeypatch):
    inspection = {
        "market": "HIT", "movers": "HIT", "finnhub": "10/10 HIT",
        "verification": "READY", "actions_needed": "none", "deferred": "none",
        "plan_ready": True,
    }
    monkeypatch.setattr(builder, "inspect_date", lambda market_date, **kwargs: inspection)
    result = builder.build(["2026-09-28"], plan=True)
    entry = result["dates"][0]
    assert entry["status"] == "PLAN_READY"
    assert entry["plan_ready"] is True


def test_current_open_us_session_is_incomplete_and_prior_day_is_trading_day():
    before_close = datetime(2026, 9, 30, 15, 59, tzinfo=ZoneInfo("America/New_York"))
    after_close = datetime(2026, 9, 30, 16, 1, tzinfo=ZoneInfo("America/New_York"))
    assert builder.trading_day_status("2026-09-30", now=before_close) == "INCOMPLETE_TRADING_DAY"
    assert builder.trading_day_status("2026-09-29", now=before_close) == "TRADING_DAY"
    assert builder.trading_day_status("2026-09-30", now=after_close) == "TRADING_DAY"


def test_build_skips_incomplete_current_session_without_network(monkeypatch, tmp_path):
    monkeypatch.setattr(builder, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(builder, "MANIFEST_PATH", tmp_path / "history" / "manifest.json")
    monkeypatch.setattr(builder.app, "main", lambda **kwargs: (_ for _ in ()).throw(AssertionError("network collection")))
    now = datetime(2026, 9, 30, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    result = builder.build(["2026-09-30"], plan=False, now=now)
    assert result["dates"][0]["status"] == "INCOMPLETE_TRADING_DAY"
