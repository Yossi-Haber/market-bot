import json

import pytest
import requests

from delivery.telegram import run_delivery
from persistence.delivery_ledger import DeliveryClaimDenied, DeliveryLedger
from persistence.github_state import GitHubStateStore
from tests.persistence_fixtures import state_repos


def test_sending_unknown_is_never_automatically_retried(state_repos):
    _, repo, _ = state_repos
    ledger = DeliveryLedger(GitHubStateStore(repo), "synthetic-hmac-key-" + "x" * 32)
    slot = ledger.begin_send("2026-10-14", "synthetic-recipient-alpha")
    ledger.record_outcome("2026-10-14", slot, "UNKNOWN", safe_error="TELEGRAM_TIMEOUT")
    with pytest.raises(DeliveryClaimDenied, match="UNKNOWN"):
        ledger.begin_send("2026-10-14", "synthetic-recipient-alpha")
    payload = json.dumps(GitHubStateStore(repo).read(fetch=True))
    assert "synthetic-recipient-alpha" not in payload
    assert slot.startswith("hmac256:")


def test_sent_slot_is_not_resent_and_other_recipient_can_complete(state_repos):
    _, repo, _ = state_repos
    ledger = DeliveryLedger(GitHubStateStore(repo), b"z" * 32)
    first = ledger.begin_send("2026-10-14", "synthetic-recipient-alpha")
    ledger.record_outcome("2026-10-14", first, "SENT")
    second = ledger.begin_send("2026-10-14", "synthetic-recipient-beta")
    ledger.record_outcome("2026-10-14", second, "SENT")
    with pytest.raises(DeliveryClaimDenied, match="SENT"):
        ledger.begin_send("2026-10-14", "synthetic-recipient-alpha")


def test_restart_converts_persisted_sending_to_unknown(state_repos):
    _, repo, _ = state_repos
    ledger = DeliveryLedger(GitHubStateStore(repo), b"s" * 32)
    slot = ledger.begin_send("2026-10-14", "synthetic-recipient-delta")
    restarted = DeliveryLedger(GitHubStateStore(repo), b"s" * 32)
    with pytest.raises(DeliveryClaimDenied, match="RECOVERED_AS_UNKNOWN"):
        restarted.begin_send("2026-10-14", "synthetic-recipient-delta")
    state = GitHubStateStore(repo).read(fetch=True)
    assert state["deliveries"]["2026-10-14"]["recipients"][slot]["status"] == "UNKNOWN"


def test_failed_safe_requires_explicit_reviewed_retry(state_repos):
    _, repo, _ = state_repos
    ledger = DeliveryLedger(GitHubStateStore(repo), b"q" * 32)
    slot = ledger.begin_send("2026-10-14", "synthetic-recipient-gamma")
    ledger.record_outcome("2026-10-14", slot, "FAILED_SAFE", safe_error="TELEGRAM_CHAT_NOT_FOUND")
    with pytest.raises(DeliveryClaimDenied):
        ledger.begin_send("2026-10-14", "synthetic-recipient-gamma")
    ledger.begin_send("2026-10-14", "synthetic-recipient-gamma", reviewed_retry=True)


def test_telegram_timeout_persists_unknown_without_raw_id_in_manifest(monkeypatch, tmp_path, state_repos):
    _, repo, _ = state_repos
    (tmp_path / "daily_report_visual_validation.json").write_text('{"valid":true}', encoding="utf-8")
    (tmp_path / "daily_report_card.png").write_bytes(b"fixture")
    (tmp_path / "daily_report_data.json").write_text('{"market_date":"2026-10-14","publication_readiness":{"status":"READY"}}', encoding="utf-8")
    monkeypatch.setattr("delivery.telegram.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "synthetic-recipient-timeout")

    class TimeoutTransport:
        def send_photo(self, *args, **kwargs):
            raise requests.Timeout()

    ledger = DeliveryLedger(GitHubStateStore(repo), b"r" * 32)
    assert run_delivery(output_dir=tmp_path, transport=TimeoutTransport(), durable_ledger=ledger) == 1
    manifest = json.loads((tmp_path / "daily_report_delivery.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "UNKNOWN"
    assert "synthetic-recipient-timeout" not in json.dumps(manifest)
    slot = ledger.slot_id("synthetic-recipient-timeout")
    state = GitHubStateStore(repo).read(fetch=True)
    assert state["deliveries"]["2026-10-14"]["recipients"][slot]["status"] == "UNKNOWN"


def test_crash_after_telegram_acceptance_never_sends_that_slot_twice(monkeypatch, tmp_path, state_repos):
    _, repo, _ = state_repos
    (tmp_path / "daily_report_visual_validation.json").write_text('{"valid":true}', encoding="utf-8")
    (tmp_path / "daily_report_card.png").write_bytes(b"fixture")
    (tmp_path / "daily_report_data.json").write_text('{"market_date":"2026-10-14","publication_readiness":{"status":"READY"}}', encoding="utf-8")
    monkeypatch.setattr("delivery.telegram.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "synthetic-recipient-after-acceptance")

    class AcceptedResponse:
        status_code = 200

        def json(self):
            return {"ok": True}

    class CountingTransport:
        calls = 0

        def send_photo(self, *args, **kwargs):
            self.calls += 1
            return AcceptedResponse()

    transport = CountingTransport()
    ledger = DeliveryLedger(GitHubStateStore(repo), b"w" * 32)
    ledger.record_outcome = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("simulated state publication loss"))
    assert run_delivery(output_dir=tmp_path, transport=transport, durable_ledger=ledger) == 1
    assert transport.calls == 1

    restarted = DeliveryLedger(GitHubStateStore(repo), b"w" * 32)
    assert run_delivery(output_dir=tmp_path, transport=transport, durable_ledger=restarted) == 1
    assert transport.calls == 1
    slot = restarted.slot_id("synthetic-recipient-after-acceptance")
    state = GitHubStateStore(repo).read(fetch=True)
    assert state["deliveries"]["2026-10-14"]["recipients"][slot]["status"] == "UNKNOWN"
