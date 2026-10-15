import json

import pytest
import requests

from persistence.d1_client import (
    D1DeliveryLedger,
    D1DeliveryDenied,
    D1OperationUncertain,
    D1StateClient,
    D1StateError,
)


SECRET = "synthetic-test-secret-at-least-32-bytes"


class Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class Session:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def client(session):
    return D1StateClient("https://state.example.test", SECRET, session=session)


def test_client_requires_https_and_finite_timeout():
    with pytest.raises(D1StateError, match="HTTPS_REQUIRED"):
        D1StateClient("http://state.example.test", SECRET)
    with pytest.raises(D1StateError, match="TIMEOUT_INVALID"):
        D1StateClient("https://state.example.test", SECRET, timeout=(0, 8))


def test_timeout_retry_reuses_operation_id_but_rotates_nonce():
    session = Session([
        requests.Timeout("synthetic"),
        Response(200, {"authorized": True, "reservation_id": "same-reservation"}),
    ])
    result = client(session).call(
        "quota.reserve",
        {"provider": "twelve_data", "credits": 1},
        operation_id="a" * 32,
    )
    assert result["authorized"] is True
    first, second = session.calls
    assert first[0] == second[0]
    assert first[1]["timeout"] == second[1]["timeout"] == (3.05, 8.0)
    assert json.loads(first[1]["data"])["operation_id"] == json.loads(second[1]["data"])["operation_id"] == "a" * 32
    assert first[1]["headers"]["X-MB-Nonce"] != second[1]["headers"]["X-MB-Nonce"]


def test_replayed_quota_reservation_never_authorizes_provider_call():
    session = Session([Response(200, {"replayed": True, "authorized": True, "reservation_id": "r"})])
    with pytest.raises(D1OperationUncertain, match="NO_REPLAY_AUTHORIZATION") as caught:
        client(session).call("quota.reserve", {"provider": "twelve_data", "credits": 1}, operation_id="d" * 32)
    assert len(session.calls) == 1
    # The successful HTTP replay proves prior operation state exists, while the client
    # intentionally refuses to return a second authorization.
    assert session.calls[0][1]["data"]
    assert caught.value.operation == "quota.reserve"
    assert caught.value.http_status == 200
    assert caught.value.failure_category == "REPLAY_INDICATES_PRIOR_COMMIT"
    assert caught.value.may_have_committed is True


def test_uncertain_timeout_exposes_safe_operation_diagnostics():
    session = Session([requests.Timeout("synthetic"), requests.Timeout("synthetic")])
    operation_id = "3c" + "a" * 26 + "beef"
    with pytest.raises(D1OperationUncertain) as caught:
        client(session).call("quota.reserve", {"provider": "twelve_data", "credits": 1}, operation_id=operation_id)
    error = caught.value
    assert error.operation == "quota.reserve"
    assert error.operation_id == operation_id
    assert error.sanitized_operation_id == f"{operation_id[:4]}…{operation_id[-4:]}"
    assert error.http_status is None
    assert error.failure_category == "HTTP_TIMEOUT"
    assert error.may_have_committed is True
    assert error.attempts == 2


def test_http_auth_failure_is_classified_and_not_marked_committed():
    session = Session([Response(401, {"error": "AUTHENTICATION_FAILED"})])
    with pytest.raises(D1StateError) as caught:
        client(session).call("quota.reserve", {"provider": "twelve_data", "credits": 1}, operation_id="b" * 32)
    assert caught.value.http_status == 401
    assert caught.value.failure_category == "AUTHENTICATION_OR_AUTHORIZATION"
    assert caught.value.may_have_committed is False


def test_worker_5xx_failure_is_reported_as_commit_possible():
    session = Session([Response(503, {"error": "STATE_BACKEND_UNAVAILABLE"}), Response(503, {"error": "STATE_BACKEND_UNAVAILABLE"})])
    with pytest.raises(D1StateError) as caught:
        client(session).call("quota.reserve", {"provider": "twelve_data", "credits": 1}, operation_id="c" * 32)
    assert caught.value.http_status == 503
    assert caught.value.failure_category == "HTTP_5XX_WORKER_ERROR"
    assert caught.value.may_have_committed is True
    assert caught.value.attempts == 2


def test_delivery_begin_409_denial_maps_to_expected_non_sendable_state():
    session = Session([Response(409, {"operation_id": "e" * 32, "authorized": False, "attempt": 1, "replayed": False})])
    with pytest.raises(D1DeliveryDenied, match="DELIVERY_SLOT_NOT_SENDABLE") as caught:
        client(session).call("delivery.begin", {"market_date": "2099-01-02"}, operation_id="e" * 32)
    assert caught.value.operation == "delivery.begin"
    assert caught.value.http_status == 409
    assert caught.value.failure_category == "DELIVERY_STATE_DENIAL"
    assert caught.value.may_have_committed is True


def test_delivery_adapter_persists_only_hmac_recipient_identity():
    class FakeClient:
        def __init__(self):
            self.arguments = None

        def call(self, op, payload, **kwargs):
            self.arguments = (op, payload)
            return {"authorized": True}

    fake = FakeClient()
    ledger = D1DeliveryLedger(fake, "h" * 32)
    slot = ledger.begin_send("2026-10-14", "private-chat-id")
    operation, payload = fake.arguments
    assert operation == "delivery.begin"
    assert payload["recipient_hmac"] == slot
    assert "private-chat-id" not in json.dumps(payload)
