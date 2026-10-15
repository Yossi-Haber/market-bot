"""Credit-aware pacing for the Twelve Data Basic plan."""

from __future__ import annotations

import logging
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class RateLimitWaitTimeout(RuntimeError):
    """Raised when a quota wait exceeds its bounded deadline."""


class QuotaPauseRequired(Exception):
    """Raised by evaluation runs instead of blocking for an operationally long wait."""

    def __init__(self, expected_wait: float, next_eligible_at: str, context: dict[str, str] | None = None) -> None:
        super().__init__("TWELVE_DATA_QUOTA_PAUSED")
        self.provider = "Twelve Data"
        self.required_wait_seconds = float(expected_wait)
        self.next_eligible_at = next_eligible_at
        self.context = context or {}


@dataclass(frozen=True)
class TwelveDataPlanLimits:
    """Known Basic-plan limits for one-credit time-series requests."""

    minute_quota: int = 8
    daily_quota: int = 800
    credits_per_request: int = 1
    safety_buffer_seconds: float = 1.5


class CreditRateLimitController:
    """Pause before requests when Twelve Data credits are exhausted."""

    def __init__(
        self,
        limits: TwelveDataPlanLimits | None = None,
        *,
        monotonic_fn: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        state_path: Path | None = None,
        max_blocking_wait_seconds: float | None = None,
        request_recorder: Callable[[dict[str, Any]], None] | None = None,
        reservation_callback: Callable[[int], Any] | None = None,
        initial_daily_requests: int = 0,
    ) -> None:
        self.limits = limits or TwelveDataPlanLimits()
        self._monotonic = monotonic_fn or time.monotonic
        self._sleep = sleep_fn or time.sleep
        self.credits_left: int | None = None
        self.credits_used: int | None = None
        self._minute_key = int(time.time() // 60)
        self._requests_in_minute = 0
        self.daily_requests = max(0, int(initial_daily_requests))
        self.rate_limit_waits = 0
        self.state_path = Path(state_path) if state_path else None
        self.max_blocking_wait_seconds = max_blocking_wait_seconds
        self.next_eligible_at: str | None = None
        self.request_recorder = request_recorder
        self.reservation_callback = reservation_callback
        self.pending_request_context: dict[str, str] = {}
        self._load_state()

    def before_request(self, logger: logging.Logger, credits_required: int = 1) -> bool:
        """Wait for a new credit window if headers or local limits require it."""
        if self.daily_requests + credits_required > self.limits.daily_quota:
            raise RuntimeError("Twelve Data daily credit quota exhausted")

        current_minute = int(time.time() // 60)
        if current_minute != self._minute_key:
            self._minute_key = current_minute
            self._requests_in_minute = 0

        logger.info("Credits remaining before next request: %s", self.credits_left if self.credits_left is not None else "unknown")

        waited = False
        if (self.credits_left is not None and self.credits_left < credits_required) or (
            self.credits_left is None and self._requests_in_minute + credits_required > self.limits.minute_quota
        ):
            self._wait_for_next_window(logger)
            waited = True

        # A cloud reservation is published before the HTTP client is allowed
        # to proceed. Any exception here is a hard stop; a consumed-but-unused
        # reservation is intentionally conservative after process failure.
        if self.reservation_callback is not None:
            try:
                self.reservation_callback(int(credits_required))
            except Exception as exc:
                logger.error("DURABLE_QUOTA_RESERVATION_REJECTED error_class=%s", type(exc).__name__)
                raise
        self._requests_in_minute += credits_required
        self.daily_requests += credits_required
        self._persist_state()
        return waited

    def set_pending_request(self, *, market_date: str | None, symbol: str, purpose: str) -> None:
        """Attach the in-progress component to any operational quota pause."""
        self.pending_request_context = {"market_date": market_date or "", "symbol": symbol, "purpose": purpose}

    def record_response(self, response: object, logger: logging.Logger) -> None:
        """Read and safely log Twelve Data credit headers from any HTTP response."""
        headers = getattr(response, "headers", {})
        used = self._header_int(headers, "api-credits-used")
        left = self._header_int(headers, "api-credits-left")
        if used is not None:
            self.credits_used = used
        if left is not None:
            self.credits_left = left
        if used is not None or left is not None:
            logger.info("Twelve Data credits: used=%s left=%s", used if used is not None else "unknown", left if left is not None else "unknown")
        self._persist_state()

    def handle_rate_limit(self, logger: logging.Logger) -> None:
        """Wait once after a rate-limit response before a caller retries."""
        logger.warning("Minute quota exhausted; preparing one rate-limit retry")
        self._wait_for_next_window(logger)

    def _wait_for_next_window(self, logger: logging.Logger) -> None:
        # Wall time identifies the provider's minute boundary; monotonic time
        # measures the duration so clock adjustments cannot extend the wait.
        now_wall = time.time()
        next_boundary = (int(now_wall // 60) + 1) * 60
        eligible_wall = next_boundary + self.limits.safety_buffer_seconds
        persisted_eligible = self._parse_timestamp(self.next_eligible_at)
        if persisted_eligible is not None and now_wall >= persisted_eligible:
            self._minute_key = int(now_wall // 60)
            self._requests_in_minute = 0
            self.credits_left = None
            self.next_eligible_at = None
            self._persist_state()
            logger.info("RATE_PERSISTED_WINDOW_ALREADY_ELIGIBLE")
            return
        expected_wait = max(0.0, (persisted_eligible if persisted_eligible is not None else eligible_wall) - now_wall)
        if self.max_blocking_wait_seconds is not None and expected_wait > self.max_blocking_wait_seconds:
            next_eligible_at = datetime.fromtimestamp(now_wall + expected_wait, timezone.utc).isoformat()
            self.next_eligible_at = next_eligible_at
            self._persist_state()
            raise QuotaPauseRequired(expected_wait, next_eligible_at, self.pending_request_context)
        if self.state_path and expected_wait > 0:
            self.next_eligible_at = datetime.fromtimestamp(now_wall + expected_wait, timezone.utc).isoformat()
            self._persist_state()
        self._wait_once(logger, expected_wait)

    def _wait_once(self, logger: logging.Logger, expected_wait: float) -> None:
        """Perform exactly one bounded sleep and reset the local window."""
        started = self._monotonic()
        maximum_wait = expected_wait + 10.0
        self.rate_limit_waits += 1
        logger.info("RATE_WAIT_START expected=%.1f", expected_wait)
        logger.info("RATE_SLEEP_START")
        self._sleep(expected_wait)
        elapsed = self._monotonic() - started
        logger.info("RATE_SLEEP_RETURNED actual=%.1f", elapsed)
        if elapsed > maximum_wait:
            logger.error(
                "RATE_LIMIT_WAIT_TIMEOUT expected=%.1f elapsed=%.1f maximum=%.1f",
                expected_wait,
                elapsed,
                maximum_wait,
            )
            if self.state_path and self.max_blocking_wait_seconds is not None:
                eligible_at = datetime.now(timezone.utc).isoformat()
                self.next_eligible_at = eligible_at
                self._persist_state()
                raise QuotaPauseRequired(expected_wait, eligible_at, self.pending_request_context)
            raise RateLimitWaitTimeout("quota wait exceeded its hard deadline")
        self._minute_key = int(time.time() // 60)
        self._requests_in_minute = 0
        self.credits_left = None
        self.next_eligible_at = None
        self._persist_state()
        logger.info("RATE_WINDOW_RESET")
        logger.info("RATE_WAIT_RETURN elapsed=%.1f", elapsed)

    def debug_wait(self, seconds: float, logger: logging.Logger) -> None:
        """Exercise the bounded wait lifecycle without making a provider request."""
        if seconds < 0:
            raise ValueError("debug wait must be non-negative")
        self._wait_once(logger, float(seconds))

    def request_started(self, *, market_date: str | None, symbol: str, purpose: str, credits: int) -> str | None:
        """Record a real outbound request when an evaluation ledger is configured."""
        if not self.request_recorder:
            return None
        import uuid
        request_id = uuid.uuid4().hex
        self.request_recorder({
            "event": "START",
            "request_id": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "provider": "Twelve Data",
            "historical_date": market_date or "",
            "symbol": symbol,
            "purpose": purpose,
            "credits": int(credits),
            "result": "IN_FLIGHT",
        })
        return request_id

    def request_finished(self, request_id: str | None, result: str) -> None:
        """Finalize a request-ledger record without affecting provider behavior."""
        if self.request_recorder and request_id:
            self.request_recorder({"event": "FINISH", "request_id": request_id, "result": result})

    def _load_state(self) -> None:
        """Restore runner-only quota state when a persisted state file is configured."""
        if not self.state_path or not self.state_path.is_file():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.credits_left = payload.get("credits_left")
            self.credits_used = payload.get("credits_used")
            self._minute_key = int(payload.get("minute_key", self._minute_key))
            self._requests_in_minute = int(payload.get("requests_in_minute", 0))
            self.daily_requests = int(payload.get("daily_requests", 0))
            self.rate_limit_waits = int(payload.get("rate_limit_waits", 0))
            self.next_eligible_at = payload.get("next_eligible_at")
            stored_day = payload.get("quota_day_utc")
            current_day = datetime.now(timezone.utc).date().isoformat()
            if stored_day and stored_day != current_day:
                self.daily_requests = 0
                self._minute_key = int(time.time() // 60)
                self._requests_in_minute = 0
                self.credits_left = None
                self.next_eligible_at = None
        except (OSError, ValueError, TypeError):
            # A malformed operational checkpoint must fail safe: retain this
            # process's fresh counters instead of weakening provider limits.
            self.daily_requests = self.limits.daily_quota

    def _persist_state(self) -> None:
        if not self.state_path:
            return
        payload = {
            "provider": "Twelve Data",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "quota_day_utc": datetime.now(timezone.utc).date().isoformat(),
            "minute_key": self._minute_key,
            "requests_in_minute": self._requests_in_minute,
            "daily_requests": self.daily_requests,
            "credits_left": self.credits_left,
            "credits_used": self.credits_used,
            "rate_limit_waits": self.rate_limit_waits,
            "next_eligible_at": self.next_eligible_at,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.state_path)

    @staticmethod
    def _parse_timestamp(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value).timestamp()
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _header_int(headers: object, name: str) -> int | None:
        try:
            value = headers.get(name)  # type: ignore[union-attr]
        except AttributeError:
            return None
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
