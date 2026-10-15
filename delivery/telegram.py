"""Manual Telegram photo delivery with a strict local publication gate."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests
from dotenv import load_dotenv

from visual.builder import READINESS_LABELS, format_date

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"
TIMEOUT = (10, 20)


class TelegramDeliveryError(RuntimeError):
    """Safe internal delivery error code; never contains credentials or raw bodies."""


class TelegramTransport:
    """Small requests-based transport that can be replaced by an offline test double."""

    def __init__(self, timeout: tuple[int, int] = TIMEOUT) -> None:
        self.timeout = timeout

    def send_photo(self, token: str, chat_id: str, image_path: Path, caption: str) -> Any:
        """Send one local PNG through Telegram Bot API."""
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        with image_path.open("rb") as image_file:
            return requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": (image_path.name, image_file, "image/png")},
                timeout=self.timeout,
            )

    def get_updates(self, token: str) -> Any:
        """Retrieve pending updates without logging the credential-bearing URL."""
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        return requests.get(url, timeout=self.timeout)


def parse_chat_ids(raw: str | None) -> list[str]:
    """Parse comma-separated IDs, preserving order and removing duplicates."""
    result: list[str] = []
    seen: set[str] = set()
    for value in (raw or "").split(","):
        chat_id = value.strip()
        if chat_id and chat_id not in seen:
            result.append(chat_id)
            seen.add(chat_id)
    return result


def resolve_chat_ids(environ: dict[str, str] | None = None) -> list[str]:
    """Prefer TELEGRAM_CHAT_IDS, falling back to the legacy single ID."""
    environ = environ or os.environ
    preferred = parse_chat_ids(environ.get("TELEGRAM_CHAT_IDS"))
    return preferred or parse_chat_ids(environ.get("TELEGRAM_CHAT_ID"))


def load_telegram_config() -> tuple[str, list[str]]:
    """Load the project .env and resolve token plus preferred recipient IDs."""
    load_dotenv(BASE_DIR / ".env")
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), resolve_chat_ids()


def mask_chat_id(chat_id: str) -> str:
    """Keep only a short suffix for safe logs."""
    return f"***{chat_id[-4:]}" if len(chat_id) > 4 else "***"


def map_telegram_response(response: Any) -> str | None:
    """Map status and safe Telegram error descriptions to internal codes."""
    status = getattr(response, "status_code", None)
    if status in (401, 403):
        return "TELEGRAM_AUTH_FAILED"
    if status == 429:
        return "TELEGRAM_RATE_LIMITED"
    if status == 400:
        try:
            payload = response.json()
        except (ValueError, AttributeError):
            payload = {}
        description = str(payload.get("description", "")).lower() if isinstance(payload, dict) else ""
        return "TELEGRAM_CHAT_NOT_FOUND" if "chat not found" in description else "TELEGRAM_API_ERROR"
    if status is None or status >= 400:
        return "TELEGRAM_API_ERROR"
    try:
        payload = response.json()
    except (ValueError, AttributeError):
        payload = {}
    if isinstance(payload, dict) and payload.get("ok") is False:
        return "TELEGRAM_API_ERROR"
    return None


def caption_from_report(report_data: dict[str, Any]) -> str:
    """Build a short public Hebrew caption from existing report fields."""
    caption = f"סיכום השוק היומי — {format_date(report_data.get('market_date'))}"
    headlines = {
        "BROADLY_POSITIVE": "וול סטריט סיימה בעליות רחבות",
        "MIXED_POSITIVE": "וול סטריט סיימה בעליות, אך לא באופן אחיד",
        "MIXED": "וול סטריט סיימה במגמה מעורבת",
        "MIXED_NEGATIVE": "וול סטריט סיימה בירידות, אך לא באופן אחיד",
        "BROADLY_NEGATIVE": "וול סטריט סיימה בירידות רחבות",
    }
    headline = headlines.get(report_data.get("market_overview", {}).get("market_direction"))
    if headline:
        caption += f"\n{headline}"
    status = report_data.get("publication_readiness", {}).get("status")
    if status == "READY_WITH_WARNINGS":
        caption += f"\n{READINESS_LABELS[status]}"
    return caption


def write_delivery_manifest(payload: dict[str, Any], output_dir: Path) -> Path:
    """Write only safe delivery state, never credentials or raw Telegram responses."""
    path = output_dir / "daily_report_delivery.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _recipient_result(durable: bool, slot: str | None, chat_id: str, status: str, error: str | None = None) -> dict[str, str]:
    """Preserve the local manifest while cloud mode uses only HMAC slots."""
    result: dict[str, str] = {"recipient_slot": str(slot)} if durable and slot else ({"chat_id": chat_id} if not durable else {})
    result["status"] = status
    if error:
        result["error"] = error
    return result


def run_delivery(
    *,
    dry_run: bool = False,
    transport: TelegramTransport | Any | None = None,
    output_dir: Path | None = None,
    logger: logging.Logger | None = None,
    durable_ledger: Any | None = None,
) -> int:
    """Run one manual delivery attempt, returning zero only for send/dry-run success."""
    output_dir = output_dir or OUTPUT_DIR
    logger = logger or logging.getLogger("market_bot.delivery")
    logger.info("Telegram delivery started")
    v3_manifest = output_dir / "daily_report_v3_visual_validation.json"
    v3_image = output_dir / "daily_report_card_v3.png"
    manifest_path = v3_manifest if v3_manifest.is_file() else output_dir / "daily_report_visual_validation.json"
    image_path = v3_image if v3_image.is_file() else output_dir / "daily_report_card.png"
    report_path = output_dir / "daily_report_data.json"

    try:
        validation = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.error("REPORT_NOT_PUBLISHABLE")
        write_delivery_manifest({"channel": "telegram", "status": "FAILED", "error": "REPORT_NOT_PUBLISHABLE"}, output_dir)
        return 1
    if validation.get("valid") is not True:
        logger.error("REPORT_NOT_PUBLISHABLE")
        write_delivery_manifest({"channel": "telegram", "status": "FAILED", "error": "REPORT_NOT_PUBLISHABLE"}, output_dir)
        return 1
    logger.info("Visual report publishability verified")

    if not image_path.is_file() or not os.access(image_path, os.R_OK):
        logger.error("REPORT_IMAGE_MISSING")
        write_delivery_manifest({"channel": "telegram", "status": "FAILED", "error": "TELEGRAM_IMAGE_MISSING"}, output_dir)
        return 1
    logger.info("Report image verified")
    try:
        report_data = json.loads(report_path.read_text(encoding="utf-8"))
        caption = caption_from_report(report_data)
    except (OSError, ValueError, KeyError):
        logger.error("REPORT_DATA_INVALID")
        write_delivery_manifest({"channel": "telegram", "status": "FAILED", "error": "REPORT_DATA_INVALID"}, output_dir)
        return 1

    token, chat_ids = load_telegram_config()
    logger.info("Telegram config loaded: token=%s, recipients=%d", "yes" if token else "no", len(chat_ids))
    if dry_run:
        if chat_ids:
            logger.info("Telegram dry-run: WOULD_SEND to %d recipients", len(chat_ids))
        else:
            logger.info("Telegram dry-run: WOULD_SEND_CONFIG_PENDING")
        write_delivery_manifest({"channel": "telegram", "status": "WOULD_SEND", "report_date": report_data.get("market_date"), "image": image_path.name, "recipients": [{"chat_id": chat_id, "status": "WOULD_SEND"} for chat_id in chat_ids], "summary": {"total": len(chat_ids), "sent": 0, "failed": 0}}, output_dir)
        return 0

    if not token or not chat_ids:
        logger.error("TELEGRAM_CONFIG_MISSING")
        write_delivery_manifest({"channel": "telegram", "status": "FAILED", "error": "TELEGRAM_CONFIG_MISSING"}, output_dir)
        return 1

    transport = transport or TelegramTransport()
    results: list[dict[str, str]] = []
    market_date = str(report_data.get("market_date") or "")
    if durable_ledger is not None and not market_date:
        logger.error("DURABLE_DELIVERY_MARKET_DATE_MISSING")
        write_delivery_manifest({"channel": "telegram", "status": "FAILED", "error": "DURABLE_DELIVERY_MARKET_DATE_MISSING"}, output_dir)
        return 1
    for index, chat_id in enumerate(chat_ids, 1):
        logger.info("Recipient %d/%d delivery started", index, len(chat_ids))
        slot_id = None
        try:
            if durable_ledger is not None:
                slot_id = durable_ledger.slot_id(chat_id)
                try:
                    durable_ledger.begin_send(market_date, chat_id)
                except Exception as exc:
                    # A pre-send publication/duplicate-state failure must block
                    # transport invocation. The durable slot remains non-sendable.
                    results.append({"recipient_slot": slot_id, "chat_id": None, "status": "BLOCKED", "error": type(exc).__name__})
                    logger.error("Recipient %d/%d blocked before send: %s", index, len(chat_ids), type(exc).__name__)
                    continue
            response = transport.send_photo(token, chat_id, image_path, caption)
            error = map_telegram_response(response)
            if error:
                if durable_ledger is not None and slot_id:
                    durable_ledger.record_outcome(market_date, slot_id, "FAILED_SAFE", safe_error=error)
                raise TelegramDeliveryError(error)
            if durable_ledger is not None and slot_id:
                durable_ledger.record_outcome(market_date, slot_id, "SENT")
            results.append(_recipient_result(durable_ledger is not None, slot_id, chat_id, "SENT"))
            logger.info("Recipient %d/%d sent", index, len(chat_ids))
        except TelegramDeliveryError as exc:
            results.append(_recipient_result(durable_ledger is not None, slot_id, chat_id, "FAILED_SAFE" if durable_ledger is not None else "FAILED", str(exc)))
            logger.error("Recipient %d/%d failed: %s", index, len(chat_ids), str(exc))
        except requests.Timeout:
            if durable_ledger is not None and slot_id:
                durable_ledger.record_outcome(market_date, slot_id, "UNKNOWN", safe_error="TELEGRAM_TIMEOUT")
            results.append(_recipient_result(durable_ledger is not None, slot_id, chat_id, "UNKNOWN" if durable_ledger is not None else "FAILED", "TELEGRAM_TIMEOUT"))
            logger.error("Recipient %d/%d failed: TELEGRAM_TIMEOUT", index, len(chat_ids))
        except requests.RequestException:
            if durable_ledger is not None and slot_id:
                durable_ledger.record_outcome(market_date, slot_id, "UNKNOWN", safe_error="TELEGRAM_NETWORK_ERROR")
            results.append(_recipient_result(durable_ledger is not None, slot_id, chat_id, "UNKNOWN" if durable_ledger is not None else "FAILED", "TELEGRAM_NETWORK_ERROR"))
            logger.error("Recipient %d/%d failed: TELEGRAM_NETWORK_ERROR", index, len(chat_ids))
        except Exception:
            if durable_ledger is not None and slot_id:
                # The send call may have reached Telegram before an unexpected
                # local exception; preserve the no-duplicate safety invariant.
                try:
                    durable_ledger.record_outcome(market_date, slot_id, "UNKNOWN", safe_error="TELEGRAM_API_ERROR")
                except Exception:
                    logger.error("Recipient %d/%d outcome persistence failed; durable state remains non-sendable", index, len(chat_ids))
            results.append(_recipient_result(durable_ledger is not None, slot_id, chat_id, "UNKNOWN" if durable_ledger is not None else "FAILED", "TELEGRAM_API_ERROR"))
            logger.error("Recipient %d/%d failed: TELEGRAM_API_ERROR", index, len(chat_ids))

    sent = sum(result["status"] == "SENT" for result in results)
    failed = len(results) - sent
    status = "SENT" if failed == 0 else "FAILED" if sent == 0 else "PARTIAL"
    if durable_ledger is not None and any(result.get("status") == "UNKNOWN" for result in results):
        status = "UNKNOWN"
    logger.info("Telegram delivery completed: %s", status)
    write_delivery_manifest({"channel": "telegram", "status": status, "sent_at": datetime.now(timezone.utc).isoformat(), "report_date": report_data.get("market_date"), "image": image_path.name, "recipients": results, "summary": {"total": len(results), "sent": sent, "failed": failed}}, output_dir)
    return 0 if status == "SENT" else 1
