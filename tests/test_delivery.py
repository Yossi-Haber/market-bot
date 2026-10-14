"""Offline tests for manual Telegram delivery and its publication gate."""

import json
from pathlib import Path

import requests

from delivery.telegram import load_telegram_config, mask_chat_id, parse_chat_ids, resolve_chat_ids, run_delivery


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload or {"ok": True}

    def json(self):
        return self.payload


class FakeTransport:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.calls = []

    def send_photo(self, token, chat_id, image_path, caption):
        self.calls.append((token, chat_id, image_path, caption))
        if self.error:
            raise self.error
        return self.response


class SequenceTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def send_photo(self, token, chat_id, image_path, caption):
        self.calls.append(chat_id)
        response = self.responses[len(self.calls) - 1]
        return response() if callable(response) else response


def setup_output(tmp_path, publishable=True):
    (tmp_path / "daily_report_visual_validation.json").write_text(json.dumps({"valid": publishable}), encoding="utf-8")
    (tmp_path / "daily_report_card.png").write_bytes(b"png")
    (tmp_path / "daily_report_data.json").write_text(json.dumps({"market_date": "2026-09-25", "publication_readiness": {"status": "READY_WITH_WARNINGS"}}), encoding="utf-8")


def manifest(tmp_path):
    return json.loads((tmp_path / "daily_report_delivery.json").read_text(encoding="utf-8"))


