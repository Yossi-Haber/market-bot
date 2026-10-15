"""NYSE session eligibility in America/New_York, including early closes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

NEW_YORK = ZoneInfo("America/New_York")
NYSE = mcal.get_calendar("NYSE")


class MarketCalendarError(RuntimeError):
    """A session date or eligibility decision is invalid or ambiguous."""


@dataclass(frozen=True)
class Session:
    market_date: str
    opens_at: datetime
    closes_at: datetime


def get_session(day: date | str) -> Session | None:
    """Return NYSE regular session bounds, or None for a closed date."""
    session_day = date.fromisoformat(day) if isinstance(day, str) else day
    return _cached_session(session_day.isoformat())


@lru_cache(maxsize=2048)
def _cached_session(day_key: str) -> Session | None:
    session_day = date.fromisoformat(day_key)
    schedule = _year_schedule(session_day.year)
    try:
        row = schedule.loc[day_key]
    except KeyError:
        return None
    if getattr(row, "empty", False):
        return None
    opened = row["market_open"].to_pydatetime().astimezone(NEW_YORK)
    closed = row["market_close"].to_pydatetime().astimezone(NEW_YORK)
    return Session(day_key, opened, closed)


@lru_cache(maxsize=8)
def _year_schedule(year: int):
    return NYSE.schedule(start_date=f"{year:04d}-01-01", end_date=f"{year:04d}-12-31")


def is_completed_session(day: date | str, *, now: datetime) -> bool:
    """Whether the specified NYSE session has closed by the supplied instant."""
    if now.tzinfo is None:
        raise MarketCalendarError("MARKET_CALENDAR_CLOCK_TIMEZONE_REQUIRED")
    session = get_session(day)
    return bool(session and now.astimezone(timezone.utc) >= session.closes_at.astimezone(timezone.utc))


def resolve_market_date(*, now: datetime, explicit_date: str | None = None, completed_dates: set[str] | None = None, start_date: str | None = None, lookback_sessions: int = 10) -> str:
    """Resolve one completed session and refuse silent missed-session skipping.

    Explicit manual dates must be genuine, already-closed NYSE sessions. For an
    automatic delayed start, the most recent eligible session is used only when
    no older unprocessed session exists in the bounded lookback window.
    """
    if now.tzinfo is None:
        raise MarketCalendarError("MARKET_CALENDAR_CLOCK_TIMEZONE_REQUIRED")
    instant = now.astimezone(timezone.utc)
    if explicit_date:
        if start_date and explicit_date < start_date:
            raise MarketCalendarError("MARKET_DATE_PRECEDES_STATE_GENESIS")
        if not is_completed_session(explicit_date, now=instant):
            raise MarketCalendarError("MARKET_DATE_NOT_A_COMPLETED_NYSE_SESSION")
        return explicit_date

    completed = completed_dates or set()
    local_day = instant.astimezone(NEW_YORK).date()
    scan_start = local_day - timedelta(days=max(30, lookback_sessions * 3))
    schedule = NYSE.schedule(start_date=scan_start.isoformat(), end_date=local_day.isoformat())
    sessions: list[Session] = []
    for label, row in schedule.iterrows():
        opened = row["market_open"].to_pydatetime().astimezone(NEW_YORK)
        closed = row["market_close"].to_pydatetime().astimezone(NEW_YORK)
        sessions.append(Session(label.date().isoformat(), opened, closed))
    eligible = [item for item in sessions if item.closes_at.astimezone(timezone.utc) <= instant and (not start_date or item.market_date >= start_date)]
    if not eligible:
        raise MarketCalendarError("NO_COMPLETED_NYSE_SESSION_AVAILABLE")
    candidates = [item for item in eligible if item.market_date not in completed]
    if not candidates:
        raise MarketCalendarError("LATEST_COMPLETED_SESSION_ALREADY_CLAIMED")
    recent = eligible[-lookback_sessions:]
    unprocessed = [item for item in recent if item.market_date not in completed]
    if len(unprocessed) > 1:
        raise MarketCalendarError("MISSED_MARKET_SESSION_REQUIRES_REVIEW")
    return unprocessed[0].market_date


def calendar_version() -> str:
    """Expose the pinned calendar package version in diagnostics."""
    return str(getattr(mcal, "__version__", "unknown"))
