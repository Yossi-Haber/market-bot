import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from collectors.rate_limit import CreditRateLimitController, QuotaPauseRequired
from config import Instrument
from evaluation import resume_extended_dataset_after_dxy as resume
from evaluation.quota_runner import TwelveDataRequestLedger
import evaluate_market_drivers
import movers


class _Logger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


def test_long_wait_pauses_without_sleep_and_persists_next_eligible(tmp_path, monkeypatch):
    wall = [1_000.0]
    monkeypatch.setattr("collectors.rate_limit.time.time", lambda: wall[0])
    state_path = tmp_path / "limiter.json"
    sleeps = []
    limiter = CreditRateLimitController(
        state_path=state_path, max_blocking_wait_seconds=10,
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )
    limiter.credits_left = 0
    limiter.set_pending_request(market_date="2026-08-20", symbol="AAPL", purpose="MOVER_FINAL")
    with pytest.raises(QuotaPauseRequired) as caught:
        limiter.before_request(_Logger())
    assert sleeps == []
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["next_eligible_at"] == caught.value.next_eligible_at
    assert caught.value.context["symbol"] == "AAPL"


def test_restart_respects_persisted_window_and_sleeps_only_short_remainder(tmp_path, monkeypatch):
    wall = [1_000.0]
    monotonic = [0.0]
    monkeypatch.setattr("collectors.rate_limit.time.time", lambda: wall[0])
    state_path = tmp_path / "limiter.json"
    first = CreditRateLimitController(state_path=state_path, max_blocking_wait_seconds=10)
    first.credits_left = 0
    with pytest.raises(QuotaPauseRequired) as caught:
        first.before_request(_Logger())

    wall[0] = 1_015.0
    def sleeper(seconds):
        wall[0] += seconds
        monotonic[0] += seconds
    second = CreditRateLimitController(
        state_path=state_path, max_blocking_wait_seconds=30,
        monotonic_fn=lambda: monotonic[0], sleep_fn=sleeper,
    )
    second.before_request(_Logger())
    assert wall[0] >= 1_021.5
    assert second.next_eligible_at is None
    assert json.loads(state_path.read_text(encoding="utf-8"))["daily_requests"] == 1


