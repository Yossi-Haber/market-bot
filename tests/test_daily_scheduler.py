from datetime import date, datetime, timezone

import run_daily_scheduled as scheduler


def test_new_york_timezone_and_dst_conversion():
    before = datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc).astimezone(scheduler.ET)
    after = datetime(2026, 3, 8, 7, 1, tzinfo=timezone.utc).astimezone(scheduler.ET)
    assert before.hour == 1 and before.utcoffset().total_seconds() == -5 * 3600
    assert after.hour == 3 and after.utcoffset().total_seconds() == -4 * 3600


def test_weekend_and_standard_market_holiday():
    assert scheduler.classify_day(date(2026, 10, 10)) == (False, "WEEKEND")
    assert scheduler.classify_day(date(2026, 7, 3)) == (False, "US_MARKET_HOLIDAY:Independence Day")


def test_close_and_schedule_time_guards():
    before_close = datetime(2026, 10, 6, 19, 59, tzinfo=timezone.utc)  # 15:59 ET
    after_close_before_schedule = datetime(2026, 10, 6, 20, 30, tzinfo=timezone.utc)  # 16:30 ET
    eligible = datetime(2026, 10, 6, 21, 16, tzinfo=timezone.utc)  # 17:16 ET
    assert scheduler.decision(before_close, completed=False)["reason"] == "BEFORE_MARKET_CLOSE"
    assert scheduler.decision(after_close_before_schedule, completed=False)["reason"] == "BEFORE_SCHEDULE_TIME"
    assert scheduler.decision(eligible, completed=False)["eligible"] is True


def test_completed_success_blocks_duplicate_but_failure_can_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "STATE_ROOT", tmp_path)
    (tmp_path / "2026-10-06.json").write_text(
        '{"status":"SUCCESS","market_date":"2026-10-06","report_exists":true,"telegram_confirmed":true,"telegram_status":"SENT"}',
        encoding="utf-8",
    )
    assert scheduler.completed_for(date(2026, 10, 6)) is True
    (tmp_path / "2026-10-06.json").write_text('{"status":"FAILED","market_date":"2026-10-06"}', encoding="utf-8")
    assert scheduler.completed_for(date(2026, 10, 6)) is False


def test_existing_status_requires_delivery_or_sent_flag(tmp_path, monkeypatch):
    status = tmp_path / "status.json"
    delivery = tmp_path / "delivery.json"
    report = tmp_path / "report.png"
    report_data = tmp_path / "report.json"
    validation = tmp_path / "validation.json"
    monkeypatch.setattr(scheduler, "STATUS_PATH", status)
    monkeypatch.setattr(scheduler, "DELIVERY_PATH", delivery)
    monkeypatch.setattr(scheduler, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(scheduler, "REPORT_PATH", report)
    monkeypatch.setattr(scheduler, "REPORT_DATA_PATH", report_data)
    monkeypatch.setattr(scheduler, "VISUAL_VALIDATION_PATH", validation)
    status.write_text('{"status":"SUCCESS","market_date":"2026-10-06","telegram_sent":true,"telegram_status":"SENT"}', encoding="utf-8")
    assert not scheduler.completed_for(date(2026, 10, 6))
    delivery.write_text('{"status":"SENT","report_date":"2026-10-06"}', encoding="utf-8")
    report.write_bytes(b"png")
    report_data.write_text('{"market_date":"2026-10-06"}', encoding="utf-8")
    validation.write_text('{"valid":true}', encoding="utf-8")
    assert scheduler.completed_for(date(2026, 10, 6))


def test_dry_run_never_launches_pipeline(tmp_path, monkeypatch, capsys):
    moment = datetime(2026, 10, 6, 21, 16, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(scheduler, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(scheduler, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(scheduler, "DELIVERY_PATH", tmp_path / "delivery.json")
    monkeypatch.setattr(scheduler.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("pipeline launched")))
    assert scheduler.main(["--dry-run"], now=moment) == 0
    output = capsys.readouterr().out
    assert "Pipeline command (not launched):" in output
    assert "2026-10-06" in output


def test_dry_run_before_schedule_reports_no_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scheduler, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(scheduler, "STATE_ROOT", tmp_path / "state")
    moment = datetime(2026, 10, 6, 20, 30, tzinfo=timezone.utc)
    assert scheduler.main(["--dry-run"], now=moment) == 0
    output = capsys.readouterr().out
    assert "Pipeline command (not launched):" in output
    assert "Would execute now: False" in output
