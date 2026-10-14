import logging

import pytest

from collectors.rate_limit import CreditRateLimitController, RateLimitWaitTimeout


class FakeClock:
    def __init__(self):
        self.value = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


def test_wait_has_one_sleep_and_returns_immediately_after_reset(caplog):
    clock = FakeClock()
    controller = CreditRateLimitController(monotonic_fn=clock.monotonic, sleep_fn=clock.sleep)
    with caplog.at_level(logging.INFO):
        controller.debug_wait(3.0, logging.getLogger("rate-test"))

    assert clock.sleeps == [3.0]
    assert caplog.messages[-2:] == ["RATE_WINDOW_RESET", "RATE_WAIT_RETURN elapsed=3.0"]
    assert "RATE_SLEEP_RETURNED actual=3.0" in caplog.messages


def test_excessive_post_sleep_delay_raises_hard_timeout():
    clock = FakeClock()

    def oversleep(seconds):
        clock.sleeps.append(seconds)
        clock.value += seconds + 11.0

    controller = CreditRateLimitController(monotonic_fn=clock.monotonic, sleep_fn=oversleep)
    with pytest.raises(RateLimitWaitTimeout):
        controller.debug_wait(3.0, logging.getLogger("rate-test"))


def test_wait_uses_monotonic_elapsed_time_not_wall_clock(monkeypatch):
    clock = FakeClock()
    controller = CreditRateLimitController(monotonic_fn=clock.monotonic, sleep_fn=clock.sleep)
    monkeypatch.setattr("collectors.rate_limit.time.time", lambda: 1_000_000.0)
    controller.debug_wait(3.0, logging.getLogger("rate-test"))
    assert clock.sleeps == [3.0]