def test_request_ledger_only_counts_new_requests_and_survives_inflight(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = TwelveDataRequestLedger(path)
    ledger.record({"event": "START", "request_id": "a", "timestamp": "t", "historical_date": "2026-08-20", "symbol": "AAPL", "purpose": "MOVER_FINAL", "credits": 8, "result": "IN_FLIGHT"})
    assert ledger.totals() == {"2026-08-20": 8}
    restored = TwelveDataRequestLedger(path)
    assert restored.rows[0]["result"] == "IN_FLIGHT"
    restored.record({"event": "FINISH", "request_id": "a", "result": "HTTP_200"})
    assert restored.rows[0]["result"] == "HTTP_200"
    assert restored.totals() == {"2026-08-20": 8}


def test_mover_checkpoint_skips_already_valid_symbols(tmp_path, monkeypatch):
    instruments = [Instrument("AAA", "A", "movers"), Instrument("BBB", "B", "movers")]
    checkpoint = tmp_path / "resume" / "movers.json"
    checkpoint.parent.mkdir(parents=True)
    cached = {"symbol": "AAA", "close": 11.0, "previous_close": 10.0, "change": 1.0, "change_pct": 10.0,
              "volume": 100, "latest_bar_datetime": "2026-08-20", "verified": True}
    checkpoint.write_text(json.dumps({"market_date": "2026-08-20", "valid_results": [cached], "errors": [], "batch_states": [{"batch_number": 1, "state": "PARTIAL"}]}), encoding="utf-8")

    class FakeClient:
        mover_checkpoint_path = checkpoint
        request_count = successful_api_requests = retry_requests = 0
        rate_limiter = SimpleNamespace(daily_requests=0, rate_limit_waits=0)
        calls = []
        def fetch_stock_batch(self, batch, logger, market_date=None):
            self.calls.append([item.symbol for item in batch])
            out = {}
            for item in batch:
                out[item.symbol] = {"symbol": item.symbol, "close": 12.0, "previous_close": 10.0,
                    "change": 2.0, "change_pct": 20.0, "volume": 100,
                    "latest_bar_datetime": "2026-08-20", "verified": True}
            return out

    client = FakeClient()
    result = movers._collect_mover_universe(client, _Logger(), "2026-08-20", instruments)
    assert client.calls == [["BBB"]]
    assert {row["symbol"] for row in result["valid_results"]} == {"AAA", "BBB"}
    assert result["collection_complete"] is True


def test_one_date_selector_limits_work_only():
    dates = ["d1", "d2", "d3"]
    states = [{"_actions": ["movers"]}, {"_actions": []}, {"_actions": ["movers"]}]
    assert resume.select_work_dates(dates, states, 1) == ["d1"]
    assert resume.select_work_dates(dates, states, None) == ["d1", "d3"]


def test_resume_sigterm_context_enters_translates_and_restores(monkeypatch):
    previous = object()
    installed = []

    monkeypatch.setattr(resume.signal, "getsignal", lambda signum: previous)
    monkeypatch.setattr(resume.signal, "signal", lambda signum, handler: installed.append(handler))

    with resume._convert_sigterm_to_interrupt():
        assert len(installed) == 1
        with pytest.raises(KeyboardInterrupt):
            installed[0](resume.signal.SIGTERM, None)

    assert installed[-1] is previous


def test_quota_pause_is_not_counted_as_failure():
    counts = resume.build_status_counts({"a": {"status": "QUOTA_PAUSED"}, "b": {"status": "FAILED"}})
    assert counts["QUOTA_PAUSED"] == 1
    assert counts["FAILED"] == 1


def test_readiness_reports_are_reconciled_and_queue_counts_are_explicit(tmp_path, monkeypatch):
    resume_dir = tmp_path / "resume"
    reliability = resume_dir / "runner_reliability_v1"
    resume_dir.mkdir()
    monkeypatch.setattr(resume, "RESUME_DIR", resume_dir)
    monkeypatch.setattr(resume, "RELIABILITY_DIR", reliability)
    monkeypatch.setattr(resume, "REQUEST_LEDGER_PATH", resume_dir / "ledger.json")
    dates = [f"2026-08-{i:02d}" for i in range(1, 31)]
    ready, partial, missing = dates[:11], dates[11:13], dates[13:]
    dataset = {"selected_dates": dates, "readiness_counts": {"READY": 11, "PARTIAL": 2, "FAILED": 17},
        "integrity_counts": {"PASS": 11, "WARN": 0, "FAIL": 19}, "skipped_dates": [{"market_date": d, "missing_components": ["movers"]} for d in missing],
        "metrics": {"stock_review_rows_abs_move_ge_4pct": 59, "stock_rows_all": 120, "diagnostic_event_sample_rows": 139, "offline_network_calls": 0, "provider_usage_totals": {"fmp_requests": 0}}}
    (resume_dir / "dataset_summary.json").write_text(json.dumps(dataset), encoding="utf-8")
    (resume_dir / "resume_summary.json").write_text(json.dumps({"replay_readiness": {"ready_days_evaluated": 12, "partial_days_evaluated": 1}}), encoding="utf-8")
    with (resume_dir / "integrity_report.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "readiness", "integrity", "reasons"]); writer.writeheader()
        writer.writerows([{"date": d, "readiness": "READY", "integrity": "PASS", "reasons": ""} for d in ready])
        writer.writerows([{"date": d, "readiness": "PARTIAL", "integrity": "FAIL", "reasons": "INVALID_REQUIRED_MOVER_ARTIFACT"} for d in partial])
    with (resume_dir / "pre_resume_status.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["market_date", "market_data_status"]); writer.writeheader()
        writer.writerows([{"market_date": d, "market_data_status": "HIT"} for d in dates])
    (resume_dir / "stock_review_queue.csv").write_text("date,symbol\n" + "2026-08-01,A\n" * 108, encoding="utf-8")
    result = resume.write_runner_reliability_report()
    assert result["readiness"] == {"READY": 11, "PARTIAL": 2, "MISSING": 17}
    assert result["reports_consistent"] is True
    assert result["all_stock_review_rows_if_applicable"] == 108
    assert result["fmp_requests"] == 0
    summary = json.loads((resume_dir / "resume_summary.json").read_text(encoding="utf-8"))
    assert summary["replay_readiness"]["ready_days_evaluated"] == 11
    check_rows = list(csv.DictReader((reliability / "readiness_consistency_check.csv").open(encoding="utf-8")))
    assert all((int(row["READY"]), int(row["PARTIAL"]), int(row["MISSING"])) == (11, 2, 17) for row in check_rows)


def test_offline_replay_guard_blocks_network(monkeypatch):
    with evaluate_market_drivers.offline_network_guard():
        with pytest.raises(RuntimeError, match="OFFLINE_NETWORK_CALL_BLOCKED"):
            evaluate_market_drivers.requests.get("https://example.invalid")
