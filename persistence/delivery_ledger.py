"""Crash-conservative per-date Telegram delivery state machine."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any

from persistence.github_state import GitHubStateStore


class DeliveryClaimDenied(RuntimeError):
    """This recipient slot is already claimed, sent, or requires review."""


class DeliveryLedger:
    """Persist recipient-slot claims before the transport can be invoked."""

    def __init__(self, store: GitHubStateStore, hmac_key: str | bytes, *, clock=lambda: datetime.now(timezone.utc)) -> None:
        key = hmac_key.encode("utf-8") if isinstance(hmac_key, str) else bytes(hmac_key)
        if len(key) < 32:
            raise ValueError("TELEGRAM_RECIPIENT_HMAC_KEY_TOO_SHORT")
        self.store = store
        self._key = key
        self.clock = clock

    def slot_id(self, chat_id: str) -> str:
        """Return a stable opaque identifier; the input is never persisted."""
        digest = hmac.new(self._key, str(chat_id).encode("utf-8"), hashlib.sha256).hexdigest()
        return f"hmac256:{digest}"

    def begin_send(self, market_date: str, chat_id: str, *, reviewed_retry: bool = False) -> str:
        """Durably publish CLAIMED then SENDING before the caller sends bytes."""
        slot = self.slot_id(chat_id)
        timestamp = self._now()
        recovered_unknown = False

        def claim(state: dict[str, Any]) -> None:
            nonlocal recovered_unknown
            record = state["deliveries"].setdefault(market_date, {"recipients": {}})
            slots = record["recipients"]
            current = slots.get(slot, {"status": "PENDING"})
            status = current["status"]
            if status == "SENDING":
                current.update({"status": "UNKNOWN", "updated_at": timestamp, "safe_error": "RECOVERED_IN_FLIGHT_AFTER_RESTART"})
                recovered_unknown = True
                return
            if status == "FAILED_SAFE" and reviewed_retry:
                pass
            elif status != "PENDING":
                raise DeliveryClaimDenied(f"DELIVERY_SLOT_NOT_SENDABLE:{status}")
            slots[slot] = {"status": "CLAIMED", "claimed_at": timestamp, "attempt": int(current.get("attempt", 0)) + 1}

        self.store.update(claim)
        if recovered_unknown:
            raise DeliveryClaimDenied("DELIVERY_SLOT_RECOVERED_AS_UNKNOWN")

        def sending(state: dict[str, Any]) -> None:
            recipient = state["deliveries"][market_date]["recipients"][slot]
            if recipient.get("status") != "CLAIMED":
                raise DeliveryClaimDenied("DELIVERY_CLAIM_STATE_CHANGED")
            recipient.update({"status": "SENDING", "send_started_at": self._now()})

        self.store.update(sending)
        return slot

    def record_outcome(self, market_date: str, slot: str, status: str, *, safe_error: str | None = None) -> None:
        """Persist SENT, UNKNOWN or definitive FAILED_SAFE immediately."""
        if status not in {"SENT", "UNKNOWN", "FAILED_SAFE"}:
            raise ValueError("DELIVERY_OUTCOME_INVALID")

        def mutate(state: dict[str, Any]) -> None:
            try:
                recipient = state["deliveries"][market_date]["recipients"][slot]
            except KeyError:
                raise DeliveryClaimDenied("DELIVERY_SLOT_MISSING") from None
            if recipient.get("status") != "SENDING":
                raise DeliveryClaimDenied("DELIVERY_NOT_IN_SENDING_STATE")
            recipient["status"] = status
            recipient["updated_at"] = self._now()
            if safe_error:
                recipient["safe_error"] = safe_error[:80]

        self.store.update(mutate)

    def reconcile_unknown(self, market_date: str, slot: str, *, confirmed_sent: bool, reviewer: str, reason: str) -> None:
        """Apply an explicit human-reviewed resolution; never infer from lease age."""
        if not reviewer.strip() or not reason.strip():
            raise ValueError("DELIVERY_RECONCILIATION_REVIEW_REQUIRED")
        reviewer_ref = self.slot_id("reviewer:" + reviewer.strip())
        reason_code = "OPERATOR_CONFIRMED_SENT" if confirmed_sent else "OPERATOR_CONFIRMED_NOT_SENT"

        def mutate(state: dict[str, Any]) -> None:
            try:
                recipient = state["deliveries"][market_date]["recipients"][slot]
            except KeyError:
                raise DeliveryClaimDenied("DELIVERY_SLOT_MISSING") from None
            if recipient.get("status") != "UNKNOWN":
                raise DeliveryClaimDenied("DELIVERY_SLOT_NOT_UNKNOWN")
            recipient.update({
                "status": "SENT" if confirmed_sent else "FAILED_SAFE",
                "updated_at": self._now(),
                "reconciliation": {"reviewer_ref": reviewer_ref, "reason_code": reason_code},
            })

        self.store.update(mutate)

    def _now(self) -> str:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("DELIVERY_CLOCK_TIMEZONE_REQUIRED")
        return value.astimezone(timezone.utc).isoformat()
