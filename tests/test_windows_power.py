import logging
from types import SimpleNamespace

import build_historical_evaluation_batch as batch
from utils import windows_power


class FakeExecutionState:
    def __init__(self, result=1):
        self.calls = []
        self.result = result

    def __call__(self, flags):
        self.calls.append(flags)
        return self.result


def _windows_api(monkeypatch, setter):
    monkeypatch.setattr(windows_power.sys, "platform", "win32")
    monkeypatch.setattr(windows_power.ctypes, "windll", SimpleNamespace(kernel32=SimpleNamespace(SetThreadExecutionState=setter)), raising=False)


def test_windows_execution_state_is_enabled_and_released(monkeypatch):
    setter = FakeExecutionState()
    _windows_api(monkeypatch, setter)
    with windows_power.prevent_system_sleep(logger=logging.getLogger("test")):
        pass
    assert setter.calls == [windows_power.ES_CONTINUOUS | windows_power.ES_SYSTEM_REQUIRED, windows_power.ES_CONTINUOUS]


def test_release_happens_after_exception_and_keyboard_interrupt(monkeypatch):
    for raised in (RuntimeError("boom"), KeyboardInterrupt()):
        setter = FakeExecutionState()
        _windows_api(monkeypatch, setter)
        try:
            with windows_power.prevent_system_sleep():
                raise raised
        except BaseException:
            pass
        assert setter.calls[-1] == windows_power.ES_CONTINUOUS


def test_non_windows_is_safe_noop(monkeypatch):
    monkeypatch.setattr(windows_power.sys, "platform", "linux")
    with windows_power.prevent_system_sleep():
        pass


def test_windows_api_failure_does_not_abort(monkeypatch):
    setter = FakeExecutionState(result=0)
    _windows_api(monkeypatch, setter)
    with windows_power.prevent_system_sleep():
        marker = True
    assert marker is True
    assert setter.calls == [windows_power.ES_CONTINUOUS | windows_power.ES_SYSTEM_REQUIRED, windows_power.ES_CONTINUOUS]


def test_plan_mode_does_not_enable_sleep_prevention(monkeypatch):
    monkeypatch.setattr(batch, "inspect_components", lambda value: {
        "date": value, "trading_day_status": "TRADING_DAY", "actions_needed": "none",
        "plan_ready": True, "expected_network_work": False,
    })

    class MustNotEnter:
        def __enter__(self):
            raise AssertionError("sleep prevention enabled during plan")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(batch, "prevent_system_sleep", lambda **kwargs: MustNotEnter())
    result = batch.run_batch(["2026-09-28"], plan=True)
    assert result["components"][0]["status"] == "PLAN"
