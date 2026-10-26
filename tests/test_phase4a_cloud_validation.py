from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import validate_cloud_state_readonly as readonly


_ZERO_INTEGRITY = dict.fromkeys(readonly.INTEGRITY_FIELDS, 0)


def _state(**overrides):
    state = {
        "schema_version": 1,
        "created_at": "2026-01-01T00:00:00.000Z",
        "ownership": {
            "cloud_quota_exclusive": False,
            "windows_sender_disabled": False,
            "cloud_delivery_enabled": False,
        },
        "runs": {},
        "quota": {"buckets": [], "reservations_by_utc_day": []},
        "deliveries_by_market_date_and_status": [],
        "operations_by_type_and_status": [],
        "integrity_window_days": 7,
        "integrity": dict(_ZERO_INTEGRITY),
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


def test_readonly_validator_accepts_explicit_production_ownership_110():
    expected = readonly.parse_expected_ownership("1,1,0")
    state = _state(ownership={
        "cloud_quota_exclusive": True,
        "windows_sender_disabled": True,
        "cloud_delivery_enabled": False,
    })
    result = readonly.validate_state_payload(state, expected_ownership=expected)
    assert result["ownership_matches_expected"] is True
    assert result["ownership"] == state["ownership"]


def test_readonly_validator_distinguishes_gate_mismatch_from_integrity_failure():
    expected = readonly.parse_expected_ownership("1,1,0")
    with pytest.raises(ValueError, match="CLOUD_OWNERSHIP_CONFIGURATION_MISMATCH expected=1,1,0 actual=0,0,0"):
        readonly.validate_state_payload(_state(), expected_ownership=expected)
    state = _state(ownership={
        "cloud_quota_exclusive": True,
        "windows_sender_disabled": True,
        "cloud_delivery_enabled": False,
    })
    state["integrity"]["reservations_without_operation"] = 1
    with pytest.raises(ValueError, match="CLOUD_STATE_INTEGRITY_FAILED"):
        readonly.validate_state_payload(state, expected_ownership=expected)


def test_readonly_validator_reports_dates_delivery_quota_and_operations_without_ids():
    expected = readonly.parse_expected_ownership("1,1,0")
    state = _state(
        ownership={"cloud_quota_exclusive": True, "windows_sender_disabled": True, "cloud_delivery_enabled": False},
        runs={"2026-10-26": {"status": "COMPLETE", "run_id": "private-run-id"}},
        quota={"buckets": [{"period": "day", "bucket_key": "2026-10-27", "used_credits": 4, "limit_credits": 800}],
               "reservations_by_utc_day": [{"utc_day": "2026-10-27", "reservation_count": 4, "credits": 4}]},
        deliveries_by_market_date_and_status=[{"market_date": "2026-10-26", "status": "SENT", "recipient_count": 2, "attempts_total": 2, "max_attempt": 1}],
        operations_by_type_and_status=[{"operation_type": "delivery.begin", "status": "COMMITTED", "operation_count": 2}],
    )
    result = readonly.validate_state_payload(state, expected_ownership=expected, market_dates=("2026-10-26", "2026-10-27"))
    assert result["run_statuses"] == {"2026-10-26": "COMPLETE", "2026-10-27": "NONE"}
    assert result["deliveries"]["2026-10-26"]["by_status"] == {"SENT": 2}
    assert result["deliveries"]["2026-10-27"]["recipient_count"] == 0
    assert result["quota_buckets"][0]["used_credits"] == 4
    assert result["operations_by_type_and_status"] == {"delivery.begin:COMMITTED": 2}
    assert "private-run-id" not in str(result)


def test_market_session_eligibility_uses_frozen_new_york_close_and_d1_claim_state():
    before_close = datetime(2026, 10, 26, 19, 30, tzinfo=timezone.utc)
    after_close = datetime(2026, 10, 26, 20, 30, tzinfo=timezone.utc)
    early = readonly.assess_date_eligibility("2026-10-26", has_claim=False, now=before_close, state_created_date="2026-01-01")
    complete = readonly.assess_date_eligibility("2026-10-26", has_claim=False, now=after_close, state_created_date="2026-01-01")
    claimed = readonly.assess_date_eligibility("2026-10-26", has_claim=True, now=after_close, state_created_date="2026-01-01")
    assert early["eligibility_reason"] == "SESSION_NOT_CLOSED"
    assert complete["eligible_for_explicit_processing"] is True
    assert claimed["eligibility_reason"] == "ALREADY_CLAIMED"


def test_readonly_validator_rejects_missing_extended_state_and_malformed_counts():
    state = _state()
    del state["quota"]
    with pytest.raises(ValueError, match="CLOUD_STATE_OPERATIONAL_SUMMARY_INVALID"):
        readonly.validate_state_payload(state)
    state = _state(integrity={**_ZERO_INTEGRITY, "deliveries_without_run": None})
    with pytest.raises(ValueError, match="CLOUD_STATE_INTEGRITY_SUMMARY_INVALID"):
        readonly.validate_state_payload(state)


@pytest.mark.parametrize("value", ["1,1", "1,2,0", "true,false,false", "1,0,0,0"])
def test_expected_ownership_parser_is_strict(value):
    with pytest.raises(ValueError, match="CLOUD_EXPECTED_OWNERSHIP_INVALID"):
        readonly.parse_expected_ownership(value)


@pytest.mark.parametrize("field", readonly.OWNERSHIP_FIELDS)
def test_readonly_validator_fails_closed_if_any_gate_is_open(field):
    state = _state()
    state["ownership"][field] = True
    with pytest.raises(ValueError, match="CLOUD_OWNERSHIP_CONFIGURATION_MISMATCH"):
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
    assert "--expected-ownership 1,1,0" in content
    assert "--market-dates" in content
    assert content.index("MARKETBOT_STATE_BACKEND: d1") > content.index("Authenticated production Worker state read")
    for secret_name in ("TWELVE_DATA_API_KEY", "OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "FINNHUB_API_KEY", "MARKETAUX_API_TOKEN", "FRED_API_KEY", "FMP_API_KEY"):
        assert f"secrets.{secret_name}" not in content


def test_state_checker_rejects_wrong_endpoint_before_constructing_client(monkeypatch, capsys):
    monkeypatch.setenv("MARKETBOT_D1_STATE_URL", "https://example.invalid")
    monkeypatch.setenv("MARKETBOT_D1_STATE_HMAC_SECRET", "synthetic-secret-not-real")
    monkeypatch.setattr(readonly.D1StateClient, "from_env", lambda: pytest.fail("client must not be created"))
    monkeypatch.setattr("sys.argv", ["validate_cloud_state_readonly.py"])
    assert readonly.main() == 1
    assert "CLOUD_WORKER_ENDPOINT_NOT_PINNED" in capsys.readouterr().out


def test_state_checker_reports_pass_without_exposing_state(monkeypatch, capsys):
    monkeypatch.setenv("MARKETBOT_D1_STATE_URL", readonly.EXPECTED_WORKER_URL)
    monkeypatch.setenv("MARKETBOT_D1_STATE_HMAC_SECRET", "synthetic-secret-not-real")

    class FakeClient:
        reads = 0

        def read(self):
            self.reads += 1
            return _state(runs={"2099-01-01": {"status": "SENSITIVE_TEST_VALUE"}})

    client = FakeClient()
    monkeypatch.setattr(readonly.D1StateClient, "from_env", lambda: client)
    monkeypatch.setattr("sys.argv", ["validate_cloud_state_readonly.py"])
    assert readonly.main() == 0
    output = capsys.readouterr().out
    assert "CLOUD_STATE_READONLY=PASS" in output
    assert "SENSITIVE_TEST_VALUE" not in output
    assert "synthetic-secret-not-real" not in output
    assert client.reads == 2


def test_no_workflow_path_can_reach_execute_or_telegram(monkeypatch):
    """The checker is intentionally only a state.get client; no run adapter import."""
    assert "run_daily_cloud" not in readonly.__dict__
    assert "D1StateClient" in readonly.__dict__
