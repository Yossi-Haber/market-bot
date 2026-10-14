"""Offline tests for Telegram private-chat discovery."""

import json

from get_telegram_chat_ids import main, parse_private_chats


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload or {"ok": True, "result": []}

    def json(self):
        return self.payload


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get_updates(self, token):
        self.calls.append(token)
        return self.response


def test_missing_token(monkeypatch, capsys):
    monkeypatch.setattr("delivery.telegram.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert main(FakeTransport(FakeResponse())) == 1
    assert capsys.readouterr().out.strip() == "TELEGRAM_CONFIG_MISSING"


def test_successful_parsing_and_deduplication(monkeypatch, capsys):
    monkeypatch.setattr("delivery.telegram.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 123, "type": "private", "first_name": "Yossi", "username": "yossi"}}},
        {"update_id": 2, "message": {"chat": {"id": 123, "type": "private", "first_name": "Yossi"}}},
        {"update_id": 3, "message": {"chat": {"id": -456, "type": "private", "first_name": "Brother"}}},
    ]
    transport = FakeTransport(FakeResponse(payload={"ok": True, "result": updates}))
    assert main(transport) == 0
    output = capsys.readouterr().out
    assert "Yossi (@yossi)" in output
    assert "TELEGRAM_CHAT_IDS=123,-456" in output
    assert "secret-token" not in output
    assert transport.calls == ["secret-token"]


def test_no_updates_prints_guidance(capsys):
    print_result = parse_private_chats([])
    assert print_result == []
    from get_telegram_chat_ids import print_chats
    print_chats(print_result)
    assert "No chats found" in capsys.readouterr().out


def test_malformed_and_non_message_updates_are_ignored():
    updates = [{"update_id": 1}, {"callback_query": {}}, {"message": {}}, {"message": {"chat": {"id": 1, "type": "group"}}}]
    assert parse_private_chats(updates) == []


def test_auth_failure_is_sanitized(monkeypatch, capsys):
    monkeypatch.setattr("delivery.telegram.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    assert main(FakeTransport(FakeResponse(401, {"ok": False, "description": "Unauthorized secret-token"}))) == 1
    output = capsys.readouterr().out
    assert output.strip() == "TELEGRAM_AUTH_FAILED"
    assert "secret-token" not in output