def test_missing_token_and_chat_id(monkeypatch, tmp_path):
    setup_output(tmp_path)
    monkeypatch.setattr("delivery.telegram.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_IDS", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert run_delivery(output_dir=tmp_path) == 1
    assert manifest(tmp_path)["error"] == "TELEGRAM_CONFIG_MISSING"


def test_non_publishable_and_missing_image_do_not_send(monkeypatch, tmp_path):
    setup_output(tmp_path, publishable=False)
    monkeypatch.setattr("delivery.telegram.load_dotenv", lambda *args, **kwargs: None)
    transport = FakeTransport()
    assert run_delivery(output_dir=tmp_path, transport=transport) == 1
    assert transport.calls == []
    assert manifest(tmp_path)["error"] == "REPORT_NOT_PUBLISHABLE"
    setup_output(tmp_path, publishable=True)
    (tmp_path / "daily_report_card.png").unlink()
    assert run_delivery(output_dir=tmp_path, transport=transport) == 1
    assert manifest(tmp_path)["error"] == "TELEGRAM_IMAGE_MISSING"


def test_dry_run_does_not_send(monkeypatch, tmp_path):
    setup_output(tmp_path)
    monkeypatch.setattr("delivery.telegram.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    transport = FakeTransport()
    assert run_delivery(dry_run=True, output_dir=tmp_path, transport=transport) == 0
    assert transport.calls == []
    assert manifest(tmp_path)["status"] == "WOULD_SEND"
    assert manifest(tmp_path)["summary"] == {"total": 0, "sent": 0, "failed": 0}


def test_success_manifest_and_caption(monkeypatch, tmp_path):
    setup_output(tmp_path)
    monkeypatch.setattr("delivery.telegram.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    transport = FakeTransport()
    assert run_delivery(output_dir=tmp_path, transport=transport) == 0
    assert manifest(tmp_path)["status"] == "SENT"
    assert "25.09.2026" in transport.calls[0][3]
    assert "secret-token" not in json.dumps(manifest(tmp_path))


def test_telegram_error_mapping(monkeypatch, tmp_path):
    monkeypatch.setattr("delivery.telegram.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    for status, payload, expected in ((401, {}, "TELEGRAM_AUTH_FAILED"), (403, {}, "TELEGRAM_AUTH_FAILED"), (400, {"description": "Bad Request: chat not found"}, "TELEGRAM_CHAT_NOT_FOUND"), (429, {}, "TELEGRAM_RATE_LIMITED")):
        setup_output(tmp_path)
        assert run_delivery(output_dir=tmp_path, transport=FakeTransport(FakeResponse(status, payload))) == 1
        assert manifest(tmp_path)["recipients"][0]["error"] == expected


def test_timeout_and_network_errors(monkeypatch, tmp_path):
    setup_output(tmp_path)
    monkeypatch.setattr("delivery.telegram.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    assert run_delivery(output_dir=tmp_path, transport=FakeTransport(error=requests.Timeout())) == 1
    assert manifest(tmp_path)["recipients"][0]["error"] == "TELEGRAM_TIMEOUT"
    assert run_delivery(output_dir=tmp_path, transport=FakeTransport(error=requests.ConnectionError())) == 1
    assert manifest(tmp_path)["recipients"][0]["error"] == "TELEGRAM_NETWORK_ERROR"


def test_chat_id_parser_and_resolution(monkeypatch):
    assert parse_chat_ids("123, 456,123,,789, -10, -10") == ["123", "456", "789", "-10"]
    assert resolve_chat_ids({"TELEGRAM_CHAT_IDS": "123,456", "TELEGRAM_CHAT_ID": "legacy"}) == ["123", "456"]
    assert resolve_chat_ids({"TELEGRAM_CHAT_IDS": "", "TELEGRAM_CHAT_ID": "legacy"}) == ["legacy"]
    assert mask_chat_id("123456789") == "***6789"


def test_loaded_env_resolves_two_recipients(monkeypatch):
    monkeypatch.setattr("delivery.telegram.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "configured-token")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "307738874,641418787")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    token, chat_ids = load_telegram_config()
    assert token == "configured-token"
    assert chat_ids == ["307738874", "641418787"]


def test_legacy_recipient_resolution_and_missing_config(monkeypatch):
    monkeypatch.setattr("delivery.telegram.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "configured-token")
    monkeypatch.delenv("TELEGRAM_CHAT_IDS", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "legacy-id")
    token, chat_ids = load_telegram_config()
    assert token == "configured-token"
    assert chat_ids == ["legacy-id"]
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    token, chat_ids = load_telegram_config()
    assert not token and chat_ids == ["legacy-id"]
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    token, chat_ids = load_telegram_config()
    assert not token and chat_ids == []


def test_two_successful_recipients_are_sent_once_each(monkeypatch, tmp_path):
    setup_output(tmp_path)
    monkeypatch.setattr("delivery.telegram.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "123, -456")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "legacy")
    transport = SequenceTransport([FakeResponse(), FakeResponse()])
    assert run_delivery(output_dir=tmp_path, transport=transport) == 0
    result = manifest(tmp_path)
    assert result["status"] == "SENT"
    assert transport.calls == ["123", "-456"]
    assert result["summary"] == {"total": 2, "sent": 2, "failed": 0}
    assert [item["chat_id"] for item in result["recipients"]] == ["123", "-456"]


def test_partial_success_attempts_remaining_recipient(monkeypatch, tmp_path):
    setup_output(tmp_path)
    monkeypatch.setattr("delivery.telegram.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "1,2,3")
    transport = SequenceTransport([FakeResponse(400, {"description": "Bad Request: chat not found"}), FakeResponse(), FakeResponse()])
    assert run_delivery(output_dir=tmp_path, transport=transport) == 1
    result = manifest(tmp_path)
    assert result["status"] == "PARTIAL"
    assert transport.calls == ["1", "2", "3"]
    assert result["summary"] == {"total": 3, "sent": 2, "failed": 1}
    assert result["recipients"][0]["error"] == "TELEGRAM_CHAT_NOT_FOUND"


def test_all_recipient_failures_are_failed(monkeypatch, tmp_path):
    setup_output(tmp_path)
    monkeypatch.setattr("delivery.telegram.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "1,2")
    transport = SequenceTransport([FakeResponse(429), FakeResponse(403)])
    assert run_delivery(output_dir=tmp_path, transport=transport) == 1
    result = manifest(tmp_path)
    assert result["status"] == "FAILED"
    assert result["summary"] == {"total": 2, "sent": 0, "failed": 2}
    assert "secret-token" not in json.dumps(result)
