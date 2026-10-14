from __future__ import annotations

import json
import importlib
from pathlib import Path

import build_historical_market_cache
import evaluation.run_targeted_mover_recovery_v1 as recovery


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _prepare(monkeypatch, tmp_path: Path, day: str, *, requested: int, valid: int, failed: int, prefilter: bool):
    monkeypatch.setattr(recovery, "MILESTONE", tmp_path / "milestone")
    monkeypatch.setattr(recovery, "SOURCE_ROOT", tmp_path / "recovery")
    monkeypatch.setattr(build_historical_market_cache, "_validate_historical_market", lambda _data, _day: True)
    snapshot = {
        "market_date": day,
        "collection_status": "SUCCESS",
        "historical_integrity": {
            "requested_market_date": day,
            "integrity_status": "PASS",
            "future_data_leak_detected": False,
            "max_source_date": day,
        },
        "symbols_requested": requested,
        "symbols_valid": valid,
        "symbols_failed": failed,
        "prefilter_enabled": prefilter,
        "candidate_pool_size": requested if prefilter else 0,
        "fallback_used": False,
        "top_gainers": [{"symbol": f"G{i}", "verified": True} for i in range(5)],
        "top_losers": [{"symbol": f"L{i}", "verified": True} for i in range(5)],
    }
    _write_json(recovery.MILESTONE / "history" / day / "market_snapshot.json", {"market_date": day})
    path = recovery.SOURCE_ROOT / day / "movers_snapshot.json" if day in recovery.TARGET_DATES else recovery.MILESTONE / "history" / day / "movers_snapshot.json"
    _write_json(path, snapshot)
    monkeypatch.setattr(recovery.movers, "load_valid_movers_cache", lambda *_args, **_kwargs: snapshot)


def test_existing_40_symbol_candidate_artifact_is_accepted(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path, "2026-09-14", requested=40, valid=40, failed=0, prefilter=True)
    assert recovery._validate_recovered_date("2026-09-14") == (True, "")


def test_recovery_date_still_requires_full_120_symbols(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path, "2026-09-22", requested=40, valid=40, failed=0, prefilter=True)
    assert recovery._validate_recovered_date("2026-09-22") == (False, "MOVER_COVERAGE_NOT_120_OF_120")


def test_non_target_partial_candidate_artifact_is_rejected(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path, "2026-09-14", requested=40, valid=39, failed=1, prefilter=True)
    assert recovery._validate_recovered_date("2026-09-14") == (False, "MOVER_COLLECTION_INCOMPLETE")


def test_offline_replay_redirects_imported_cache_constants(tmp_path):
    names = (
        "evaluate_market_drivers",
        "news.providers.finnhub",
        "news.verification_acquisition",
    )
    modules = {name: importlib.import_module(name) for name in names}
    previous = {
        (name, attribute): getattr(module, attribute)
        for name, module in modules.items()
        for attribute in ("CACHE_DIR", "OFFICIAL_DISCOVERY_CACHE_DIR")
        if hasattr(module, attribute)
    }
    try:
        recovery._redirect_loaded_cache_roots(tmp_path / "isolated-cache")
        assert modules["evaluate_market_drivers"].CACHE_DIR == tmp_path / "isolated-cache"
        assert modules["news.providers.finnhub"].CACHE_DIR == tmp_path / "isolated-cache" / "finnhub"
        assert modules["news.verification_acquisition"].CACHE_DIR == tmp_path / "isolated-cache" / "verification_v2"
        assert modules["news.verification_acquisition"].OFFICIAL_DISCOVERY_CACHE_DIR == tmp_path / "isolated-cache" / "verification_official_domains"
    finally:
        for (name, attribute), value in previous.items():
            setattr(modules[name], attribute, value)
