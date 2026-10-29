"""Offline failure injection at Telegram and D1 acknowledgment boundaries."""

import copy
import json
from datetime import datetime, timezone

import pytest
import requests

import run_daily_cloud as cloud
from delivery.telegram import classify_telegram_response, run_delivery
from persistence.d1_client import D1DeliveryDenied, D1DeliveryLedger, D1OperationUncertain, D1StateClient, D1StateError


class Response:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.payload = payload

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


ACK = {"ok": True, "result": {"message_id": 123}}


@pytest.fixture(autouse=True)
def isolate_execution_modes(monkeypatch):
    """Cloud run sets process-local modes; do not leak them into other tests."""
    monkeypatch.setenv("MARKETBOT_PERSISTENT_QUOTA_MODE", "0")
    monkeypatch.setenv("MARKETBOT_PERSISTENT_DELIVERY_MODE", "0")


@pytest.mark.parametrize("status,payload", [
    (200, ValueError("synthetic malformed JSON")), (200, {}), (200, []),
    (200, {"ok": False}), (200, {"ok": 1, "result": {"message_id": 1}}),
    (200, {"ok": True}), (200, {"ok": True, "result": []}),
    (200, {"ok": True, "result": {}}),
    (200, {"ok": True, "result": {"message_id": True}}),
    (200, {"ok": True, "result": {"message_id": 0}}),
    (200, {"ok": True, "result": {"message_id": -1}}),
    (200, {"ok": True, "result": {"message_id": "123"}}),
    (500, {"ok": False, "error_code": 500}), (503, ACK),
    (400, {}), (403, ValueError("synthetic proxy reply")), (302, ACK),
])
def test_ambiguous_protocol_responses_are_unknown(status, payload):
    outcome, error = classify_telegram_response(Response(status, payload))
    assert outcome == "UNKNOWN"
    assert error is not None


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
def test_only_explicit_supported_non_delivery_errors_are_failed_safe(status):
    outcome, _ = classify_telegram_response(Response(status, {"ok": False, "error_code": status}))
    assert outcome == "FAILED_SAFE"
    assert classify_telegram_response(Response(status, {"ok": False, "error_code": 999}))[0] == "UNKNOWN"


def test_complete_telegram_message_ack_is_sent():
    assert classify_telegram_response(Response(200, ACK)) == ("SENT", None)


class MemoryClient:
    """A failure-injecting client double; real Worker SQL is tested separately."""
    def __init__(self, *, lose_begin=False, outcome_failure=None, fail_read=False, legacy_worker=False):
        self.row = None
        self.lose_begin = lose_begin
        self.outcome_failure = outcome_failure
        self.fail_read = fail_read
        self.legacy_worker = legacy_worker
        self.calls = []

    def call(self, op, payload, **kwargs):
        self.calls.append((op, copy.deepcopy(payload), kwargs))
        if op == "state.get":
            if self.fail_read:
                raise D1StateError("SYNTHETIC_READ_UNAVAILABLE")
            return {"schema_version": 1, "delivery_protocol_version": 2, "delivery_recipient": copy.deepcopy(self.row)}
        if op == "delivery.begin":
            if self.row and not (self.row["status"] == "FAILED_SAFE" and payload["reviewed_retry"]):
                raise D1DeliveryDenied("DELIVERY_SLOT_NOT_SENDABLE")
            self.row = {"status": "SENDING", "attempt_id": payload["attempt_id"], "attempt": (self.row or {}).get("attempt", 0) + 1}
            if self.lose_begin:
                raise D1OperationUncertain("SYNTHETIC_BEGIN_ACK_LOSS", may_have_committed=True)
            return {"authorized": True, **({} if self.legacy_worker else {"attempt_id": payload["attempt_id"]})}
        if op == "delivery.outcome":
            if self.row["status"] != "SENDING" or payload["attempt_id"] != self.row["attempt_id"]:
                raise D1DeliveryDenied("STALE_ATTEMPT")
            if self.outcome_failure == "before":
                raise D1StateError("SYNTHETIC_WRITE_UNAVAILABLE", may_have_committed=True)
            self.row["status"] = payload["status"]
            if self.outcome_failure == "after":
                raise D1OperationUncertain("SYNTHETIC_OUTCOME_ACK_LOSS", may_have_committed=True)
            return {"applied": True}
        raise AssertionError("unexpected operation")


