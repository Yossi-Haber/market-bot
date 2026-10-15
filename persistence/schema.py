"""Versioned, bounded and privacy-safe schema for the cloud state branch."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

SCHEMA_VERSION = 1
MAX_STATE_BYTES = 1_000_000
MAX_RUN_DATES = 400
MAX_DELIVERY_DATES = 400
MAX_RESERVATIONS = 500
MAX_STATE_AGE = timedelta(days=30)
RECIPIENT_SLOT_RE = re.compile(r"^hmac256:[0-9a-f]{64}$")
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SAFE_CODE_RE = re.compile(r"^[A-Z0-9_]{1,80}$")
DELIVERY_STATES = {"PENDING", "CLAIMED", "SENDING", "SENT", "UNKNOWN", "FAILED_SAFE"}
RUN_STATES = {"CLAIMED", "RUNNING", "REPORT_READY", "DELIVERY_IN_PROGRESS", "COMPLETE", "PARTIAL_DELIVERY", "DELIVERY_UNKNOWN", "FAILED"}


class StateValidationError(ValueError):
    """State is malformed, unsafe, stale, or from an unsupported schema."""


def canonical_json(value: Any) -> bytes:
    """Serialize deterministic UTF-8 JSON used for checksums and commits."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def seal_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return a deep-copied state with a SHA-256 checksum over its contents."""
    result = deepcopy(state)
    result.pop("checksum", None)
    result["checksum"] = hashlib.sha256(canonical_json(result)).hexdigest()
    return result


def new_state(*, now: datetime | None = None) -> dict[str, Any]:
    """Create a genesis document for an operator-controlled branch bootstrap."""
    stamp = _utc(now or datetime.now(timezone.utc)).isoformat()
    return seal_state({
        "schema_version": SCHEMA_VERSION,
        "generation": 0,
        "created_at": stamp,
        "updated_at": stamp,
        "ownership": {"cloud_quota_exclusive": False, "windows_sender_disabled": False},
        "quota": {"daily": {}, "minute": {}, "reservations": [], "last_reservation_at": None},
        "runs": {},
        "deliveries": {},
        "audit": [],
    })


