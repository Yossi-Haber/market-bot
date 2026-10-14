import json
from datetime import datetime, timezone

from evaluation import auto_resume_phase_b as auto


def test_daily_budget_uses_persisted_usage_and_configured_quota(tmp_path, monkeypatch):
    state = tmp_path / "limiter.json"
    state.write_text(json.dumps({"quota_day_utc": "2026-10-05", "daily_requests": 780}), encoding="utf-8")
    monkeypatch.setattr(auto.resume, "LIMITER_STATE_PATH", state)
    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
    budget = auto._daily_budget_state(40, now=now)
    assert budget["daily_quota"] == 800
    assert budget["daily_remaining"] == 20
    assert budget["sufficient"] is False
    assert budget["next_daily_reset"] == "2026-10-06T00:00:00+00:00"


def test_daily_budget_resets_usage_on_new_utc_quota_day(tmp_path, monkeypatch):
    state = tmp_path / "limiter.json"
    state.write_text(json.dumps({"quota_day_utc": "2026-10-04", "daily_requests": 800}), encoding="utf-8")
    monkeypatch.setattr(auto.resume, "LIMITER_STATE_PATH", state)
    budget = auto._daily_budget_state(120, now=datetime(2026, 10, 5, 0, 1, tzinfo=timezone.utc))
    assert budget["daily_used"] == 0
    assert budget["sufficient"] is True


def test_estimated_mover_credits_respect_checkpoint_and_valid_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(auto.resume, "MILESTONE", tmp_path)
    day = "2026-08-20"
    checkpoint = tmp_path / "history" / day / ".movers_build" / ".resume" / "mover_checkpoint.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(json.dumps({"market_date": day, "valid_results": [{}] * 40}), encoding="utf-8")
    assert auto.estimate_remaining_mover_credits(day, {"movers_status": "MISSING"}) == 80
    assert auto.estimate_remaining_mover_credits(day, {"movers_status": "HIT"}) == 0


def test_next_eligible_reads_pause_payload_and_preserves_wait(tmp_path, monkeypatch):
    limiter = tmp_path / "limiter.json"
    limiter.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(auto.resume, "LIMITER_STATE_PATH", limiter)
    stamp = datetime.now(timezone.utc).replace(microsecond=0)
    progress = {"quota_pause": json.dumps({"next_eligible_at": stamp.isoformat()})}
    value, wait = auto._next_eligible(progress)
    assert value == stamp.isoformat()
    assert wait is not None and wait <= 0.1


def test_run_one_uses_existing_runner_and_excludes_quarantined_date(monkeypatch):
    observed = {}
    monkeypatch.setattr(auto.subprocess, "run", lambda command, **kwargs: (observed.update(command=command, kwargs=kwargs) or type("Result", (), {"returncode": 0})()))
    assert auto._run_one(["2026-09-22"]) == 0
    command = observed["command"]
    assert command[command.index("--max-dates") + 1] == "1"
    assert command[command.index("--exclude-date") + 1] == "2026-09-22"
