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


def classify_telegram_response(response: Any, *, expect_message: bool = True) -> tuple[str, str | None]:
    """Require a complete sendPhoto acknowledgment; ambiguity is never safe failure."""
    status = getattr(response, "status_code", None)
    if not isinstance(status, int) or isinstance(status, bool) or status >= 500:
        return "UNKNOWN", "TELEGRAM_SERVER_ERROR" if isinstance(status, int) and status >= 500 else "TELEGRAM_RESPONSE_INVALID"
    try:
        payload = response.json()
    except (ValueError, AttributeError):
        return "UNKNOWN", "TELEGRAM_RESPONSE_INVALID"
    if not isinstance(payload, dict):
        return "UNKNOWN", "TELEGRAM_RESPONSE_INVALID"
    if status in {400, 401, 403, 404, 429} and payload.get("ok") is False and type(payload.get("error_code")) is int and payload["error_code"] == status:
        if status in {401, 403}:
            error = "TELEGRAM_AUTH_FAILED"
        elif status == 429:
            error = "TELEGRAM_RATE_LIMITED"
        elif status == 400 and "chat not found" in str(payload.get("description", "")).lower():
            error = "TELEGRAM_CHAT_NOT_FOUND"
        else:
            error = "TELEGRAM_API_ERROR"
        return "FAILED_SAFE", error
    result = payload.get("result")
    message_valid = isinstance(result, dict) and type(result.get("message_id")) is int and result["message_id"] > 0
    if 200 <= status < 300 and payload.get("ok") is True and (message_valid or not expect_message):
        return "SENT", None
    return "UNKNOWN", "TELEGRAM_RESPONSE_INVALID"


def map_telegram_response(response: Any) -> str | None:
    """Shared non-delivery API error mapping (for example getUpdates)."""
    status = getattr(response, "status_code", None)
    if status in {401, 403}:
        return "TELEGRAM_AUTH_FAILED"
    if status == 429:
        return "TELEGRAM_RATE_LIMITED"
    return classify_telegram_response(response, expect_message=False)[1]


def _persist_outcome(ledger: Any, market_date: str, slot: str, status: str, error: str | None, logger: logging.Logger) -> str:
    """A lost state acknowledgment never authorizes another Telegram request."""
    try:
        ledger.record_outcome(market_date, slot, status, safe_error=error)
        return status
    except Exception:
        if hasattr(ledger, "resolve_outcome"):
            try:
                resolved = ledger.resolve_outcome(market_date, slot)
                # Only an authoritative result for this attempt can settle it.
                if resolved == "SENT" or resolved == "UNKNOWN" or (resolved == "FAILED_SAFE" and status == "FAILED_SAFE"):
                    logger.info("Durable delivery outcome reconciled: %s", resolved)
                    return resolved
            except Exception:
                pass
        logger.error("Delivery outcome unresolved; recipient remains non-sendable")
        return "UNKNOWN"


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
    if not dry_run and durable_ledger is None and (os.getenv("MARKETBOT_STATE_BACKEND", "").strip().lower() == "d1" or os.getenv("MARKETBOT_PERSISTENT_DELIVERY_MODE") == "1"):
        logger.error("DURABLE_DELIVERY_LEDGER_REQUIRED")
        write_delivery_manifest({"channel": "telegram", "status": "FAILED", "error": "DURABLE_DELIVERY_LEDGER_REQUIRED"}, output_dir)
        return 1
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
    # .env loading can select cloud mode after the initial entry-point guard.
    if not dry_run and durable_ledger is None and (os.getenv("MARKETBOT_STATE_BACKEND", "").strip().lower() == "d1" or os.getenv("MARKETBOT_PERSISTENT_DELIVERY_MODE") == "1"):
        logger.error("DURABLE_DELIVERY_LEDGER_REQUIRED")
        write_delivery_manifest({"channel": "telegram", "status": "FAILED", "error": "DURABLE_DELIVERY_LEDGER_REQUIRED"}, output_dir)
        return 1
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
                    slot_id = durable_ledger.begin_send(market_date, chat_id)
                except Exception as exc:
                    # A pre-send publication/duplicate-state failure must block
                    # transport invocation. The durable slot remains non-sendable.
                    status = "BLOCKED"
                    if hasattr(durable_ledger, "begin_failure_status"):
                        try:
                            status = durable_ledger.begin_failure_status(market_date, slot_id, exc)
                        except Exception:
                            status = "UNKNOWN"
                    results.append({"recipient_slot": slot_id, "status": status, "error": type(exc).__name__})
                    logger.error("Recipient %d/%d blocked before send: %s", index, len(chat_ids), type(exc).__name__)
                    continue
            response = transport.send_photo(token, chat_id, image_path, caption)
            status, error = classify_telegram_response(response)
            if durable_ledger is not None and slot_id:
                status = _persist_outcome(durable_ledger, market_date, slot_id, status, error, logger)
            results.append(_recipient_result(durable_ledger is not None, slot_id, chat_id, status, error))
            logger.info("Recipient %d/%d outcome: %s", index, len(chat_ids), status)
        except requests.Timeout:
            status = "UNKNOWN"
            if durable_ledger is not None and slot_id:
                status = _persist_outcome(durable_ledger, market_date, slot_id, "UNKNOWN", "TELEGRAM_TIMEOUT", logger)
            results.append(_recipient_result(durable_ledger is not None, slot_id, chat_id, status, "TELEGRAM_TIMEOUT"))
            logger.error("Recipient %d/%d failed: TELEGRAM_TIMEOUT", index, len(chat_ids))
        except requests.RequestException:
            status = "UNKNOWN"
            if durable_ledger is not None and slot_id:
                status = _persist_outcome(durable_ledger, market_date, slot_id, "UNKNOWN", "TELEGRAM_NETWORK_ERROR", logger)
            results.append(_recipient_result(durable_ledger is not None, slot_id, chat_id, status, "TELEGRAM_NETWORK_ERROR"))
            logger.error("Recipient %d/%d failed: TELEGRAM_NETWORK_ERROR", index, len(chat_ids))
        except Exception:
            status = "UNKNOWN"
            if durable_ledger is not None and slot_id:
                # The send call may have reached Telegram before an unexpected
                # local exception; preserve the no-duplicate safety invariant.
                status = _persist_outcome(durable_ledger, market_date, slot_id, "UNKNOWN", "TELEGRAM_API_ERROR", logger)
            results.append(_recipient_result(durable_ledger is not None, slot_id, chat_id, status, "TELEGRAM_API_ERROR"))
            logger.error("Recipient %d/%d failed: TELEGRAM_API_ERROR", index, len(chat_ids))

    sent = sum(result["status"] == "SENT" for result in results)
    failed = len(results) - sent
    status = "SENT" if failed == 0 else "FAILED" if sent == 0 else "PARTIAL"
    if any(result.get("status") == "UNKNOWN" for result in results):
        status = "UNKNOWN"
    logger.info("Telegram delivery completed: %s", status)
    write_delivery_manifest({"channel": "telegram", "status": status, "sent_at": datetime.now(timezone.utc).isoformat(), "report_date": report_data.get("market_date"), "image": image_path.name, "recipients": results, "summary": {"total": len(results), "sent": sent, "failed": failed}}, output_dir)
    return 0 if status == "SENT" else 1