def validate_state(value: Any, *, now: datetime | None = None, enforce_freshness: bool = True) -> dict[str, Any]:
    """Validate state shape, integrity, privacy, size, and retention bounds."""
    if not isinstance(value, dict):
        raise StateValidationError("STATE_INVALID_ROOT")
    _require_keys(value, {"schema_version", "generation", "created_at", "updated_at", "ownership", "quota", "runs", "deliveries", "audit", "checksum"}, "STATE_ROOT_FIELDS_INVALID")
    try:
        encoded = canonical_json(value)
    except (TypeError, ValueError):
        raise StateValidationError("STATE_NOT_CANONICAL_JSON") from None
    if len(encoded) > MAX_STATE_BYTES:
        raise StateValidationError("STATE_SIZE_LIMIT_EXCEEDED")
    _reject_sensitive_fields(value)
    if value.get("schema_version") != SCHEMA_VERSION:
        raise StateValidationError("STATE_SCHEMA_VERSION_UNSUPPORTED")
    if not isinstance(value.get("generation"), int) or value["generation"] < 0:
        raise StateValidationError("STATE_GENERATION_INVALID")
    checksum = value.get("checksum")
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise StateValidationError("STATE_CHECKSUM_MISSING")
    expected = seal_state(value)["checksum"]
    if not hmac.compare_digest(checksum, expected):
        raise StateValidationError("STATE_CHECKSUM_MISMATCH")
    created = _parse_utc(value.get("created_at"), "STATE_CREATED_AT_INVALID")
    updated = _parse_utc(value.get("updated_at"), "STATE_UPDATED_AT_INVALID")
    current = _utc(now or datetime.now(timezone.utc))
    if created > current + timedelta(minutes=5) or updated > current + timedelta(minutes=5):
        raise StateValidationError("STATE_CLOCK_AHEAD")
    if updated < created:
        raise StateValidationError("STATE_TIMESTAMP_ORDER_INVALID")
    if enforce_freshness and current - updated > MAX_STATE_AGE:
        raise StateValidationError("STATE_STALE_REQUIRES_REVIEW")
    if not isinstance(value.get("quota"), dict):
        raise StateValidationError("STATE_QUOTA_INVALID")
    ownership = value.get("ownership")
    if not isinstance(ownership, dict) or not all(isinstance(ownership.get(key), bool) for key in ("cloud_quota_exclusive", "windows_sender_disabled")):
        raise StateValidationError("STATE_OWNERSHIP_ATTESTATION_INVALID")
    quota = value["quota"]
    _require_keys(quota, {"daily", "minute", "reservations", "last_reservation_at"}, "STATE_QUOTA_FIELDS_INVALID")
    if not all(isinstance(quota.get(key), dict) for key in ("daily", "minute")):
        raise StateValidationError("STATE_QUOTA_BUCKETS_INVALID")
    reservations = quota.get("reservations")
    if not isinstance(reservations, list) or len(reservations) > MAX_RESERVATIONS:
        raise StateValidationError("STATE_RESERVATIONS_INVALID")
    for bucket_name in ("daily", "minute"):
        for key, count in quota[bucket_name].items():
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise StateValidationError("STATE_QUOTA_COUNTER_INVALID")
            if bucket_name == "daily" and not _is_iso_date(key):
                raise StateValidationError("STATE_QUOTA_DAY_INVALID")
            if bucket_name == "minute":
                try:
                    datetime.strptime(str(key), "%Y-%m-%dT%H:%MZ")
                except ValueError:
                    raise StateValidationError("STATE_QUOTA_MINUTE_INVALID") from None
    for reservation in reservations:
        if isinstance(reservation, dict):
            _require_keys(reservation, {"reservation_id", "reserved_at", "utc_day", "utc_minute", "credits", "outcome"}, "STATE_RESERVATION_FIELDS_INVALID")
        if not isinstance(reservation, dict) or not isinstance(reservation.get("credits"), int) or reservation.get("credits", 0) <= 0:
            raise StateValidationError("STATE_RESERVATION_RECORD_INVALID")
        if not isinstance(reservation.get("reservation_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", reservation["reservation_id"]):
            raise StateValidationError("STATE_RESERVATION_ID_INVALID")
        if not _is_iso_date(reservation.get("utc_day")) or not isinstance(reservation.get("outcome"), str) or not SAFE_CODE_RE.fullmatch(reservation["outcome"]):
            raise StateValidationError("STATE_RESERVATION_METADATA_INVALID")
        _parse_utc(reservation.get("reserved_at"), "STATE_RESERVATION_TIME_INVALID")
    last_reservation = quota.get("last_reservation_at")
    if last_reservation is not None:
        _parse_utc(last_reservation, "STATE_QUOTA_LAST_RESERVATION_INVALID")
    runs = value.get("runs")
    deliveries = value.get("deliveries")
    if not isinstance(runs, dict) or len(runs) > MAX_RUN_DATES:
        raise StateValidationError("STATE_RUN_RETENTION_INVALID")
    if not isinstance(deliveries, dict) or len(deliveries) > MAX_DELIVERY_DATES:
        raise StateValidationError("STATE_DELIVERY_RETENTION_INVALID")
    if not isinstance(value.get("audit"), list) or len(value["audit"]) > 500:
        raise StateValidationError("STATE_AUDIT_INVALID")
    if value["audit"]:
        raise StateValidationError("STATE_AUDIT_EVENTS_NOT_SUPPORTED")
    for market_date, run in runs.items():
        if isinstance(run, dict):
            _require_keys(run, {"status", "run_id", "claimed_at", "started_at", "finished_at", "source_sha", "reason_code"}, "STATE_RUN_FIELDS_INVALID")
        if not _is_iso_date(market_date) or not isinstance(run, dict) or run.get("status") not in RUN_STATES:
            raise StateValidationError("STATE_RUN_RECORD_INVALID")
        if "run_id" in run and not RUN_ID_RE.fullmatch(str(run["run_id"])):
            raise StateValidationError("STATE_RUN_ID_INVALID")
        for stamp_name in ("claimed_at", "started_at", "finished_at"):
            if stamp_name in run:
                _parse_utc(run[stamp_name], "STATE_RUN_TIMESTAMP_INVALID")
        if "source_sha" in run and not re.fullmatch(r"[0-9a-f]{40,64}", str(run["source_sha"])):
            raise StateValidationError("STATE_RUN_SOURCE_SHA_INVALID")
        if "reason_code" in run and not SAFE_CODE_RE.fullmatch(str(run["reason_code"])):
            raise StateValidationError("STATE_RUN_REASON_INVALID")
    for market_date, record in deliveries.items():
        if isinstance(record, dict):
            _require_keys(record, {"recipients"}, "STATE_DELIVERY_FIELDS_INVALID")
        if not _is_iso_date(market_date) or not isinstance(record, dict) or not isinstance(record.get("recipients"), dict):
            raise StateValidationError("STATE_DELIVERY_RECORD_INVALID")
        for slot, recipient in record["recipients"].items():
            if isinstance(recipient, dict):
                _require_keys(recipient, {"status", "claimed_at", "attempt", "send_started_at", "updated_at", "safe_error", "reconciliation"}, "STATE_RECIPIENT_FIELDS_INVALID")
            if not RECIPIENT_SLOT_RE.fullmatch(str(slot)) or not isinstance(recipient, dict) or recipient.get("status") not in DELIVERY_STATES:
                raise StateValidationError("STATE_RECIPIENT_RECORD_INVALID")
            if "safe_error" in recipient and (not isinstance(recipient["safe_error"], str) or not SAFE_CODE_RE.fullmatch(recipient["safe_error"])):
                raise StateValidationError("STATE_RECIPIENT_ERROR_INVALID")
            for stamp_name in ("claimed_at", "send_started_at", "updated_at"):
                if stamp_name in recipient:
                    _parse_utc(recipient[stamp_name], "STATE_RECIPIENT_TIMESTAMP_INVALID")
            if "reconciliation" in recipient:
                details = recipient["reconciliation"]
                if not isinstance(details, dict):
                    raise StateValidationError("STATE_RECONCILIATION_INVALID")
                _require_keys(details, {"reviewer_ref", "reason_code"}, "STATE_RECONCILIATION_FIELDS_INVALID")
                if not RECIPIENT_SLOT_RE.fullmatch(str(details.get("reviewer_ref", ""))) or details.get("reason_code") not in {"OPERATOR_CONFIRMED_SENT", "OPERATOR_CONFIRMED_NOT_SENT"}:
                    raise StateValidationError("STATE_RECONCILIATION_INVALID")
    return deepcopy(value)


def advance_state(state: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Increment generation and refresh the integrity checksum."""
    result = deepcopy(state)
    result["generation"] = int(result["generation"]) + 1
    result["updated_at"] = _utc(now or datetime.now(timezone.utc)).isoformat()
    return seal_state(result)


def _reject_sensitive_fields(value: Any, parent: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if (
                normalized in {"token", "api_key", "apikey", "authorization", "chat_id", "recipient_id", "bot_token", "secret", "password"}
                or normalized.endswith("_token")
                or "api_key" in normalized
                or "chat_id" in normalized
                or "recipient_id" in normalized
            ):
                raise StateValidationError("STATE_SENSITIVE_FIELD_REJECTED")
            if normalized in {"recipients"} and isinstance(child, dict):
                for slot in child:
                    if not RECIPIENT_SLOT_RE.fullmatch(str(slot)):
                        raise StateValidationError("STATE_RECIPIENT_SLOT_INVALID")
            _reject_sensitive_fields(child, normalized)
    elif isinstance(value, list):
        for child in value:
            _reject_sensitive_fields(child, parent)


def _require_keys(value: dict[str, Any], allowed: set[str], error: str) -> None:
    if set(value) - allowed:
        raise StateValidationError(error)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise StateValidationError("STATE_TIMESTAMP_TIMEZONE_REQUIRED")
    return value.astimezone(timezone.utc)


def _parse_utc(value: Any, error: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
        return _utc(parsed)
    except (TypeError, ValueError, StateValidationError):
        raise StateValidationError(error) from None


def _is_iso_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat() == value
    except ValueError:
        return False