class Transport:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    def send_photo(self, *args):
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def deliver(tmp_path, monkeypatch, client, outcome):
    (tmp_path / "daily_report_visual_validation.json").write_text('{"valid":true}', encoding="utf-8")
    (tmp_path / "daily_report_card.png").write_bytes(b"synthetic PNG")
    (tmp_path / "daily_report_data.json").write_text('{"market_date":"2026-10-27","publication_readiness":{"status":"READY"}}', encoding="utf-8")
    monkeypatch.setattr("delivery.telegram.load_telegram_config", lambda: ("synthetic-token", ["synthetic-recipient"]))
    ledger = D1DeliveryLedger(client, "synthetic-recipient-hmac-" + "x" * 32)
    transport = Transport(outcome)
    code = run_delivery(output_dir=tmp_path, durable_ledger=ledger, transport=transport)
    manifest = json.loads((tmp_path / "daily_report_delivery.json").read_text(encoding="utf-8"))
    assert "synthetic-recipient" not in json.dumps(manifest)
    assert "synthetic-token" not in json.dumps(manifest)
    return code, manifest, transport


def test_lost_begin_ack_records_uncertainty_without_sending(tmp_path, monkeypatch):
    client = MemoryClient(lose_begin=True)
    code, manifest, transport = deliver(tmp_path, monkeypatch, client, Response(200, ACK))
    assert code == 1 and manifest["status"] == "UNKNOWN"
    assert client.row["status"] == "SENDING" and transport.calls == 0
    assert [op for op, _, _ in client.calls] == ["delivery.begin", "state.get"]


@pytest.mark.parametrize("error", [requests.ReadTimeout(), requests.ConnectionError()])
def test_lost_unknown_write_never_escapes_as_retryable_failure(tmp_path, monkeypatch, error):
    client = MemoryClient(outcome_failure="before")
    code, manifest, transport = deliver(tmp_path, monkeypatch, client, error)
    assert code == 1 and manifest["status"] == "UNKNOWN"
    assert client.row["status"] == "SENDING" and transport.calls == 1


def test_lost_sent_ack_is_resolved_from_same_attempt_without_resending(tmp_path, monkeypatch):
    client = MemoryClient(outcome_failure="after")
    code, manifest, transport = deliver(tmp_path, monkeypatch, client, Response(200, ACK))
    assert code == 0 and manifest["status"] == "SENT"
    assert client.row["status"] == "SENT" and transport.calls == 1
    assert [op for op, _, _ in client.calls] == ["delivery.begin", "delivery.outcome", "state.get"]


def test_failed_reconciliation_retains_uncertainty(tmp_path, monkeypatch):
    client = MemoryClient(outcome_failure="after", fail_read=True)
    code, manifest, transport = deliver(tmp_path, monkeypatch, client, Response(200, ACK))
    assert code == 1 and manifest["status"] == "UNKNOWN"
    assert client.row["status"] == "SENT" and transport.calls == 1


def test_old_worker_cannot_authorize_unfenced_send(tmp_path, monkeypatch):
    client = MemoryClient(legacy_worker=True)
    code, manifest, transport = deliver(tmp_path, monkeypatch, client, Response(200, ACK))
    assert code == 1 and manifest["status"] == "UNKNOWN" and transport.calls == 0


def test_stale_handle_cannot_update_new_attempt_in_same_ledger_instance():
    client = MemoryClient()
    ledger = D1DeliveryLedger(client, "x" * 32)
    first = ledger.begin_send("2026-10-27", "synthetic-recipient")
    ledger.record_outcome("2026-10-27", first, "FAILED_SAFE")
    with pytest.raises(D1DeliveryDenied, match="STOP_CONFIRMATION"):
        ledger.begin_send("2026-10-27", "synthetic-recipient", reviewed_retry=True)
    second = ledger.begin_send("2026-10-27", "synthetic-recipient", reviewed_retry=True, sender_stopped=True)
    assert first == second and first.attempt_id != second.attempt_id
    with pytest.raises(AttributeError):
        first.attempt_id = second.attempt_id
    with pytest.raises(D1DeliveryDenied, match="STALE_ATTEMPT"):
        ledger.record_outcome("2026-10-27", first, "SENT")
    assert client.row["status"] == "SENDING" and client.row["attempt"] == 2
    ledger.record_outcome("2026-10-27", second, "SENT")
    assert client.calls[-3][2]["admin"] is True


