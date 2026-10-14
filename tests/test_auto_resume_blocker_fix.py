import json
import logging

import pytest

from evaluation import auto_resume_phase_b as auto
from evaluation import resume_extended_dataset_after_dxy as resume
from evaluation import run_extended_dataset_v2 as runner


def test_partial_finnhub_cache_fetches_only_missing_symbol(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setattr(runner, "cache_root", lambda: cache)
    day = "2026-09-04"
    symbols = ["AAPL", "AMD", "AVGO", "KLAC"]
    for symbol in symbols[:-1]:
        directory = cache / "finnhub" / symbol
        directory.mkdir(parents=True)
        (directory / f"{day}_2026-09-05.json").write_text("{}", encoding="utf-8")

    requested = []

    class FakeProvider:
        live_requests = 0

        def search_company(self, symbol, start, end):
            requested.append((symbol, start, end))
            self.live_requests += 1
            target = cache / "finnhub" / symbol / f"{start}_{end}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps([{"headline": "cached"}]), encoding="utf-8")
            return []

    missing, calls = runner.complete_missing_finnhub_caches(
        day, symbols, logging.getLogger("test"), provider_factory=FakeProvider,
    )
    assert requested == [("KLAC", day, "2026-09-05")]
    assert missing == []
    assert calls == 1
    assert runner.missing_finnhub_symbols(day, symbols) == []
    # Existing dated cache contents are preserved.
    assert (cache / "finnhub" / "AAPL" / f"{day}_2026-09-05.json").read_text(encoding="utf-8") == "{}"


def test_complete_finnhub_cache_does_not_refetch(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setattr(runner, "cache_root", lambda: cache)
    day = "2026-09-04"
    directory = cache / "finnhub" / "KLAC"
    directory.mkdir(parents=True)
    (directory / f"{day}_2026-09-05.json").write_text("[]", encoding="utf-8")

    class NeverProvider:
        def __init__(self):
            raise AssertionError("complete cache must not instantiate provider")

    assert runner.complete_missing_finnhub_caches(
        day, ["KLAC"], logging.getLogger("test"), provider_factory=NeverProvider,
    ) == ([], 0)


def test_partial_stock_news_makes_existing_driver_stale(tmp_path, monkeypatch):
    day = "2026-09-04"
    monkeypatch.setattr(resume, "MILESTONE", tmp_path)
    day_dir = tmp_path / "history" / day
    day_dir.mkdir(parents=True)
    (day_dir / "market_snapshot.json").write_text(json.dumps({"market_date": day}), encoding="utf-8")
    (day_dir / "movers_snapshot.json").write_text(json.dumps({"market_date": day}), encoding="utf-8")
    (day_dir / "market_driver_snapshot.json").write_text(
        json.dumps({"market_date": day, "driver_analysis": {}, "metrics": {}}), encoding="utf-8",
    )
    monkeypatch.setattr(resume.historical, "_validate_historical_market", lambda *a, **k: True)
    monkeypatch.setattr(resume.movers, "load_valid_movers_cache", lambda *a, **k: {"top_gainers": []})
    monkeypatch.setattr(resume.historical, "_market_news_state", lambda _: "HIT")
    monkeypatch.setattr(resume.historical, "_mover_symbols", lambda _: ["A", "B", "C"])
    monkeypatch.setattr(resume.historical, "_finnhub_cache_status", lambda *a: (2, 3))

    state = resume._artifact_states(day)
    assert state["stock_news_status"] == "PARTIAL"
    assert state["snapshot_status"] == "STALE_STOCK_NEWS_DEPENDENCY"
    assert state["verification_status"] == "DEFERRED"
    assert "stock_news" in state["_actions"]
    assert "market_driver_snapshot" in state["_actions"]


def test_atomic_summary_write_succeeds_and_retries_sharing_violation(tmp_path):
    path = tmp_path / "auto_resume_summary.json"
    calls = []
    sleeps = []

    def replace(source, target):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError("transient sharing violation")
        auto.os.replace(source, target)

    auto._atomic_json_retry(path, {"status": "ok"}, replace=replace, sleep=sleeps.append)
    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "ok"}
    assert len(calls) == 2
    assert sleeps == [0.05]
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_summary_write_preserves_old_file_after_bounded_failure(tmp_path):
    path = tmp_path / "auto_resume_summary.json"
    path.write_text('{"old": true}', encoding="utf-8")
    sleeps = []

    def locked_replace(source, target):
        raise PermissionError("persistent sharing violation")

    with pytest.raises(PermissionError, match="AUTO_RESUME_SUMMARY_WRITE_FAILED"):
        auto._atomic_json_retry(path, {"new": True}, replace=locked_replace, sleep=sleeps.append)
    assert json.loads(path.read_text(encoding="utf-8")) == {"old": True}
    assert sleeps == [0.05, 0.1, 0.15000000000000002]
    assert not list(tmp_path.glob(".*.tmp"))
