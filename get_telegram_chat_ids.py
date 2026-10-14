"""Discover private Telegram chat IDs from pending bot updates."""

from __future__ import annotations

import logging
import sys
from typing import Any

import requests
from delivery.telegram import TelegramDeliveryError, TelegramTransport, load_telegram_config, map_telegram_response


def parse_private_chats(updates: Any) -> list[dict[str, str]]:
    """Extract unique private chats from message updates in stable order."""
    chats: list[dict[str, str]] = []
    seen: set[str] = set()
    if not isinstance(updates, list):
        return chats
    for update in updates:
        if not isinstance(update, dict):
            continue
        message = update.get("message")
        chat = message.get("chat") if isinstance(message, dict) else None
        if not isinstance(chat, dict) or chat.get("type") != "private" or chat.get("id") is None:
            continue
        chat_id = str(chat["id"])
        if chat_id in seen:
            continue
        seen.add(chat_id)
        chats.append({
            "chat_id": chat_id,
            "first_name": str(chat.get("first_name") or "Unknown"),
            "username": str(chat["username"]) if chat.get("username") else "",
        })
    return chats


def fetch_updates(token: str, transport: TelegramTransport | Any | None = None) -> list[dict[str, Any]]:
    """Fetch and validate Telegram updates using the shared transport."""
    transport = transport or TelegramTransport()
    try:
        response = transport.get_updates(token)
        error = map_telegram_response(response)
        if error:
            raise TelegramDeliveryError(error)
        payload = response.json()
    except TelegramDeliveryError:
        raise
    except requests.Timeout as exc:
        raise TelegramDeliveryError("TELEGRAM_TIMEOUT") from exc
    except requests.RequestException as exc:
        raise TelegramDeliveryError("TELEGRAM_NETWORK_ERROR") from exc
    except (ValueError, AttributeError) as exc:
        raise TelegramDeliveryError("TELEGRAM_API_ERROR") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise TelegramDeliveryError("TELEGRAM_API_ERROR")
    return payload.get("result", []) if isinstance(payload.get("result", []), list) else []


def print_chats(chats: list[dict[str, str]]) -> None:
    """Print only safe chat identity fields and a copyable environment value."""
    if not chats:
        print("No chats found. Make sure each recipient opened the bot and sent /start.")
        return
    print("Found Telegram chats:\n")
    for index, chat in enumerate(chats, 1):
        username = f" (@{chat['username']})" if chat.get("username") else ""
        print(f"{index}. {chat['first_name']}{username}")
        print(f"   chat_id: {chat['chat_id']}\n")
    print("Suggested .env value:")
    print(f"TELEGRAM_CHAT_IDS={','.join(chat['chat_id'] for chat in chats)}")


def main(transport: TelegramTransport | Any | None = None) -> int:
    """Discover and print private chat IDs, returning a shell-friendly status."""
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")
    token, _ = load_telegram_config()
    if not token:
        print("TELEGRAM_CONFIG_MISSING")
        return 1
    try:
        chats = parse_private_chats(fetch_updates(token, transport))
    except TelegramDeliveryError as exc:
        print(str(exc))
        return 1
    print_chats(chats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
