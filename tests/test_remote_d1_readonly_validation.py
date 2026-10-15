from __future__ import annotations

import json

import pytest

from persistence.d1_client import D1StateClient, D1StateError
from validate_remote_d1_readonly import StatusRecordingSession, get_secret, summarize_read, validate_read_only


def _state(*, schema_version: int = 1, gates: dict | None = None) -> dict:
    return {
        "schema_version": schema_version,
        "generation": 0,
        "created_at": "2026-10-14T00:00:00.000Z",
        "ownership": gates or {
            "cloud_quota_exclusive": False,
            "windows_sender_disabled": False,
            "cloud_delivery_enabled": False,
        },
        "runs": {},
    }


class _FakeResponse:
    status_code = 200

    def __init__(self, payload: dict):
        self.payload = payload

    def json(self) -> dict:
        return self.payload


class _FakeSession:
    def __init__(self):
        self.requests: list[dict] = []

    def post(self, url, *, data, headers, timeout):
        self.requests.append({"url": url, "body": json.loads(data), "headers": headers, "timeout": timeout})
        return _FakeResponse(_state())


def test_remote_validation_uses_only_two_authenticated_state_get_reads():
    fake = _FakeSession()
    status_session = StatusRecordingSession(fake)
    client = D1StateClient(
        "https://example.workers.dev",
        "synthetic-test-caller-secret-value-32-bytes",
        session=status_session,
    )

    result = validate_read_only(client, status_session.statuses)

    assert result["operation"] == "state.get"
    assert result["http_statuses"] == [200, 200]
    assert result["schema_compatible"] is True
    assert result["reads_consistent"] is True
    assert result["ownership_unchanged_and_closed"] is True
    assert result["state_changing_operations"] == 0
    assert len(fake.requests) == 2
    assert [request["body"]["op"] for request in fake.requests] == ["state.get", "state.get"]
    assert all(request["headers"].get("X-MB-Signature") for request in fake.requests)


def test_schema_mismatch_fails_closed():
    with pytest.raises(D1StateError, match="D1_STATE_SCHEMA_INVALID"):
        summarize_read(_state(schema_version=2))


@pytest.mark.parametrize(
    "gates",
    [
        {"cloud_quota_exclusive": False, "windows_sender_disabled": False},
        {"cloud_quota_exclusive": False, "windows_sender_disabled": False, "cloud_delivery_enabled": 0},
        {"cloud_quota_exclusive": False, "windows_sender_disabled": False, "cloud_delivery_enabled": False, "extra": False},
    ],
)
def test_ownership_schema_requires_exact_boolean_fields(gates):
    with pytest.raises(D1StateError, match="D1_OWNERSHIP_SCHEMA_INVALID|D1_OWNERSHIP_VALUES_INVALID"):
        summarize_read(_state(gates=gates))


def test_no_secret_is_in_validation_result():
    secret = "synthetic-test-caller-secret-value-32-bytes"
    fake = _FakeSession()
    status_session = StatusRecordingSession(fake)
    client = D1StateClient("https://example.workers.dev", secret, session=status_session)
    result = validate_read_only(client, status_session.statuses)
    assert secret not in json.dumps(result)


def test_secret_loads_from_private_environment_without_display(monkeypatch):
    expected = "synthetic-env-caller-secret-32-bytes-minimum"
    monkeypatch.delenv("MARKETBOT_D1_STATE_HMAC_SECRET", raising=False)
    monkeypatch.setenv("STATE_API_HMAC_SECRET", expected)
    assert get_secret() == expected


def test_missing_secret_fails_without_prompt_by_default(monkeypatch):
    monkeypatch.delenv("MARKETBOT_D1_STATE_HMAC_SECRET", raising=False)
    monkeypatch.delenv("STATE_API_HMAC_SECRET", raising=False)
    with pytest.raises(D1StateError, match="D1_WORKER_CONFIGURATION_MISSING"):
        get_secret()
