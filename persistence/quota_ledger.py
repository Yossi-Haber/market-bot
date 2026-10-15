"""Conservative durable Twelve Data request reservations."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from collectors.rate_limit import TwelveDataPlanLimits
from persistence.github_state import GitHubStateStore


class QuotaUnavailable(RuntimeError):
    """Reservation could not be safely published; the provider call must not run."""


class QuotaExhausted(RuntimeError):
    """The durable minute or daily allowance is exhausted."""


class QuotaLedger:
    """Push a durable charge before every actual outbound request attempt."""

    def __init__(self, store: GitHubStateStore, limits: TwelveDataPlanLimits | None = None, *, clock=lambda: datetime.now(timezone.utc)) -> None:
        self.store = store
        self.limits = limits or TwelveDataPlanLimits()
        self.clock = clock

    def reserve(self, credits: int = 1) -> str:
        """Publish one reservation and return its opaque identifier.

        Reservations remain charged after a crash, timeout, HTTP failure, or
        ambiguous result. This deliberately prefers under-use over overspend.
        """
        if not isinstance(credits, int) or credits <= 0:
            raise ValueError("QUOTA_CREDITS_INVALID")
        now = self.clock()
        if now.tzinfo is None:
            raise QuotaUnavailable("QUOTA_CLOCK_TIMEZONE_REQUIRED")
        now = now.astimezone(timezone.utc)
        day_key = now.date().isoformat()
        minute_key = now.strftime("%Y-%m-%dT%H:%MZ")
        reservation_id = uuid.uuid4().hex

        def mutate(state: dict[str, Any]) -> None:
            quota = state["quota"]
            previous_at = quota.get("last_reservation_at")
            if previous_at:
                try:
                    previous = datetime.fromisoformat(previous_at).astimezone(timezone.utc)
                except (TypeError, ValueError):
                    raise QuotaUnavailable("QUOTA_LAST_TIMESTAMP_INVALID") from None
                if now < previous:
                    raise QuotaUnavailable("QUOTA_CLOCK_ROLLBACK_DETECTED")
            daily = quota["daily"]
            minute = quota["minute"]
            used_day = int(daily.get(day_key, 0))
            used_minute = int(minute.get(minute_key, 0))
            if used_day + credits > self.limits.daily_quota:
                raise QuotaExhausted("TWELVE_DATA_DAILY_QUOTA_EXHAUSTED")
            if used_minute + credits > self.limits.minute_quota:
                raise QuotaExhausted("TWELVE_DATA_MINUTE_QUOTA_EXHAUSTED")
            daily[day_key] = used_day + credits
            minute[minute_key] = used_minute + credits
            quota["reservations"].append({
                "reservation_id": reservation_id,
                "reserved_at": now.isoformat(),
                "utc_day": day_key,
                "utc_minute": minute_key,
                "credits": credits,
                "outcome": "RESERVED_CONSERVATIVELY",
            })
            quota["last_reservation_at"] = now.isoformat()
            quota["reservations"] = quota["reservations"][-500:]
            oldest_day = (now.date() - timedelta(days=2)).isoformat()
            quota["daily"] = {key: value for key, value in daily.items() if key >= oldest_day}
            oldest_minute = now.timestamp() - 180
            quota["minute"] = {
                key: value for key, value in minute.items()
                if _minute_timestamp(key) is not None and _minute_timestamp(key) >= oldest_minute
            }
        try:
            self.store.update(mutate)
        except QuotaExhausted:
            raise
        except QuotaUnavailable:
            raise
        except Exception as exc:
            raise QuotaUnavailable("QUOTA_RESERVATION_NOT_PUBLISHED; provider call blocked") from exc
        return reservation_id

    def __call__(self, credits: int = 1) -> str:
        """Allow direct use as a rate-limiter reservation callback."""
        return self.reserve(credits)


def _minute_timestamp(value: str) -> float | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
