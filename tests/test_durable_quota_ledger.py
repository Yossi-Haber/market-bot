from datetime import datetime, timezone
import logging

import pytest

from collectors.rate_limit import CreditRateLimitController, TwelveDataPlanLimits
from persistence.github_state import GitHubStateStore, StateUnavailable
from persistence.quota_ledger import QuotaExhausted, QuotaLedger, QuotaUnavailable
from tests.persistence_fixtures import git


def test_reservation_is_published_and_survives_runner_restart(state_repos):
    _, repo, _ = state_repos
    now = datetime(2026, 10, 14, 14, 5, tzinfo=timezone.utc)
    store = GitHubStateStore(repo)
    ledger = QuotaLedger(store, clock=lambda: now)
    reservation = ledger.reserve(credits=2)
    state = GitHubStateStore(repo).read(fetch=True)
    assert reservation
    assert state["quota"]["daily"]["2026-10-14"] == 2
    assert state["quota"]["minute"]["2026-10-14T14:05Z"] == 2
    assert state["quota"]["reservations"][0]["outcome"] == "RESERVED_CONSERVATIVELY"


def test_failed_and_ambiguous_requests_remain_charged(state_repos):
    _, repo, _ = state_repos
    now = datetime(2026, 10, 14, 14, 5, tzinfo=timezone.utc)
    limits = TwelveDataPlanLimits(minute_quota=2, daily_quota=3)
    ledger = QuotaLedger(GitHubStateStore(repo), limits, clock=lambda: now)
    ledger.reserve()
    # Simulate a runner crash after reservation but before recording a response.
    restarted = QuotaLedger(GitHubStateStore(repo), limits, clock=lambda: now)
    restarted.reserve()
    with pytest.raises(QuotaExhausted, match="MINUTE_QUOTA"):
        restarted.reserve()
    with pytest.raises(QuotaExhausted, match="MINUTE_QUOTA"):
        restarted.reserve()


def test_quota_window_and_day_rollover_are_utc_and_bounded(state_repos):
    _, repo, _ = state_repos
    limits = TwelveDataPlanLimits(minute_quota=1, daily_quota=2)
    first = datetime(2026, 10, 14, 23, 59, tzinfo=timezone.utc)
    ledger = QuotaLedger(GitHubStateStore(repo), limits, clock=lambda: first)
    ledger.reserve()
    next_minute = QuotaLedger(GitHubStateStore(repo), limits, clock=lambda: datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc))
    next_minute.reserve()
    state = GitHubStateStore(repo).read(fetch=True)
    assert state["quota"]["daily"] == {"2026-10-14": 1, "2026-10-15": 1}


def test_clock_rollback_fails_closed_without_resetting_quota(state_repos):
    _, repo, _ = state_repos
    ledger = QuotaLedger(GitHubStateStore(repo), clock=lambda: datetime(2026, 10, 14, 14, 5, tzinfo=timezone.utc))
    ledger.reserve()
    rolled_back = QuotaLedger(GitHubStateStore(repo), clock=lambda: datetime(2026, 10, 14, 14, 2, tzinfo=timezone.utc))
    with pytest.raises(QuotaUnavailable, match="CLOCK_ROLLBACK"):
        rolled_back.reserve()
    state = GitHubStateStore(repo).read(fetch=True)
    assert state["quota"]["daily"]["2026-10-14"] == 1


def test_limiter_never_allows_provider_call_when_reservation_fails(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr("collectors.market.requests.get", lambda *args, **kwargs: calls.append((args, kwargs)))
    limiter = CreditRateLimitController(reservation_callback=lambda credits: (_ for _ in ()).throw(QuotaUnavailable("STATE_PUSH_FAILED")))
    from collectors.market import TwelveDataClient
    from config import Instrument

    result = TwelveDataClient("synthetic-key", rate_limiter=limiter).fetch_instrument(Instrument("SPY", "Test", "core"), logging.getLogger("quota-test"))
    assert result["verified"] is False
    assert calls == []


def test_each_provider_retry_has_a_reservation_before_http(state_repos, monkeypatch):
    _, repo, _ = state_repos
    from collectors.market import TwelveDataClient
    from config import Instrument
    import requests

    now = datetime(2026, 10, 14, 14, 5, tzinfo=timezone.utc)
    ledger = QuotaLedger(GitHubStateStore(repo), TwelveDataPlanLimits(minute_quota=2, daily_quota=10), clock=lambda: now)
    limiter = CreditRateLimitController(
        limits=TwelveDataPlanLimits(minute_quota=2, daily_quota=10),
        reservation_callback=ledger.reserve,
    )
    calls = []

    def timeout_after_reservation(*args, **kwargs):
        state = GitHubStateStore(repo).read(fetch=True)
        assert state["quota"]["daily"]["2026-10-14"] == len(calls) + 1
        calls.append(True)
        raise requests.Timeout()

    monkeypatch.setattr("collectors.market.requests.get", timeout_after_reservation)
    result = TwelveDataClient("synthetic-key", rate_limiter=limiter).fetch_instrument(
        Instrument("SPY", "Test", "core"), logging.getLogger("quota-test")
    )
    assert result["verified"] is False
    assert len(calls) == 2
    assert GitHubStateStore(repo).read(fetch=True)["quota"]["daily"]["2026-10-14"] == 2


def test_local_limiter_without_callback_preserves_existing_behavior():
    limiter = CreditRateLimitController(limits=TwelveDataPlanLimits(minute_quota=1, daily_quota=2), sleep_fn=lambda _: None)
    limiter.before_request(logging.getLogger("quota-test"))
    assert limiter.daily_requests == 1
