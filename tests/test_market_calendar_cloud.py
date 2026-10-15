from datetime import date, datetime, timezone

import pytest

from market_calendar import MarketCalendarError, get_session, is_completed_session, resolve_market_date


def test_weekends_regular_and_exceptional_closures_are_not_sessions():
    for day in ("2026-10-17", "2026-07-03", "2001-09-11", "2018-12-05"):
        assert get_session(day) is None
    assert get_session("2026-10-16") is not None


def test_nyse_early_close_is_used_for_date_eligibility():
    session = get_session("2026-11-27")
    assert session is not None
    assert session.closes_at.hour == 13 and session.closes_at.minute == 0
    assert is_completed_session("2026-11-27", now=datetime(2026, 11, 27, 13, 1, tzinfo=session.closes_at.tzinfo))
    assert not is_completed_session("2026-11-27", now=datetime(2026, 11, 27, 12, 59, tzinfo=session.closes_at.tzinfo))


def test_eastern_dst_changes_utc_close_not_local_close():
    before = get_session("2026-03-06")
    after = get_session("2026-03-09")
    assert before and after
    assert before.closes_at.hour == after.closes_at.hour == 16
    assert before.closes_at.astimezone(timezone.utc).hour == 21
    assert after.closes_at.astimezone(timezone.utc).hour == 20


def test_delayed_start_after_weekend_resolves_friday_once_prior_days_complete():
    completed = {"2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"}
    now = datetime(2026, 10, 17, 12, 0, tzinfo=get_session("2026-10-16").closes_at.tzinfo)
    assert resolve_market_date(now=now, completed_dates=completed, start_date="2026-10-12") == "2026-10-16"


def test_manual_date_requires_completed_session_and_honors_state_genesis():
    with pytest.raises(MarketCalendarError, match="COMPLETED_NYSE_SESSION"):
        resolve_market_date(now=datetime(2026, 10, 14, 12, tzinfo=timezone.utc), explicit_date="2026-10-14")
    with pytest.raises(MarketCalendarError, match="PRECEDES_STATE_GENESIS"):
        resolve_market_date(now=datetime(2026, 10, 14, 23, tzinfo=timezone.utc), explicit_date="2026-10-13", start_date="2026-10-14")


def test_missed_sessions_require_explicit_operator_review():
    now = datetime(2026, 10, 14, 22, tzinfo=timezone.utc)
    with pytest.raises(MarketCalendarError, match="MISSED_MARKET_SESSION_REQUIRES_REVIEW"):
        resolve_market_date(now=now, start_date="2026-10-12")


def test_cloud_adapter_calendar_check_is_offline_and_emits_manifest(tmp_path, monkeypatch):
    import run_daily_cloud

    monkeypatch.setattr(run_daily_cloud, "MANIFEST_DIR", tmp_path)
    monkeypatch.setattr(run_daily_cloud, "OUTPUT_DIR", tmp_path)
    result = run_daily_cloud.run(["--calendar-check", "--market-date", "2026-10-13"], now=datetime(2026, 10, 14, 12, tzinfo=timezone.utc))
    assert result == 0
    data = (tmp_path / "latest.json").read_text(encoding="utf-8")
    assert '"provider_calls": 0' in data
    assert '"telegram_sends": 0' in data


def test_cloud_execute_refuses_until_windows_and_quota_ownership_are_exclusive(state_repos, tmp_path, monkeypatch):
    import run_daily_cloud

    _, repo, _ = state_repos
    from persistence.github_state import GitHubStateStore
    GitHubStateStore(repo).update(lambda state: state.update({"created_at": "2026-10-13T00:00:00+00:00"}))
    monkeypatch.setenv("MARKETBOT_STATE_REPO", str(repo))
    monkeypatch.setenv("MARKETBOT_CLOUD_LIVE_ENABLED", "1")
    monkeypatch.setattr(run_daily_cloud, "MANIFEST_DIR", tmp_path)
    monkeypatch.setattr(run_daily_cloud, "OUTPUT_DIR", tmp_path)
    result = run_daily_cloud.run(["--execute", "--market-date", "2026-10-14"], now=datetime(2026, 10, 14, 21, tzinfo=timezone.utc))
    assert result == 1
    assert "CLOUD_OWNERSHIP_NOT_EXCLUSIVE" in (tmp_path / "latest.json").read_text(encoding="utf-8")
    assert GitHubStateStore(repo).read(fetch=True)["runs"] == {}


def test_crashed_inflight_cloud_send_is_recovered_as_unknown_not_retried(state_repos, tmp_path, monkeypatch):
    import run_daily_cloud
    from persistence.github_state import GitHubStateStore

    _, repo, _ = state_repos
    store = GitHubStateStore(repo)
    started = "2026-10-14T15:20:00+00:00"
    slot = "hmac256:" + "a" * 64

    def seed(state):
        state["created_at"] = "2026-10-13T00:00:00+00:00"
        state["ownership"].update({"cloud_quota_exclusive": True, "windows_sender_disabled": True})
        state["runs"]["2026-10-14"] = {"status": "RUNNING", "run_id": "b" * 32, "started_at": started}
        state["deliveries"]["2026-10-14"] = {"recipients": {slot: {"status": "SENDING", "attempt": 1}}}

    store.update(seed)
    monkeypatch.setenv("MARKETBOT_STATE_REPO", str(repo))
    monkeypatch.setenv("MARKETBOT_CLOUD_LIVE_ENABLED", "1")
    monkeypatch.setattr(run_daily_cloud, "MANIFEST_DIR", tmp_path)
    monkeypatch.setattr(run_daily_cloud, "OUTPUT_DIR", tmp_path)
    result = run_daily_cloud.run(["--execute", "--market-date", "2026-10-14"], now=datetime(2026, 10, 14, 21, tzinfo=timezone.utc))
    assert result == 1
    recovered = GitHubStateStore(repo).read(fetch=True)
    assert recovered["runs"]["2026-10-14"]["status"] == "DELIVERY_UNKNOWN"
    assert recovered["deliveries"]["2026-10-14"]["recipients"][slot]["status"] == "UNKNOWN"
