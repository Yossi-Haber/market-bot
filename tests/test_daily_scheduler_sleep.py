import json
import logging
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

import run_daily_scheduled as scheduler


def test_sleep_requires_complete_successful_delivery_and_persisted_state():
    allowed, why = scheduler.post_report_sleep_eligible(
        pipeline_success=True, report_exists=True, telegram_status="SENT",
        completion_persisted=True, retry_pending=False, dry_run=False,
    )
    assert allowed and why == "ELIGIBLE"


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({"telegram_status": "FAILED"}, "TELEGRAM_NOT_CONFIRMED_SENT"),
        ({"telegram_status": "UNKNOWN"}, "TELEGRAM_NOT_CONFIRMED_SENT"),
        ({"pipeline_success": False}, "PIPELINE_NOT_SUCCESSFUL"),
        ({"report_exists": False}, "REPORT_ARTIFACT_MISSING"),
        ({"retry_pending": True}, "RETRY_STATE_NOT_CONFIRMED_CLEAR"),
        ({"retry_pending": None}, "RETRY_STATE_NOT_CONFIRMED_CLEAR"),
        ({"dry_run": True}, "DRY_RUN"),
        ({"trading_day": False}, "NON_TRADING_DAY"),
        ({"completion_persisted": False}, "COMPLETION_NOT_PERSISTED"),
    ],
)
def test_sleep_is_fail_closed(changes, expected):
    conditions = dict(
        pipeline_success=True, report_exists=True, telegram_status="SENT",
        completion_persisted=True, retry_pending=False, dry_run=False,
    )
    conditions.update(changes)
    allowed, why = scheduler.post_report_sleep_eligible(**conditions)
    assert not allowed
    assert why == expected


def _pipeline_fixture(tmp_path, monkeypatch, *, exit_code=0, daily_status=None, delivery=None, report=True):
    output = tmp_path / "output"
    logs = tmp_path / "logs"
    state = logs / "state"
    output.mkdir()
    logs.mkdir()
    monkeypatch.setattr(scheduler, "ROOT", tmp_path)
    monkeypatch.setattr(scheduler, "OUTPUT", output)
    monkeypatch.setattr(scheduler, "LOG_ROOT", logs)
    monkeypatch.setattr(scheduler, "STATE_ROOT", state)
    monkeypatch.setattr(scheduler, "STATUS_PATH", output / "daily_run_status.json")
    monkeypatch.setattr(scheduler, "DELIVERY_PATH", output / "daily_report_delivery.json")
    monkeypatch.setattr(scheduler, "REPORT_PATH", output / "daily_report_card_v3.png")
    monkeypatch.setattr(scheduler, "REPORT_DATA_PATH", output / "daily_report_data.json")
    monkeypatch.setattr(scheduler, "VISUAL_VALIDATION_PATH", output / "daily_report_v3_visual_validation.json")
    day_key = "2026-10-07"
    (output / "daily_run_status.json").write_text(json.dumps(daily_status or {
        "status": "SUCCESS", "market_date": day_key, "telegram_sent": True, "telegram_status": "SENT",
    }), encoding="utf-8")
    (output / "daily_report_delivery.json").write_text(json.dumps(delivery or {
        "status": "SENT", "report_date": day_key,
    }), encoding="utf-8")
    (output / "daily_report_data.json").write_text(json.dumps({"market_date": day_key}), encoding="utf-8")
    (output / "daily_report_v3_visual_validation.json").write_text(json.dumps({"valid": True}), encoding="utf-8")
    if report:
        (output / "daily_report_card_v3.png").write_bytes(b"fixture")
    monkeypatch.setattr(scheduler.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=exit_code))
    logger = logging.getLogger(f"scheduler-test-{tmp_path.name}")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return day_key, logger, output, state


def test_success_persists_completion_before_sleep(tmp_path, monkeypatch):
    day, logger, _, state = _pipeline_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("MARKETBOT_SLEEP_AFTER_REPORT", "true")
    calls = []
    monkeypatch.setattr(scheduler, "maybe_sleep_after_report", lambda *args, **kwargs: calls.append(kwargs) or True)
    assert scheduler._run_pipeline(day, logger) == 0
    completion = json.loads((state / f"{day}.json").read_text(encoding="utf-8"))
    assert completion["status"] == "SUCCESS"
    assert completion["telegram_confirmed"] is True
    assert completion["report_exists"] is True
    assert completion["telegram_status"] == "SENT"
    assert calls == [{"delay_seconds": 60}]


