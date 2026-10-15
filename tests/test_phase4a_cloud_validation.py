from __future__ import annotations

import os
from pathlib import Path

import pytest

import validate_cloud_state_readonly as readonly


def _state(**overrides):
    state = {
        "schema_version": 1,
        "ownership": {
            "cloud_quota_exclusive": False,
            "windows_sender_disabled": False,
            "cloud_delivery_enabled": False,
        },
        "runs": {},
    }
    state.update(overrides)
    return state


def test_readonly_validator_accepts_supported_schema_and_closed_gates():
    result = readonly.validate_state_payload(_state())
    assert result["ownership"] == {
        "cloud_quota_exclusive": False,
        "windows_sender_disabled": False,
        "cloud_delivery_enabled": False,
    }


@pytest.mark.parametrize("field", readonly.OWNERSHIP_FIELDS)
def test_readonly_validator_fails_closed_if_any_gate_is_open(field):
    state = _state()
    state["ownership"][field] = True
    with pytest.raises(ValueError, match="CLOUD_OWNERSHIP_GATE_NOT_CLOSED"):
        readonly.validate_state_payload(state)


def test_readonly_validator_rejects_missing_or_non_boolean_gate():
    state = _state()
    del state["ownership"]["cloud_delivery_enabled"]
    with pytest.raises(ValueError, match="CLOUD_STATE_OWNERSHIP_INVALID"):
        readonly.validate_state_payload(state)


def test_workflow_has_no_schedule_execute_admin_secret_or_provider_secrets():
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/phase4a-cloud-validation.yml"
    content = workflow.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in content
    assert "schedule:" not in content
    assert "contents: read" in content and "contents: write" not in content
    assert "--execute" not in content and "--send-telegram" not in content
    assert "MARKETBOT_D1_STATE_ADMIN_HMAC_SECRET" not in content
    assert "secrets.MARKETBOT_D1_STATE_HMAC_SECRET" in content
    assert "MARKETBOT_CLOUD_LIVE_ENABLED: \"0\"" in content
    assert "MARKETBOT_CLOUD_TELEGRAM_ENABLED: \"0\"" in content
    assert "MARKETBOT_STATE_BACKEND: d1" in content
    assert content.index("MARKETBOT_STATE_BACKEND: d1") > content.index("Authenticated production Worker state read")
    for secret_name in ("TWELVE_DATA_API_KEY", "OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "FINNHUB_API_KEY", "MARKETAUX_API_TOKEN", "FRED_API_KEY", "FMP_API_KEY"):
        assert f"secrets.{secret_name}" not in content


def test_state_checker_rejects_wrong_endpoint_before_constructing_client(monkeypatch, capsys):
    monkeypatch.setenv("MARKETBOT_D1_STATE_URL", "https://example.invalid")
    monkeypatch.setenv("MARKETBOT_D1_STATE_HMAC_SECRET", "synthetic-secret-not-real")
    monkeypatch.setattr(readonly.D1StateClient, "from_env", lambda: pytest.fail("client must not be created"))
    assert readonly.main() == 1
    assert "CLOUD_WORKER_ENDPOINT_NOT_PINNED" in capsys.readouterr().out


def test_state_checker_reports_pass_without_exposing_state(monkeypatch, capsys):
    monkeypatch.setenv("MARKETBOT_D1_STATE_URL", readonly.EXPECTED_WORKER_URL)
    monkeypatch.setenv("MARKETBOT_D1_STATE_HMAC_SECRET", "synthetic-secret-not-real")

    class FakeClient:
        def read(self):
            return _state(runs={"2099-01-01": {"status": "SENSITIVE_TEST_VALUE"}})

    monkeypatch.setattr(readonly.D1StateClient, "from_env", lambda: FakeClient())
    assert readonly.main() == 0
    output = capsys.readouterr().out
    assert "CLOUD_STATE_READONLY=PASS" in output
    assert "SENSITIVE_TEST_VALUE" not in output
    assert "synthetic-secret-not-real" not in output


def test_no_workflow_path_can_reach_execute_or_telegram(monkeypatch):
    """The checker is intentionally only a state.get client; no run adapter import."""
    assert "run_daily_cloud" not in readonly.__dict__
    assert "D1StateClient" in readonly.__dict__
