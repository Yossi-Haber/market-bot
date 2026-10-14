"""Run-local budgets and circuit breakers for news acquisition."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests


NEWS_DISCOVERY_BUDGET_SECONDS = 60.0


class NewsBudgetExhausted(requests.RequestException):
    """Raised internally to prevent a new external request after the budget."""


class CoverageSubBudgetExhausted(NewsBudgetExhausted):
    """Raised when a request cannot start within the active coverage deadline."""


class ProviderCircuitOpen(requests.RequestException):
    """Raised internally when a provider has repeated network failures."""


@dataclass
class NewsPerformance:
    """Monotonic stage budget, request counters, and provider breakers."""

    logger: logging.Logger
    budget_seconds: float = NEWS_DISCOVERY_BUDGET_SECONDS
    clock: Callable[[], float] = time.monotonic
    started: float = field(init=False)
    timeouts: int = 0
    circuit_breakers_opened: int = 0
    _failures: dict[str, int] = field(default_factory=dict)
    _open: set[str] = field(default_factory=set)
    _budget_logged: bool = False

    def __post_init__(self) -> None:
        self.started = self.clock()

    @property
    def elapsed(self) -> float:
        return max(0.0, self.clock() - self.started)

    @property
    def exhausted(self) -> bool:
        exhausted = self.elapsed >= self.budget_seconds
        if exhausted and not self._budget_logged:
            self._budget_logged = True
            self.logger.warning("[NEWS] Discovery budget exhausted after %.1fs", self.elapsed)
        return exhausted

    def allow(self, provider: str) -> None:
        if self.exhausted:
            raise NewsBudgetExhausted("NEWS_DISCOVERY_BUDGET_EXHAUSTED")
        if provider in self._open:
            raise ProviderCircuitOpen(f"{provider.upper()}_CIRCUIT_OPEN")

    def record_failure(self, provider: str, exc: BaseException) -> None:
        is_timeout = isinstance(exc, requests.Timeout)
        if is_timeout:
            self.timeouts += 1
        if not isinstance(exc, (requests.Timeout, requests.ConnectionError)):
            return
        failures = self._failures.get(provider, 0) + 1
        self._failures[provider] = failures
        if failures >= 2 and provider not in self._open:
            self._open.add(provider)
            self.circuit_breakers_opened += 1
            self.logger.warning("[NEWS] Provider circuit opened: %s", provider)

    def request(self, provider: str, request_get: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Guard one provider request without changing its response parsing."""
        self.allow(provider)
        try:
            response = request_get(*args, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as exc:
            self.record_failure(provider, exc)
            raise
        self._failures[provider] = 0
        return response

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_news_seconds": round(self.elapsed, 3),
            "timeouts": self.timeouts,
            "circuit_breakers_opened": self.circuit_breakers_opened,
            "budget_seconds": self.budget_seconds,
            "budget_exhausted": self.exhausted,
            "open_providers": sorted(self._open),
        }


class ProviderRequest:
    """Callable adapter that attaches a provider name to an existing client."""

    def __init__(self, performance: NewsPerformance, provider: str, request_get: Callable[..., Any]) -> None:
        self.performance = performance
        self.provider = provider
        self.request_get = request_get
        self.deadline: float | None = None

    def set_deadline(self, deadline: float | None) -> None:
        """Set an optional monotonic deadline for subsequent requests."""
        self.deadline = deadline

    def remaining_seconds(self) -> float:
        """Return the smallest remaining shared/external budget."""
        now = self.performance.clock()
        remaining_shared = self.performance.budget_seconds - (now - self.performance.started)
        remaining_external = float("inf") if self.deadline is None else self.deadline - now
        return min(remaining_shared, remaining_external)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if "timeout" not in kwargs and self.deadline is None:
            return self.performance.request(self.provider, self.request_get, *args, **kwargs)
        remaining = self.remaining_seconds()
        if remaining <= 0:
            message = "COVERAGE_SUB_BUDGET_TIMEOUT" if self.deadline is not None else "NEWS_DISCOVERY_BUDGET_EXHAUSTED"
            raise CoverageSubBudgetExhausted(message) if self.deadline is not None else NewsBudgetExhausted(message)
        timeout = kwargs.get("timeout")
        if timeout is not None:
            if isinstance(timeout, tuple):
                kwargs["timeout"] = (min(float(timeout[0]), remaining), min(float(timeout[1]), remaining))
            else:
                kwargs["timeout"] = min(float(timeout), remaining)
        self.performance.logger.info("[NEWS PERF] provider=%s remaining_news_seconds=%.3f effective_timeout_seconds=%s", self.provider, remaining, kwargs.get("timeout"))
        return self.performance.request(self.provider, self.request_get, *args, **kwargs)
