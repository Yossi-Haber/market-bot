from __future__ import annotations

import json
from pathlib import Path

import pytest

import evaluation.run_targeted_mover_recovery_v1 as recovery
from evaluate_market_drivers import offline_network_guard


def _snapshot(day: str, *, integrity: str = "PASS", count: int = 120) -> dict:
    return {
        "market_date": day,
        "collection_status": "SUCCESS",
        "historical_integrity": {
            "requested_market_date": day,
            "integrity_status": integrity,
            "future_data_leak_detected": False,
            "max_source_date": day,
        },
        "symbols_requested": count,
        "symbols_valid": count,
        "symbols_failed": 0,
        "top_gainers": [{"symbol": f"G{i}"} for i in range(5)],
        "top_losers": [{"symbol": f"L{i}"} for i in range(5)],
    }


def _setup(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    milestone = tmp_path / "milestone"
    source_root = tmp_path / "recovered"
    monkeypatch.setattr(recovery, "MILESTONE", milestone)
    monkeypatch.setattr(recovery, "SOURCE_ROOT", source_root)
    monkeypatch.setattr(recovery.movers, "load_valid_movers_cache", lambda *_a, **_k: {"valid": True})
    return milestone, source_root


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_valid_recovered_artifact_outranks_failed_history(monkeypatch, tmp_path):
    milestone, recovered = _setup(monkeypatch, tmp_path)
    day = "2026-08-19"
    _write(milestone / "history" / day / "movers_snapshot.json", _snapshot(day, integrity="FAIL"))
    recovered_path = recovered / day / "movers_snapshot.json"
    _write(recovered_path, _snapshot(day))

    path, source = recovery._resolve_replay_mover_source(day)

    assert path == recovered_path
    assert source == "RECOVERED"


@pytest.mark.parametrize("recovered_payload", [
    _snapshot("2026-08-19", count=40),
    _snapshot("2026-08-18"),
    _snapshot("2026-08-19", integrity="FAIL"),
])
def test_invalid_recovered_artifact_does_not_outrank_valid_history(monkeypatch, tmp_path, recovered_payload):
    milestone, recovered = _setup(monkeypatch, tmp_path)
    day = "2026-08-19"
    history_path = milestone / "history" / day / "movers_snapshot.json"
    _write(history_path, _snapshot(day))
    _write(recovered / day / "movers_snapshot.json", recovered_payload)

    path, source = recovery._resolve_replay_mover_source(day)

    assert path == history_path
    assert source == "HISTORY"


def test_normal_date_keeps_history_and_labels_sep14_validated_existing(monkeypatch, tmp_path):
    milestone, recovered = _setup(monkeypatch, tmp_path)
    day = "2026-09-14"
    history_path = milestone / "history" / day / "movers_snapshot.json"
    _write(history_path, _snapshot(day, count=40))
    _write(recovered / day / "movers_snapshot.json", _snapshot(day))

    path, source = recovery._resolve_replay_mover_source(day)

    assert path == history_path
    assert source == "VALIDATED_EXISTING"


def test_no_valid_replay_input_fails_preflight(monkeypatch, tmp_path):
    milestone, recovered = _setup(monkeypatch, tmp_path)
    day = "2026-09-22"
    _write(recovered / day / "movers_snapshot.json", _snapshot(day, integrity="FAIL"))

    with pytest.raises(RuntimeError, match="NO_VALID_REPLAY_MOVER_SOURCE"):
        recovery._resolve_replay_mover_source(day)


def test_offline_replay_network_guard_blocks_provider_requests():
    import requests

    with offline_network_guard(), pytest.raises(RuntimeError, match="OFFLINE_NETWORK_CALL_BLOCKED"):
        requests.get("https://example.invalid")