@pytest.mark.parametrize(
    "exit_code,status,delivery,report,expected_code",
    [
        (3, {"status": "FAILED", "market_date": "2026-10-07", "telegram_sent": False, "telegram_status": "FAILED"}, {"status": "FAILED", "report_date": "2026-10-07"}, True, 3),
        (0, {"status": "SUCCESS", "market_date": "2026-10-07", "telegram_sent": False, "telegram_status": "UNKNOWN"}, {"status": "UNKNOWN", "report_date": "2026-10-07"}, True, 1),
        (1, {"status": "FAILED", "market_date": "2026-10-07", "telegram_sent": False}, {"status": "FAILED", "report_date": "2026-10-07"}, True, 1),
        (0, {"status": "SUCCESS", "market_date": "2026-10-07", "telegram_sent": True, "telegram_status": "SENT"}, {"status": "SENT", "report_date": "2026-10-07"}, False, 1),
    ],
)
def test_failed_or_unverified_run_never_sleeps(tmp_path, monkeypatch, exit_code, status, delivery, report, expected_code):
    day, logger, _, state = _pipeline_fixture(
        tmp_path, monkeypatch, exit_code=exit_code, daily_status=status, delivery=delivery, report=report,
    )
    monkeypatch.setenv("MARKETBOT_SLEEP_AFTER_REPORT", "true")
    monkeypatch.setattr(scheduler, "maybe_sleep_after_report", lambda *args, **kwargs: pytest.fail("sleep called"))
    assert scheduler._run_pipeline(day, logger) == expected_code
    assert not (state / f"{day}.json").exists()


def test_already_completed_requires_report_and_delivery_match(tmp_path, monkeypatch):
    day, _, output, state = _pipeline_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(scheduler, "STATE_ROOT", state)
    assert scheduler.completed_for(date.fromisoformat(day)) is True
    (output / "daily_report_delivery.json").write_text('{"status":"SENT","report_date":"2026-10-06"}', encoding="utf-8")
    assert scheduler.completed_for(date.fromisoformat(day)) is False


def test_already_completed_invocation_never_relaunches_pipeline(tmp_path, monkeypatch):
    day, _, _, state = _pipeline_fixture(tmp_path, monkeypatch)
    (state / f"{day}.json").parent.mkdir(parents=True, exist_ok=True)
    (state / f"{day}.json").write_text(json.dumps({
        "status": "SUCCESS", "market_date": day, "report_exists": True,
        "telegram_confirmed": True, "telegram_status": "SENT",
    }), encoding="utf-8")
    monkeypatch.setattr(scheduler, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setenv("MARKETBOT_SLEEP_AFTER_REPORT", "true")
    monkeypatch.setattr(scheduler.subprocess, "run", lambda *a, **k: pytest.fail("duplicate pipeline launched"))
    sleep_calls = []
    monkeypatch.setattr(scheduler, "maybe_sleep_after_report", lambda *a, **k: sleep_calls.append(True) or True)
    # 17:16 New York on the test report date.
    moment = datetime(2026, 10, 7, 21, 16, tzinfo=timezone.utc)
    assert scheduler.main([], now=moment) == 0
    assert sleep_calls == [True]


def test_dry_run_never_requests_sleep_even_when_enabled(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scheduler, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(scheduler, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setenv("MARKETBOT_SLEEP_AFTER_REPORT", "true")
    monkeypatch.setattr(scheduler, "maybe_sleep_after_report", lambda *a, **k: pytest.fail("sleep called in dry-run"))
    moment = datetime(2026, 10, 7, 21, 16, tzinfo=timezone.utc)
    assert scheduler.main(["--dry-run"], now=moment) == 0
    assert "Sleep invoked: False" in capsys.readouterr().out


def test_sleep_request_failure_is_logged_without_crashing(monkeypatch, caplog):
    monkeypatch.setattr(scheduler, "battery_warning", lambda logger: None)
    monkeypatch.setattr(scheduler.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(scheduler, "request_system_sleep", lambda: (_ for _ in ()).throw(OSError("not supported")))
    logger = logging.getLogger("sleep-failure-test")
    with caplog.at_level(logging.ERROR, logger="sleep-failure-test"):
        assert scheduler.maybe_sleep_after_report(logger, delay_seconds=0) is False
    assert "SLEEP_REQUEST_FAILED" in caplog.text


def test_config_is_opt_in_and_invalid_delay_disables_sleep():
    assert scheduler.sleep_configuration({}) == (False, 60)
    assert scheduler.sleep_configuration({"MARKETBOT_SLEEP_AFTER_REPORT": "true", "MARKETBOT_SLEEP_DELAY_SECONDS": "5"}) == (True, 5)
    assert scheduler.sleep_configuration({"MARKETBOT_SLEEP_AFTER_REPORT": "true", "MARKETBOT_SLEEP_DELAY_SECONDS": "-1"}) == (False, 60)


def test_simulation_exercises_decisions_without_sleep(monkeypatch, capsys):
    monkeypatch.setattr(scheduler, "request_system_sleep", lambda: pytest.fail("real sleep requested"))
    monkeypatch.setattr(scheduler.subprocess, "run", lambda *a, **k: pytest.fail("pipeline started"))
    assert scheduler.main(["--simulate-post-report"], now=datetime.now(timezone.utc)) == 0
    text = capsys.readouterr().out
    assert "successful_report_and_telegram: sleep_eligible=True" in text
    assert "retry_pending: sleep_eligible=False" in text
    assert "sleep_invoked=False" in text
