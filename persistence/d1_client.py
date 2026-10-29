"""Fail-closed client for the narrowly scoped Market Bot D1 Worker API."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import requests


class D1StateError(RuntimeError):
    """The remote durable state API could not safely authorize an operation."""

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        operation_id: str | None = None,
        http_status: int | None = None,
        failure_category: str | None = None,
        may_have_committed: bool = False,
        attempts: int | None = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.operation_id = operation_id
        self.http_status = http_status
        self.failure_category = failure_category
        self.may_have_committed = may_have_committed
        self.attempts = attempts

    @property
    def sanitized_operation_id(self) -> str | None:
        if not self.operation_id:
            return None
        return f"{self.operation_id[:4]}…{self.operation_id[-4:]}"


class D1OperationUncertain(D1StateError):
    """A write may have committed, so no external side effect is authorized."""


class D1QuotaExhausted(D1StateError):
    """Worker rejected a quota reservation at a configured limit."""


class D1DeliveryDenied(D1StateError):
    """The durable delivery state does not authorize another Telegram send."""


class D1DeliveryAttempt(str):
    """String-compatible recipient slot with its immutable send authorization."""

    def __new__(cls, slot: str, attempt_id: str):
        result = super().__new__(cls, slot)
        object.__setattr__(result, "_attempt_id", attempt_id)
        return result

    def __setattr__(self, name, value):
        raise AttributeError("Delivery attempt authorization is immutable")

    @property
    def attempt_id(self) -> str:
        return self._attempt_id


class D1StateClient:
    """HTTP adapter; mutation retries reuse an operation ID and never reauthorize."""

    def __init__(
        self,
        url: str,
        secret: str,
        *,
        admin_secret: str | None = None,
        timeout: tuple[float, float] = (3.05, 8.0),
        session: Any = requests,
    ) -> None:
        endpoint = str(url).strip().rstrip("/")
        if not endpoint.startswith("https://"):
            raise D1StateError("D1_WORKER_HTTPS_REQUIRED")
        if len(str(secret).encode("utf-8")) < 32:
            raise D1StateError("D1_WORKER_SECRET_INVALID")
        if admin_secret is not None and len(admin_secret.encode("utf-8")) < 32:
            raise D1StateError("D1_ADMIN_SECRET_INVALID")
        if len(timeout) != 2 or min(timeout) <= 0:
            raise D1StateError("D1_TIMEOUT_INVALID")
        self.url = endpoint + "/v1/state"
        self._secret = secret.encode("utf-8")
        self._admin_secret = admin_secret.encode("utf-8") if admin_secret else None
        self.timeout = timeout
        self.session = session

    @classmethod
    def from_env(cls) -> "D1StateClient":
        url = os.getenv("MARKETBOT_D1_STATE_URL", "").strip()
        secret = os.getenv("MARKETBOT_D1_STATE_HMAC_SECRET", "")
        admin_secret = os.getenv("MARKETBOT_D1_STATE_ADMIN_HMAC_SECRET", "") or None
        if not url or not secret:
            raise D1StateError("D1_WORKER_CONFIGURATION_MISSING")
        return cls(url, secret, admin_secret=admin_secret)

    def call(self, op: str, payload: dict[str, Any], *, operation_id: str | None = None, admin: bool = False) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z][a-z0-9_.]{1,63}", op):
            raise D1StateError("D1_OPERATION_INVALID")
        if admin and self._admin_secret is None:
            raise D1StateError("D1_ADMIN_CONFIGURATION_MISSING")
        op_id = operation_id or uuid.uuid4().hex
        if not re.fullmatch(r"[0-9a-f]{32}", op_id):
            raise D1StateError("D1_OPERATION_ID_INVALID")
        body = json.dumps(
            {"schema_version": 1, "operation_id": op_id, "op": op, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        secret = self._admin_secret if admin else self._secret
        assert secret is not None
        is_mutation = op != "state.get"

        def failure(
            error_type: type[D1StateError],
            message: str,
            *,
            category: str,
            status: int | None = None,
            may_commit: bool | None = None,
            attempts: int,
        ) -> D1StateError:
            return error_type(
                message,
                operation=op,
                operation_id=op_id,
                http_status=status,
                failure_category=category,
                may_have_committed=is_mutation if may_commit is None else may_commit,
                attempts=attempts,
            )

        last_error: Exception | None = None
        for attempt in range(2):
            timestamp = str(int(datetime.now(timezone.utc).timestamp()))
            nonce = uuid.uuid4().hex
            signature = hmac.new(secret, timestamp.encode() + b"." + nonce.encode() + b"." + body, hashlib.sha256).hexdigest()
            headers = {
                "Content-Type": "application/json",
                "X-MB-Timestamp": timestamp,
                "X-MB-Nonce": nonce,
                "X-MB-Admin-Signature" if admin else "X-MB-Signature": signature,
            }
            try:
                response = self.session.post(self.url, data=body, headers=headers, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempt == 0:
                    continue
                category = "HTTP_TIMEOUT" if isinstance(exc, requests.Timeout) else "NETWORK_ERROR"
                raise failure(D1OperationUncertain, "D1_WORKER_RESPONSE_UNCERTAIN", category=category, attempts=attempt + 1) from None
            try:
                data = response.json()
            except (ValueError, AttributeError):
                if response.status_code >= 500 and attempt == 0:
                    continue
                raise failure(D1StateError, "D1_WORKER_RESPONSE_INVALID", category="RESPONSE_PARSE_ERROR", status=getattr(response, "status_code", None), attempts=attempt + 1) from None
            if not isinstance(data, dict):
                raise failure(D1StateError, "D1_WORKER_RESPONSE_INVALID", category="RESPONSE_PARSE_ERROR", status=response.status_code, attempts=attempt + 1)
            if response.status_code >= 500 and attempt == 0:
                continue
            if response.status_code == 429 and data.get("error") == "QUOTA_EXHAUSTED":
                raise failure(D1QuotaExhausted, "TWELVE_DATA_DURABLE_QUOTA_EXHAUSTED", category="HTTP_429_QUOTA_EXHAUSTED", status=429, may_commit=False, attempts=attempt + 1)
            if response.status_code == 403 and data.get("error") == "CLOUD_OWNERSHIP_GATE_CLOSED":
                raise failure(D1StateError, "D1_OWNERSHIP_GATE_CLOSED", category="OWNERSHIP_GATE", status=403, may_commit=False, attempts=attempt + 1)
            if response.status_code == 409 and op == "delivery.begin" and data.get("authorized") is False and data.get("replayed") is False:
                # The Worker records the operation ID but does not authorize a
                # delivery transition when the recipient slot is not sendable.
                raise failure(D1DeliveryDenied, "DELIVERY_SLOT_NOT_SENDABLE", category="DELIVERY_STATE_DENIAL", status=409, may_commit=True, attempts=attempt + 1)
            if response.status_code >= 400:
                code = data.get("error")
                if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_]{1,80}", code):
                    code = "D1_WORKER_REQUEST_REJECTED"
                if code in {"SCHEMA_VERSION_UNSUPPORTED", "SCHEMA_MISMATCH", "D1_SCHEMA_MISMATCH"}:
                    category = "SCHEMA_MISMATCH"
                elif response.status_code in {401, 403}:
                    category = "AUTHENTICATION_OR_AUTHORIZATION"
                elif response.status_code >= 500:
                    category = "HTTP_5XX_WORKER_ERROR"
                else:
                    category = "HTTP_4XX_WORKER_REJECTION"
                may_commit = is_mutation and (response.status_code >= 500 or response.status_code == 409)
                raise failure(D1StateError, code, category=category, status=response.status_code, may_commit=may_commit, attempts=attempt + 1)
            if data.get("replayed") is True:
                # Replayed operation results never authorize an external HTTP call.
                if op in {"quota.reserve", "delivery.begin", "run.claim"}:
                    data["authorized"] = False
                    data["claim_authorized"] = False
                    if op in {"quota.reserve", "delivery.begin"}:
                        raise failure(D1OperationUncertain, "D1_OPERATION_ALREADY_COMMITTED_NO_REPLAY_AUTHORIZATION", category="REPLAY_INDICATES_PRIOR_COMMIT", status=response.status_code, may_commit=True, attempts=attempt + 1)
            return data
        raise failure(D1OperationUncertain, "D1_WORKER_RESPONSE_UNCERTAIN", category="HTTP_5XX_OR_INVALID_RESPONSE", attempts=2) from last_error

    def read(self, *, fetch: bool = False) -> dict[str, Any]:
        del fetch  # D1 is authoritative; there is no separate fetch phase.
        data = self.call("state.get", {})
        if data.get("schema_version") != 1 or not isinstance(data.get("runs"), dict) or not isinstance(data.get("ownership"), dict):
            raise D1StateError("D1_STATE_SCHEMA_INVALID")
        return data

    def reserve_quota(self, credits: int = 1) -> str:
        op_id = uuid.uuid4().hex
        result = self.call("quota.reserve", {"provider": "twelve_data", "credits": credits}, operation_id=op_id)
        if result.get("authorized") is not True or result.get("reservation_id") != op_id:
            raise D1OperationUncertain("D1_QUOTA_RESERVATION_NOT_AUTHORIZED")
        return op_id

    def claim_run(self, market_date: str, run_id: str) -> dict[str, Any]:
        result = self.call("run.claim", {"market_date": market_date, "run_id": run_id})
        if result.get("claim_authorized") is not True:
            raise D1OperationUncertain("D1_RUN_CLAIM_NOT_AUTHORIZED")
        return result

    def transition_run(self, market_date: str, run_id: str, old: str, new: str, *, reason_code: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"market_date": market_date, "run_id": run_id, "from": old, "to": new}
        if reason_code is not None:
            payload["reason_code"] = reason_code
        return self.call("run.transition", payload)

    def recover_run(self, market_date: str, run_id: str, *, sender_stopped: bool = False) -> dict[str, Any]:
        if sender_stopped is not True:
            raise D1StateError("SENDER_STOP_CONFIRMATION_REQUIRED")
        result = self.call("run.recover", {"market_date": market_date, "run_id": run_id, "sender_stopped": True}, admin=True)
        if result.get("applied") is not True:
            raise D1StateError("D1_RUN_RECOVERY_NOT_APPLIED")
        return result

    def set_ownership(self, *, cloud_quota_exclusive: bool, windows_sender_disabled: bool, cloud_delivery_enabled: bool) -> dict[str, Any]:
        return self.call("ownership.set", {"cloud_quota_exclusive": cloud_quota_exclusive, "windows_sender_disabled": windows_sender_disabled, "cloud_delivery_enabled": cloud_delivery_enabled}, admin=True)

    def prune(self) -> dict[str, Any]:
        return self.call("admin.prune", {}, admin=True)


class D1QuotaLedger:
    """Callback-compatible durable quota adapter for the existing limiter."""

    def __init__(self, client: D1StateClient) -> None:
        self.client = client

    def reserve(self, credits: int = 1) -> str:
        return self.client.reserve_quota(credits)

    def __call__(self, credits: int = 1) -> str:
        return self.reserve(credits)


class D1DeliveryLedger:
    """Method-compatible Telegram ledger storing only HMAC recipient identities."""

    def __init__(self, client: D1StateClient, hmac_key: str | bytes) -> None:
        key = hmac_key.encode("utf-8") if isinstance(hmac_key, str) else bytes(hmac_key)
        if len(key) < 32:
            raise ValueError("TELEGRAM_RECIPIENT_HMAC_KEY_TOO_SHORT")
        self.client = client
        self._key = key

    def slot_id(self, chat_id: str) -> str:
        digest = hmac.new(self._key, str(chat_id).encode("utf-8"), hashlib.sha256).hexdigest()
        return f"hmac256:{digest}"

    def begin_send(self, market_date: str, chat_id: str, *, reviewed_retry: bool = False, sender_stopped: bool = False) -> str:
        slot = self.slot_id(chat_id)
        if reviewed_retry and sender_stopped is not True:
            raise D1DeliveryDenied("SENDER_STOP_CONFIRMATION_REQUIRED")
        attempt_id = uuid.uuid4().hex
        payload = {"market_date": market_date, "recipient_hmac": slot, "attempt_id": attempt_id, "reviewed_retry": reviewed_retry}
        if reviewed_retry:
            payload["sender_stopped"] = True
        result = self.client.call("delivery.begin", payload, admin=reviewed_retry)
        if result.get("authorized") is not True or result.get("attempt_id") != attempt_id:
            raise D1OperationUncertain("D1_DELIVERY_AUTHORIZATION_INVALID", may_have_committed=True)
        return D1DeliveryAttempt(slot, attempt_id)

    def record_outcome(self, market_date: str, slot: str, status: str, *, safe_error: str | None = None) -> None:
        if status not in {"SENT", "UNKNOWN", "FAILED_SAFE"}:
            raise ValueError("DELIVERY_OUTCOME_INVALID")
        attempt_id = getattr(slot, "attempt_id", None)
        if not attempt_id:
            raise D1DeliveryDenied("DELIVERY_ATTEMPT_MISSING")
        payload: dict[str, Any] = {"market_date": market_date, "recipient_hmac": str(slot), "attempt_id": attempt_id, "status": status}
        if safe_error:
            payload["safe_error"] = safe_error
        result = self.client.call("delivery.outcome", payload)
        if result.get("applied") is not True:
            raise D1DeliveryDenied("DELIVERY_OUTCOME_NOT_APPLIED")

    def read_recipient(self, market_date: str, slot: str) -> dict[str, Any] | None:
        """Read one HMAC slot; never infer non-delivery from an absent row."""
        result = self.client.call("state.get", {"market_date": market_date, "recipient_hmac": str(slot)})
        if result.get("schema_version") != 1 or result.get("delivery_protocol_version") != 2:
            raise D1StateError("D1_DELIVERY_READ_UNSUPPORTED")
        if "delivery_recipient" not in result:
            raise D1StateError("D1_DELIVERY_READ_UNSUPPORTED")
        row = result["delivery_recipient"]
        if row is None:
            return None
        if not isinstance(row, dict) or row.get("status") not in {"SENDING", "SENT", "UNKNOWN", "FAILED_SAFE"} or not re.fullmatch(r"[0-9a-f]{32}", str(row.get("attempt_id", ""))):
            raise D1StateError("D1_DELIVERY_STATE_INVALID")
        return row

    def resolve_outcome(self, market_date: str, slot: str) -> str:
        """Resolve only this attempt's committed result, without any new send."""
        row = self.read_recipient(market_date, slot)
        if row is None or row.get("attempt_id") != getattr(slot, "attempt_id", None):
            return "UNKNOWN"
        return "UNKNOWN" if row["status"] == "SENDING" else row["status"]

    def begin_failure_status(self, market_date: str, slot: str, error: Exception) -> str:
        row = self.read_recipient(market_date, slot)
        if row and row["status"] in {"SENDING", "UNKNOWN"}:
            return "UNKNOWN"
        if isinstance(error, D1DeliveryDenied) or (isinstance(error, D1StateError) and not error.may_have_committed and not isinstance(error, D1OperationUncertain)):
            return "BLOCKED"
        return "UNKNOWN"

    def reconcile_unknown(self, market_date: str, slot: str, *, confirmed_sent: bool, reviewer: str, reason: str) -> None:
        if not reviewer.strip() or not reason.strip():
            raise ValueError("DELIVERY_RECONCILIATION_REVIEW_REQUIRED")
        reviewer_hmac = self.slot_id("reviewer:" + reviewer.strip())
        result = self.client.call("delivery.reconcile", {"market_date": market_date, "recipient_hmac": str(slot), "reviewer_hmac": reviewer_hmac, "confirmed_sent": confirmed_sent}, admin=True)
        if result.get("applied") is not True:
            raise D1DeliveryDenied("DELIVERY_RECONCILIATION_NOT_APPLIED")