def test_plain_slot_cannot_invent_an_attempt_token():
    ledger = D1DeliveryLedger(MemoryClient(), "x" * 32)
    with pytest.raises(D1DeliveryDenied, match="ATTEMPT_MISSING"):
        ledger.record_outcome("2026-10-27", ledger.slot_id("synthetic-recipient"), "SENT")


def test_legacy_sender_cannot_bypass_cloud_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKETBOT_STATE_BACKEND", "d1")
    transport = Transport(Response(200, ACK))
    assert run_delivery(output_dir=tmp_path, transport=transport) == 1
    assert transport.calls == 0
    manifest = json.loads((tmp_path / "daily_report_delivery.json").read_text(encoding="utf-8"))
    assert manifest["error"] == "DURABLE_DELIVERY_LEDGER_REQUIRED"


def test_cloud_mode_loaded_late_cannot_bypass_ledger(tmp_path, monkeypatch):
    monkeypatch.delenv("MARKETBOT_STATE_BACKEND", raising=False)
    monkeypatch.delenv("MARKETBOT_PERSISTENT_DELIVERY_MODE", raising=False)
    (tmp_path / "daily_report_visual_validation.json").write_text('{"valid":true}', encoding="utf-8")
    (tmp_path / "daily_report_card.png").write_bytes(b"synthetic PNG")
    (tmp_path / "daily_report_data.json").write_text('{"market_date":"2026-10-27"}', encoding="utf-8")
    def load():
        monkeypatch.setenv("MARKETBOT_STATE_BACKEND", "d1")
        return "synthetic-token", ["synthetic-recipient"]
    monkeypatch.setattr("delivery.telegram.load_telegram_config", load)
    transport = Transport(Response(200, ACK))
    assert run_delivery(output_dir=tmp_path, transport=transport) == 1
    assert transport.calls == 0
    assert "DURABLE_DELIVERY_LEDGER_REQUIRED" in (tmp_path / "daily_report_delivery.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("value,expected", [
    ("2026-10-26T21:00:00.123Z", datetime(2026, 10, 26, 21, 0, 0, 123000, tzinfo=timezone.utc)),
    ("2026-10-26T21:00:00+00:00", datetime(2026, 10, 26, 21, tzinfo=timezone.utc)),
    ("2026-10-26T17:00:00-04:00", datetime(2026, 10, 26, 21, tzinfo=timezone.utc)),
    (None, None), ("invalid", None), ("2026-10-26T21:00:00", None),
])
def test_worker_timestamps_are_python310_compatible_and_aware(value, expected):
    assert cloud._parse_utc(value) == expected


class Session:
    def __init__(self, *, prior=None, delivery_status=None, fail_read=False):
        self.calls = []
        self.fail_read = fail_read
        self.state = {"schema_version": 1, "generation": 0, "created_at": "2026-10-01T00:00:00Z", "ownership": {"cloud_quota_exclusive": True, "windows_sender_disabled": True, "cloud_delivery_enabled": False}, "runs": {"2026-10-27": prior} if prior else {}, "deliveries_by_market_date_and_status": ([{"market_date": "2026-10-27", "status": delivery_status, "recipient_count": 1}] if delivery_status else [])}

    def post(self, url, **kwargs):
        request = json.loads(kwargs["data"])
        self.calls.append(request)
        op, payload = request["op"], request["payload"]
        if op == "state.get":
            if self.fail_read:
                raise requests.ConnectionError()
            return Response(200, copy.deepcopy(self.state))
        if op == "run.claim":
            assert not self.state["runs"]
            self.state["runs"][payload["market_date"]] = {"status": "CLAIMED", "run_id": payload["run_id"]}
            return Response(200, {"claim_authorized": True})
        if op == "run.transition":
            row = self.state["runs"][payload["market_date"]]
            assert row["status"] == payload["from"]
            row["status"] = payload["to"]
            return Response(200, {"applied": True})
        raise AssertionError("unexpected mutation")


def state_client(session):
    return D1StateClient("https://offline.example.test", "synthetic-api-secret-" + "x" * 32, session=session)


