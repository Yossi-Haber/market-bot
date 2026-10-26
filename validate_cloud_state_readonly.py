"""Fail-closed, authenticated, read-only production D1 state validation.

The Worker persists signed-request nonces to prevent replay. A validation run
uses only ``state.get``; nonce bookkeeping is the only expected remote write.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import requests

from market_calendar import MarketCalendarError, get_session, is_completed_session, resolve_market_date
from persistence.d1_client import D1StateClient, D1StateError

EXPECTED_WORKER_URL = "https://marketbot-state-api.yossihabar1994.workers.dev"
OWNERSHIP_FIELDS = (
    "cloud_quota_exclusive",
    "windows_sender_disabled",
    "cloud_delivery_enabled",
)
INTEGRITY_FIELDS = (
    "reservations_without_operation",
    "recent_runs_without_claim_operation",
    "recent_runs_without_last_operation",
    "recent_deliveries_without_last_operation",
    "deliveries_without_run",
    "completed_runs_with_unsent_delivery",
    "daily_quota_bucket_mismatches",
    "minute_quota_bucket_mismatches",
)


def parse_expected_ownership(value: str) -> dict[str, bool]:
    """Parse the explicit comma-separated 0/1 production gate expectation."""
    parts = value.split(",")
    if len(parts) != len(OWNERSHIP_FIELDS) or any(part not in {"0", "1"} for part in parts):
        raise ValueError("CLOUD_EXPECTED_OWNERSHIP_INVALID")
    return dict(zip(OWNERSHIP_FIELDS, (part == "1" for part in parts)))


def _count(value: Any, reason: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(reason)
    return value


def assess_date_eligibility(
    market_date: str,
    *,
    has_claim: bool,
    now: datetime,
    state_created_date: str | None = None,
) -> dict[str, Any]:
    """Report explicit-date session eligibility using the production calendar."""
    session = get_session(market_date)
    completed = bool(session and is_completed_session(market_date, now=now))
    reason = "ALREADY_CLAIMED" if has_claim else None
    if not session:
        reason = "NOT_NYSE_SESSION"
    elif not completed:
        reason = "SESSION_NOT_CLOSED"
    elif state_created_date and market_date < state_created_date:
        reason = "MARKET_DATE_PRECEDES_STATE_GENESIS"
    else:
        try:
            resolve_market_date(now=now, explicit_date=market_date, start_date=state_created_date)
        except MarketCalendarError as exc:
            reason = str(exc)
    return {
        "is_nyse_session": session is not None,
        "completed_as_of_validation": completed,
        "eligible_for_explicit_processing": reason is None,
        "eligibility_reason": reason or "ELIGIBLE",
    }


def validate_state_payload(
    state: dict[str, Any],
    *,
    expected_ownership: dict[str, bool] | None = None,
    market_dates: tuple[str, ...] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate operational state and return only non-secret summaries.

    Ownership mismatch is classified separately from schema/integrity errors.
    Delivery recipient references, run IDs, operation IDs and audit subjects
    are deliberately omitted from the summary.
    """
    if state.get("schema_version") != 1:
        raise ValueError("CLOUD_STATE_SCHEMA_INVALID")
    ownership = state.get("ownership")
    if not isinstance(ownership, dict) or set(ownership) != set(OWNERSHIP_FIELDS):
        raise ValueError("CLOUD_STATE_OWNERSHIP_INVALID")
    if any(type(ownership[field]) is not bool for field in OWNERSHIP_FIELDS):
        raise ValueError("CLOUD_STATE_OWNERSHIP_INVALID")
    expected = expected_ownership or dict.fromkeys(OWNERSHIP_FIELDS, False)
    if ownership != expected:
        expected_text = ",".join("1" if expected[field] else "0" for field in OWNERSHIP_FIELDS)
        actual_text = ",".join("1" if ownership[field] else "0" for field in OWNERSHIP_FIELDS)
        raise ValueError(f"CLOUD_OWNERSHIP_CONFIGURATION_MISMATCH expected={expected_text} actual={actual_text}")

    runs = state.get("runs")
    quota = state.get("quota")
    if not isinstance(runs, dict) or not isinstance(quota, dict):
        raise ValueError("CLOUD_STATE_OPERATIONAL_SUMMARY_INVALID")
    buckets = quota.get("buckets")
    reservations = quota.get("reservations_by_utc_day")
    delivery_rows = state.get("deliveries_by_market_date_and_status")
    operation_rows = state.get("operations_by_type_and_status")
    integrity = state.get("integrity")
    if not all(isinstance(value, list) for value in (buckets, reservations, delivery_rows, operation_rows)):
        raise ValueError("CLOUD_STATE_OPERATIONAL_SUMMARY_INVALID")
    if not isinstance(integrity, dict) or set(integrity) != set(INTEGRITY_FIELDS):
        raise ValueError("CLOUD_STATE_INTEGRITY_SUMMARY_INVALID")
    if state.get("integrity_window_days") != 7:
        raise ValueError("CLOUD_STATE_INTEGRITY_SUMMARY_INVALID")
    safe_integrity = {key: _count(integrity[key], "CLOUD_STATE_INTEGRITY_SUMMARY_INVALID") for key in INTEGRITY_FIELDS}
    if any(safe_integrity.values()):
        raise ValueError("CLOUD_STATE_INTEGRITY_FAILED")

    run_statuses = {
        date: (runs[date].get("status") if isinstance(runs.get(date), dict) else None) or "NONE"
        for date in market_dates
    }
    validation_time = now or datetime.now(timezone.utc)
    state_created_at = state.get("created_at")
    state_created_date = state_created_at[:10] if isinstance(state_created_at, str) and len(state_created_at) >= 10 else None
    date_eligibility = {
        date: assess_date_eligibility(date, has_claim=(run_statuses[date] != "NONE"), now=validation_time, state_created_date=state_created_date)
        for date in market_dates
    }
    delivery_by_date: dict[str, dict[str, Any]] = {
        date: {"by_status": {}, "recipient_count": 0, "attempts_total": 0, "max_attempt": 0}
        for date in market_dates
    }
    for row in delivery_rows:
        if not isinstance(row, dict):
            raise ValueError("CLOUD_STATE_DELIVERY_SUMMARY_INVALID")
        date, status = row.get("market_date"), row.get("status")
        if not isinstance(date, str) or not isinstance(status, str):
            raise ValueError("CLOUD_STATE_DELIVERY_SUMMARY_INVALID")
        if date in delivery_by_date:
            count = _count(row.get("recipient_count"), "CLOUD_STATE_DELIVERY_SUMMARY_INVALID")
            attempts = _count(row.get("attempts_total"), "CLOUD_STATE_DELIVERY_SUMMARY_INVALID")
            maximum = _count(row.get("max_attempt"), "CLOUD_STATE_DELIVERY_SUMMARY_INVALID")
            item = delivery_by_date[date]
            item["by_status"][status] = count
            item["recipient_count"] += count
            item["attempts_total"] += attempts
            item["max_attempt"] = max(item["max_attempt"], maximum)

    safe_buckets = []
    for row in buckets:
        if not isinstance(row, dict) or row.get("period") not in {"day", "minute"} or not isinstance(row.get("bucket_key"), str):
            raise ValueError("CLOUD_STATE_QUOTA_SUMMARY_INVALID")
        used, limit = row.get("used_credits"), row.get("limit_credits")
        if type(used) is not int or type(limit) is not int or used < 0 or limit < 1 or used > limit:
            raise ValueError("CLOUD_STATE_QUOTA_SUMMARY_INVALID")
        safe_buckets.append({"period": row["period"], "bucket_key": row["bucket_key"], "used_credits": used, "limit_credits": limit})

    safe_reservations = []
    for row in reservations:
        if not isinstance(row, dict) or not isinstance(row.get("utc_day"), str):
            raise ValueError("CLOUD_STATE_QUOTA_SUMMARY_INVALID")
        safe_reservations.append({
            "utc_day": row["utc_day"],
            "reservation_count": _count(row.get("reservation_count"), "CLOUD_STATE_QUOTA_SUMMARY_INVALID"),
            "credits": _count(row.get("credits"), "CLOUD_STATE_QUOTA_SUMMARY_INVALID"),
        })

    safe_operations: dict[str, int] = {}
    for row in operation_rows:
        if not isinstance(row, dict) or not isinstance(row.get("operation_type"), str) or not isinstance(row.get("status"), str):
            raise ValueError("CLOUD_STATE_OPERATION_SUMMARY_INVALID")
        safe_operations[f"{row['operation_type']}:{row['status']}"] = _count(row.get("operation_count"), "CLOUD_STATE_OPERATION_SUMMARY_INVALID")

    return {
        "schema_version": 1,
        "ownership": dict(ownership),
        "ownership_matches_expected": True,
        "run_count": len(runs),
        "run_statuses": run_statuses,
        "date_eligibility": date_eligibility,
        "deliveries": delivery_by_date,
        "quota_buckets": safe_buckets,
        "quota_reservations_by_utc_day": safe_reservations,
        "operations_by_type_and_status": safe_operations,
        "integrity": safe_integrity,
    }