@pytest.mark.parametrize("status", ["CLAIMED", "RUNNING", "COMPLETE", "DELIVERY_UNKNOWN"])
def test_existing_claim_is_never_automatically_recovered_or_reclaimed(tmp_path, monkeypatch, status):
    session = Session(prior={"status": status, "run_id": "a" * 32, "started_at": "2026-10-27T20:00:00.000Z"})
    monkeypatch.setattr(cloud, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(cloud, "_state_store", lambda: state_client(session))
    monkeypatch.setattr(cloud, "MANIFEST_DIR", tmp_path)
    monkeypatch.setenv("MARKETBOT_CLOUD_LIVE_ENABLED", "1")
    assert cloud.run(["--execute", "--market-date", "2026-10-27"], now=datetime(2026, 10, 28, 22, tzinfo=timezone.utc)) == 1
    assert [request["op"] for request in session.calls] == ["state.get"]
    manifest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert manifest["stale"] is True and manifest["automatic_recovery"] is False


@pytest.mark.parametrize("pipeline_code,expected", [(0, "COMPLETE"), (1, "FAILED")])
def test_report_only_path_is_unchanged(tmp_path, monkeypatch, pipeline_code, expected):
    session = Session()
    monkeypatch.setattr(cloud, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(cloud, "_state_store", lambda: state_client(session))
    monkeypatch.setattr(cloud, "MANIFEST_DIR", tmp_path)
    monkeypatch.setenv("MARKETBOT_CLOUD_LIVE_ENABLED", "1")
    monkeypatch.setattr("run_daily.run", lambda args: pipeline_code if "--skip-telegram" in args else pytest.fail("Telegram must remain skipped"))
    assert cloud.run(["--execute", "--market-date", "2026-10-27"], now=datetime(2026, 10, 28, 22, tzinfo=timezone.utc)) == pipeline_code
    assert session.state["runs"]["2026-10-27"]["status"] == expected
    assert not any(request["op"].startswith(("quota.", "delivery.")) for request in session.calls)


@pytest.mark.parametrize("status", ["SENDING", "UNKNOWN"])
def test_authoritative_uncertainty_overrides_local_failed_manifest(tmp_path, monkeypatch, status):
    (tmp_path / "daily_report_delivery.json").write_text('{"report_date":"2026-10-27","status":"FAILED"}', encoding="utf-8")
    monkeypatch.setattr(cloud, "OUTPUT_DIR", tmp_path)
    assert cloud._delivery_status(1, "2026-10-27", store=state_client(Session(delivery_status=status))) == "DELIVERY_UNKNOWN"


def test_state_read_outage_cannot_become_retryable_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud, "OUTPUT_DIR", tmp_path)
    assert cloud._delivery_status(1, "2026-10-27", store=state_client(Session(fail_read=True))) == "DELIVERY_UNKNOWN"


def test_cloud_persists_unknown_terminal_disposition_after_delivery_state_failure(tmp_path, monkeypatch):
    session = Session()
    session.state["ownership"]["cloud_delivery_enabled"] = True
    monkeypatch.setattr(cloud, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(cloud, "_state_store", lambda: state_client(session))
    monkeypatch.setattr(cloud, "MANIFEST_DIR", tmp_path)
    monkeypatch.setattr(cloud, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("MARKETBOT_CLOUD_LIVE_ENABLED", "1")
    monkeypatch.setenv("MARKETBOT_CLOUD_TELEGRAM_ENABLED", "1")
    def pipeline(args):
        assert "--skip-telegram" not in args
        session.state["deliveries_by_market_date_and_status"] = [{"market_date": "2026-10-27", "status": "SENDING", "recipient_count": 1}]
        (tmp_path / "daily_report_delivery.json").write_text('{"report_date":"2026-10-27","status":"UNKNOWN"}', encoding="utf-8")
        return 3
    monkeypatch.setattr("run_daily.run", pipeline)
    assert cloud.run(["--execute", "--send-telegram", "--market-date", "2026-10-27"], now=datetime(2026, 10, 28, 22, tzinfo=timezone.utc)) == 3
    assert session.state["runs"]["2026-10-27"]["status"] == "DELIVERY_UNKNOWN"
    assert json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))["status"] == "DELIVERY_UNKNOWN"


def test_recovery_requires_stop_confirmation_and_admin_credentials():
    client = state_client(Session())
    with pytest.raises(D1StateError, match="SENDER_STOP_CONFIRMATION_REQUIRED"):
        client.recover_run("2026-10-27", "a" * 32)
    with pytest.raises(D1StateError, match="ADMIN_CONFIGURATION_MISSING"):
        client.recover_run("2026-10-27", "a" * 32, sender_stopped=True)