def validate_read_only(
    client: D1StateClient,
    *,
    expected_ownership: dict[str, bool],
    market_dates: tuple[str, ...],
) -> dict[str, Any]:
    """Perform two authenticated state.get reads and require a stable snapshot."""
    validation_time = datetime.now(timezone.utc)
    first_summary = validate_state_payload(client.read(), expected_ownership=expected_ownership, market_dates=market_dates, now=validation_time)
    final_summary = validate_state_payload(client.read(), expected_ownership=expected_ownership, market_dates=market_dates, now=validation_time)
    return {
        "operation": "state.get",
        "schema_compatible": True,
        "reads_consistent": first_summary == final_summary,
        "ownership_unchanged_and_matches_expected": first_summary["ownership"] == final_summary["ownership"] == expected_ownership,
        "state_changing_business_operations": 0,
        "first_read": first_summary,
        "final_read": final_summary,
        "note": "Signed state.get may append request nonce bookkeeping; no quota, run, delivery, or ownership mutation was requested.",
    }


class StatusRecordingSession:
    """Record response status only; never retain headers or response bodies."""

    def __init__(self) -> None:
        self.statuses: list[int] = []

    def post(self, *args: Any, **kwargs: Any) -> Any:
        response = requests.post(*args, **kwargs)
        self.statuses.append(int(response.status_code))
        return response


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-ownership", default="0,0,0", help="Expected gate values in cloud_quota_exclusive,windows_sender_disabled,cloud_delivery_enabled order.")
    parser.add_argument("--market-dates", default="", help="Comma-separated market dates to report, e.g. 2026-10-26,2026-10-27.")
    args = parser.parse_args()

    session: StatusRecordingSession | None = None
    try:
        configured_url = os.getenv("MARKETBOT_D1_STATE_URL", "").strip().rstrip("/")
        if configured_url != EXPECTED_WORKER_URL:
            raise ValueError("CLOUD_WORKER_ENDPOINT_NOT_PINNED")
        if not os.getenv("MARKETBOT_D1_STATE_HMAC_SECRET", ""):
            raise ValueError("CLOUD_STATE_CREDENTIAL_MISSING")
        expected = parse_expected_ownership(args.expected_ownership)
        market_dates = tuple(dict.fromkeys(filter(None, args.market_dates.split(","))))
        for date in market_dates:
            # Date validation is strict and independent from market-calendar policy.
            from datetime import date as date_type
            try:
                valid_date = date_type.fromisoformat(date).isoformat() == date and len(date) == 10
            except ValueError:
                valid_date = False
            if not valid_date:
                raise ValueError("CLOUD_MARKET_DATE_INVALID")
        client = D1StateClient.from_env()
        session = StatusRecordingSession()
        client.session = session
        result = validate_read_only(
            client,
            expected_ownership=expected,
            market_dates=market_dates,
        )
        if not result["reads_consistent"]:
            raise ValueError("CLOUD_STATE_READS_CHANGED_DURING_VALIDATION")
        final = result["final_read"]
        print("CLOUD_STATE_READONLY=PASS")
        print(f"HTTP_STATUSES={','.join(map(str, session.statuses))}")
        print(f"SCHEMA_VERSION={final['schema_version']}")
        print(f"OWNERSHIP_FLAGS={','.join('1' if expected[field] else '0' for field in OWNERSHIP_FIELDS)}")
        print(f"RUN_COUNT={final['run_count']}")
        print(f"MARKET_DATE_RUN_STATUS={json.dumps(final['run_statuses'], sort_keys=True)}")
        print(f"MARKET_DATE_ELIGIBILITY={json.dumps(final['date_eligibility'], sort_keys=True)}")
        print(f"MARKET_DATE_DELIVERIES={json.dumps(final['deliveries'], sort_keys=True)}")
        print(f"QUOTA_BUCKET_COUNT={len(final['quota_buckets'])}")
        print(f"QUOTA_RESERVATION_DAYS={len(final['quota_reservations_by_utc_day'])}")
        print(f"OPERATION_TYPE_STATUS_COUNTS={json.dumps(final['operations_by_type_and_status'], sort_keys=True)}")
        print(f"STATE_INTEGRITY={json.dumps(final['integrity'], sort_keys=True)}")
        print("STATE_CHANGING_BUSINESS_OPERATIONS=0")
        print("NOTE=state.get authentication may append replay-protection nonce rows")
        return 0
    except Exception as exc:
        reason = str(exc) if isinstance(exc, (D1StateError, ValueError)) else "CLOUD_STATE_READONLY_FAILED"
        if not reason.isascii() or len(reason) > 160:
            reason = "CLOUD_STATE_READONLY_FAILED"
        statuses = ",".join(map(str, session.statuses)) if session else "none"
        category = exc.failure_category if isinstance(exc, D1StateError) and exc.failure_category else "VALIDATION"
        status = exc.http_status if isinstance(exc, D1StateError) and exc.http_status is not None else "none"
        print(f"CLOUD_STATE_READONLY=FAIL reason={reason} http_status={status} observed_statuses={statuses} category={category}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
