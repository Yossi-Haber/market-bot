"""Fail-closed synthetic mutation test runner for the isolated Phase 3C D1 Worker.

Default mode is plan-only. Remote mutation requires --execute-synthetic-mutations,
the exact TEST database assertion, TEST-only HMAC credentials, the expected TEST
Wrangler config and an authenticated Worker response proving the D1 identity row.
This runner has no provider or Telegram integrations.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from persistence.d1_client import D1OperationUncertain, D1QuotaExhausted, D1StateClient, D1StateError

import requests

WORKER_DIR = ROOT / "cloudflare" / "marketbot-state-worker"
TEST_CONFIG = WORKER_DIR / "wrangler.test.jsonc"
PRODUCTION_CONFIG = WORKER_DIR / "wrangler.jsonc"
WRANGLER_JS = WORKER_DIR / "node_modules" / "wrangler" / "bin" / "wrangler.js"
TEST_URL = "https://marketbot-state-api-test.yossihabar1994.workers.dev"
TEST_DATABASE_ID = "8049a22c-eb1f-4e9d-928d-29b35dcc6f70"
PRODUCTION_DATABASE_ID = "3f40e99b-8fb3-4fb8-90c7-ea1b4e019061"
PRODUCTION_URL = "https://marketbot-state-api.yossihabar1994.workers.dev"
TEST_MODE = "phase3c-synthetic-mutations-v1"
TEST_WORKER_NAME = "marketbot-state-api-test"
RECONCILED_OPERATIONS_FILE = ROOT / "output" / "qa" / "github_actions_migration_phase3c" / "reconciled_operations.json"
OPERATION_JOURNAL_FILE = ROOT / "output" / "qa" / "github_actions_migration_phase3c" / "remote_test_operation_journal.jsonl"
_GENERATED_OPERATION_IDS: set[str] = set()
_JOURNAL_LOCK = threading.Lock()

TEST_RECIPIENTS = (
    "hmac256:ac00581af76fae2942fefc401985c751c927c4412b3e8486f54cda8e4d420419",
    "hmac256:ad4e4df2c02723b74f014769278cd2c1edac43e7f17751f1b9048f40b11aa692",
)
RECONCILED_DELIVERY_DATE = "2099-01-02"
RECONCILED_RECIPIENT = TEST_RECIPIENTS[0]
RECONCILIATION_OPERATION_ID = "3cb759039c7fea61eff99cfe8a930e3e"
PREFLIGHT_SQL = """
SELECT
 (SELECT version FROM schema_meta WHERE singleton=1) AS schema_version,
 (SELECT cloud_quota_exclusive FROM ownership WHERE singleton=1) AS cloud_quota_exclusive,
 (SELECT windows_sender_disabled FROM ownership WHERE singleton=1) AS windows_sender_disabled,
 (SELECT cloud_delivery_enabled FROM ownership WHERE singleton=1) AS cloud_delivery_enabled,
 (SELECT COUNT(*) FROM operations) AS operation_count,
 (SELECT COUNT(*) FROM quota_reservation) AS reservation_count,
 (SELECT COUNT(*) FROM run_claim) AS run_count,
 (SELECT COUNT(*) FROM delivery_recipient) AS delivery_count,
 (SELECT COUNT(*) FROM audit_event) AS audit_count,
 (SELECT COUNT(*) FROM quota_bucket) AS quota_bucket_count,
 (SELECT database_id FROM test_environment_identity WHERE singleton=1) AS database_id,
 (SELECT worker_name FROM test_environment_identity WHERE singleton=1) AS worker_name,
 (SELECT mode FROM test_environment_identity WHERE singleton=1) AS mode,
 (SELECT attestation_hash FROM test_environment_identity WHERE singleton=1) AS attestation_hash,
 COALESCE((SELECT used_credits FROM quota_bucket WHERE period='minute' AND bucket_key=strftime('%Y-%m-%dT%H:%MZ','now')),0) AS minute_used,
 COALESCE((SELECT used_credits FROM quota_bucket WHERE period='day' AND bucket_key=strftime('%Y-%m-%d','now')),0) AS day_used
""".strip()

# Exact user-reconciled state before the remaining delivery/recovery scenarios.
# This is read-only and intentionally rejects any drift instead of replaying the
# earlier quota/concurrency mutations.
TARGETED_RESUME_SQL = f"""
SELECT
 (SELECT version FROM schema_meta WHERE singleton=1) AS schema_version,
 (SELECT cloud_quota_exclusive FROM ownership WHERE singleton=1) AS cloud_quota_exclusive,
 (SELECT windows_sender_disabled FROM ownership WHERE singleton=1) AS windows_sender_disabled,
 (SELECT cloud_delivery_enabled FROM ownership WHERE singleton=1) AS cloud_delivery_enabled,
 (SELECT database_id FROM test_environment_identity WHERE singleton=1) AS database_id,
 (SELECT worker_name FROM test_environment_identity WHERE singleton=1) AS worker_name,
 (SELECT mode FROM test_environment_identity WHERE singleton=1) AS mode,
 (SELECT attestation_hash FROM test_environment_identity WHERE singleton=1) AS attestation_hash,
 (SELECT COUNT(*) FROM quota_reservation WHERE reservation_id LIKE '3c%') AS synthetic_reservations,
 (SELECT COUNT(*) FROM quota_reservation WHERE reservation_id LIKE '3c%' AND utc_day='2026-10-14') AS reservations_oct14,
 (SELECT COUNT(*) FROM quota_reservation WHERE reservation_id LIKE '3c%' AND utc_day='2026-10-15') AS reservations_oct15,
 (SELECT used_credits FROM quota_bucket WHERE period='day' AND bucket_key='2026-10-15') AS quota_day_oct15_used,
 COALESCE((SELECT MAX(used_credits) FROM quota_bucket WHERE period='minute' AND bucket_key LIKE '2026-10-15T%'),0) AS max_oct15_minute_used,
 (SELECT COUNT(*) FROM run_claim WHERE market_date BETWEEN '2099-01-01' AND '2099-01-02' AND run_id LIKE '3c%') AS synthetic_claims,
 (SELECT status FROM run_claim WHERE market_date='2099-01-02' AND run_id LIKE '3c%') AS delivery_run_status,
 (SELECT COUNT(*) FROM delivery_recipient WHERE market_date BETWEEN '2099-01-01' AND '2099-12-31') AS synthetic_deliveries,
 (SELECT status FROM delivery_recipient WHERE market_date='{RECONCILED_DELIVERY_DATE}' AND recipient_hmac='{RECONCILED_RECIPIENT}') AS reconciled_status,
 (SELECT attempt FROM delivery_recipient WHERE market_date='{RECONCILED_DELIVERY_DATE}' AND recipient_hmac='{RECONCILED_RECIPIENT}') AS reconciled_attempt,
 (SELECT safe_error FROM delivery_recipient WHERE market_date='{RECONCILED_DELIVERY_DATE}' AND recipient_hmac='{RECONCILED_RECIPIENT}') AS reconciled_safe_error,
 (SELECT reconciliation_code FROM delivery_recipient WHERE market_date='{RECONCILED_DELIVERY_DATE}' AND recipient_hmac='{RECONCILED_RECIPIENT}') AS reconciled_code,
 (SELECT last_operation_id FROM delivery_recipient WHERE market_date='{RECONCILED_DELIVERY_DATE}' AND recipient_hmac='{RECONCILED_RECIPIENT}') AS reconciled_last_operation_id,
 (SELECT COUNT(*) FROM delivery_recipient WHERE market_date='{RECONCILED_DELIVERY_DATE}' AND recipient_hmac='{TEST_RECIPIENTS[1]}') AS second_recipient_rows,
 (SELECT operation_type FROM operations WHERE operation_id='{RECONCILIATION_OPERATION_ID}') AS reconciled_operation_type,
 (SELECT status FROM operations WHERE operation_id='{RECONCILIATION_OPERATION_ID}') AS reconciled_operation_status,
 (SELECT COUNT(*) FROM audit_event WHERE audit_id='{RECONCILIATION_OPERATION_ID}' AND event_type='OPERATOR_CONFIRMED_NOT_SENT' AND subject_ref='{RECONCILED_DELIVERY_DATE}:{RECONCILED_RECIPIENT}' AND actor_ref='{TEST_RECIPIENTS[1]}') AS reconciled_audit_matches,
 (SELECT COUNT(*) FROM operations WHERE operation_id LIKE '3c%' AND operation_type='quota.reserve') AS quota_operations,
 (SELECT COUNT(*) FROM operations WHERE operation_id LIKE '3c%' AND operation_type='run.claim') AS claim_operations,
 (SELECT COUNT(*) FROM operations WHERE operation_id LIKE '3c%' AND operation_type='delivery.begin') AS begin_operations,
 (SELECT COUNT(*) FROM operations WHERE operation_id LIKE '3c%' AND operation_type='delivery.outcome') AS outcome_operations,
 (SELECT COUNT(*) FROM operations WHERE operation_id LIKE '3c%' AND operation_type='delivery.reconcile') AS reconcile_operations,
 (SELECT COUNT(*) FROM run_claim WHERE market_date='2099-01-03') AS recovery_date_rows
""".strip()


class SafeTestError(RuntimeError):
    """A safe, non-secret test preflight or assertion failure."""


UNCERTAIN_OPERATION_RECORD = ROOT / "output" / "qa" / "github_actions_migration_phase3c" / "unresolved_remote_operation.json"


def _record_uncertain_operation(exc: D1OperationUncertain) -> str:
    """Persist exact synthetic operation identity privately for later reconciliation."""
    if not exc.operation or not exc.operation_id:
        return "UNAVAILABLE"
    record = {
        "operation": exc.operation,
        "operation_id": exc.operation_id,
        "sanitized_operation_id": exc.sanitized_operation_id,
        "http_status": exc.http_status,
        "failure_category": exc.failure_category or "UNKNOWN",
        "may_have_committed": exc.may_have_committed,
        "attempts": exc.attempts,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "target": {"worker": TEST_WORKER_NAME, "database": "marketbot-test", "database_id": TEST_DATABASE_ID},
        "reconciled": False,
    }
    UNCERTAIN_OPERATION_RECORD.parent.mkdir(parents=True, exist_ok=True)
    try:
        with UNCERTAIN_OPERATION_RECORD.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except FileExistsError:
        return "EXISTING_UNRESOLVED_RECORD_PRESERVED"
    return str(UNCERTAIN_OPERATION_RECORD)


def _format_operation_failure(exc: D1StateError) -> str:
    status = str(exc.http_status) if exc.http_status is not None else "none"
    op_id = exc.sanitized_operation_id or "unavailable"
    return (
        f"REMOTE_OPERATION_FAILURE operation={exc.operation or 'unknown'} operation_id={op_id} "
        f"http_status={status} category={exc.failure_category or 'UNKNOWN'} "
        f"may_have_committed={str(exc.may_have_committed).lower()} code={str(exc)}"
    )


def assert_test_target(database_id_assertion: str | None = None) -> None:
    try:
        test = json.loads(TEST_CONFIG.read_text(encoding="utf-8"))
        production = json.loads(PRODUCTION_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise SafeTestError("TEST_TARGET_CONFIG_INVALID") from None
    expected_db = [{"binding": "DB", "database_name": "marketbot-test", "database_id": TEST_DATABASE_ID, "migrations_dir": "migrations"}]
    expected_vars = {"MARKETBOT_TEST_MODE": TEST_MODE, "MARKETBOT_TEST_DATABASE_ID": TEST_DATABASE_ID, "MARKETBOT_TEST_WORKER_NAME": TEST_WORKER_NAME}
    if (
        test.get("name") != TEST_WORKER_NAME
        or test.get("main") != "src/index.ts"
        or test.get("workers_dev") is not True
        or test.get("d1_databases") != expected_db
        or test.get("vars") != expected_vars
    ):
        raise SafeTestError("TEST_TARGET_IDENTITY_MISMATCH")
    if database_id_assertion is not None and database_id_assertion != TEST_DATABASE_ID:
        raise SafeTestError("TEST_DATABASE_ASSERTION_MISMATCH")
    if production.get("name") != "marketbot-state-api" or production.get("d1_databases", [{}])[0].get("database_id") != PRODUCTION_DATABASE_ID:
        raise SafeTestError("PRODUCTION_CONFIG_IDENTITY_UNEXPECTED")
    if production.get("vars") and any(key.startswith("MARKETBOT_TEST_") for key in production["vars"]):
        raise SafeTestError("TEST_MODE_CONFIG_PRESENT_IN_PRODUCTION")
    if TEST_DATABASE_ID == PRODUCTION_DATABASE_ID or TEST_URL == PRODUCTION_URL:
        raise SafeTestError("TEST_PRODUCTION_IDENTITY_COLLISION")
    raw = TEST_CONFIG.read_text(encoding="utf-8")
    if PRODUCTION_DATABASE_ID in raw or PRODUCTION_URL in raw or '"marketbot-state-api"' in raw:
        raise SafeTestError("PRODUCTION_IDENTITY_IN_TEST_CONFIG")


def _read_secret(env_name: str, prompt: str) -> str:
    value = os.environ.get(env_name, "")
    if not value:
        value = getpass.getpass(prompt)
    if len(value.encode("utf-8")) < 32:
        raise SafeTestError("TEST_SECRET_INVALID")
    return value


def get_test_credentials() -> tuple[str, str]:
    api = _read_secret("MARKETBOT_D1_TEST_HMAC_SECRET", "TEST Worker API HMAC secret (hidden): ")
    admin = _read_secret("MARKETBOT_D1_TEST_ADMIN_HMAC_SECRET", "TEST Worker admin HMAC secret (hidden): ")
    if hmac.compare_digest(api, admin):
        raise SafeTestError("TEST_SECRETS_MUST_BE_INDEPENDENT")
    production_names = (
        "MARKETBOT_D1_STATE_HMAC_SECRET", "MARKETBOT_D1_STATE_ADMIN_HMAC_SECRET",
        "STATE_API_HMAC_SECRET", "STATE_ADMIN_HMAC_SECRET",
    )
    for name in production_names:
        other = os.environ.get(name, "")
        if other and (hmac.compare_digest(api, other) or hmac.compare_digest(admin, other)):
            raise SafeTestError("TEST_SECRET_MATCHES_NON_TEST_CREDENTIAL")
    return api, admin


def _safe_process_detail(value: Any) -> str:
    """Bound and redact CLI diagnostics before they reach console output."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    detail = str(value or "").strip().splitlines()[0][:240]
    detail = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", detail)
    detail = re.sub(r"(?i)(secret|token|password|api[_-]?key|authorization)(\s*[:=]\s*|\s+)[^\s,;]+", r"\1\2[REDACTED]", detail)
    detail = re.sub(r"https?://[^\s/@:]+:[^\s/@]+@", "https://[REDACTED]@", detail)
    return detail or "no_cli_detail"


def _wrangler_query(sql: str, *, operation_name: str = "test_d1.read") -> dict[str, Any]:
    node = shutil.which("node")
    if not node or not WRANGLER_JS.is_file():
        raise SafeTestError(f"TEST_D1_READ_FAILED operation={operation_name} category=LOCAL_DEPENDENCY_MISSING node_or_wrangler=unavailable target=marketbot-test/{TEST_DATABASE_ID[:4]}…{TEST_DATABASE_ID[-4:]}")
    command = [node, str(WRANGLER_JS), "d1", "execute", "marketbot-test", "--remote", "--json", "--config", str(TEST_CONFIG), "--command", sql]
    try:
        result = subprocess.run(command, cwd=WORKER_DIR, capture_output=True, text=True, timeout=45, check=False)
    except subprocess.TimeoutExpired as exc:
        detail = _safe_process_detail(exc.stderr or exc.stdout)
        raise SafeTestError(
            f"TEST_D1_READ_FAILED operation={operation_name} category=WRANGLER_TIMEOUT "
            f"error_class={type(exc).__name__} timeout_seconds=45 http_status=unavailable "
            f"target=marketbot-test/{TEST_DATABASE_ID[:4]}…{TEST_DATABASE_ID[-4:]} detail={detail}"
        ) from None
    except OSError as exc:
        detail = _safe_process_detail(exc)
        raise SafeTestError(
            f"TEST_D1_READ_FAILED operation={operation_name} category=WRANGLER_PROCESS_START "
            f"error_class={type(exc).__name__} http_status=unavailable "
            f"target=marketbot-test/{TEST_DATABASE_ID[:4]}…{TEST_DATABASE_ID[-4:]} detail={detail}"
        ) from None
    if result.returncode != 0:
        combined = _safe_process_detail(result.stderr or result.stdout)
        status_match = re.search(r"\b(?:HTTP\s*)?(401|403|404|408|409|429|5\d\d)\b", combined, re.IGNORECASE)
        status = status_match.group(1) if status_match else "unavailable"
        category = "WRANGLER_REMOTE_HTTP_OR_AUTH" if status != "unavailable" else "WRANGLER_NONZERO_EXIT"
        raise SafeTestError(
            f"TEST_D1_READ_FAILED operation={operation_name} category={category} "
            f"error_class=WranglerProcessError exit_code={result.returncode} http_status={status} "
            f"target=marketbot-test/{TEST_DATABASE_ID[:4]}…{TEST_DATABASE_ID[-4:]} detail={combined}"
        )
    parsed: Any = None
    decoder = json.JSONDecoder()
    for index, char in enumerate(result.stdout):
        if char not in "[{":
            continue
        try:
            parsed, _ = decoder.raw_decode(result.stdout[index:])
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(parsed, list) or len(parsed) != 1 or not isinstance(parsed[0], dict):
        raise SafeTestError("TEST_D1_RESULT_SHAPE_INVALID")
    result_set = parsed[0]
    rows = result_set.get("results")
    if result_set.get("success") is not True or not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise SafeTestError("TEST_D1_RESULT_SHAPE_INVALID")
    return rows[0]


def assert_remote_test_database_isolated(*, initially_empty: bool, admin_secret: str) -> dict[str, Any]:
    row = _wrangler_query(PREFLIGHT_SQL, operation_name="test_database_preflight")
    expected = {
        "schema_version", "cloud_quota_exclusive", "windows_sender_disabled", "cloud_delivery_enabled",
        "operation_count", "reservation_count", "run_count", "delivery_count", "audit_count", "quota_bucket_count", "database_id", "worker_name", "mode", "attestation_hash",
        "minute_used", "day_used",
    }
    if set(row) != expected or any(row.get(key) is None for key in expected):
        raise SafeTestError("TEST_D1_IDENTITY_FIELDS_INVALID")
    numeric = expected - {"database_id", "worker_name", "mode", "attestation_hash"}
    if any(type(row[key]) is not int for key in numeric):
        raise SafeTestError("TEST_D1_VALUES_NOT_NUMERIC")
    if row["database_id"] != TEST_DATABASE_ID or row["worker_name"] != TEST_WORKER_NAME or row["mode"] != TEST_MODE:
        raise SafeTestError("TEST_D1_IDENTITY_MISMATCH")
    expected_attestation = hashlib.sha256(admin_secret.encode("utf-8")).hexdigest()
    if not isinstance(row["attestation_hash"], str) or not hmac.compare_digest(row["attestation_hash"], expected_attestation):
        raise SafeTestError("TEST_D1_SECRET_ATTESTATION_MISMATCH")
    if row["schema_version"] != 1 or any(row[key] != 0 for key in ("cloud_quota_exclusive", "windows_sender_disabled", "cloud_delivery_enabled")):
        raise SafeTestError("TEST_D1_SCHEMA_OR_OWNERSHIP_INVALID")
    if initially_empty and any(row[key] != 0 for key in ("operation_count", "reservation_count", "run_count", "delivery_count", "audit_count", "quota_bucket_count", "minute_used", "day_used")):
        raise SafeTestError("TEST_D1_BUSINESS_STATE_NOT_EMPTY_CLEANUP_REQUIRED")
    if not initially_empty and (row["minute_used"] > 8 or row["day_used"] > 800):
        raise SafeTestError("TEST_D1_QUOTA_COUNTER_INVALID")
    return row


def _load_reconciled_reservation() -> dict[str, Any]:
    try:
        data = json.loads(RECONCILED_OPERATIONS_FILE.read_text(encoding="utf-8"))
        operation = data["operations"][0]
    except (OSError, json.JSONDecodeError, KeyError, IndexError, TypeError):
        raise SafeTestError("RECONCILED_TEST_BASELINE_MISSING_OR_INVALID") from None
    if (
        set(data) != {"source", "target", "operations"}
        or data.get("target") != {"worker": TEST_WORKER_NAME, "database": "marketbot-test", "database_id": TEST_DATABASE_ID}
        or data.get("source") != "user-confirmed direct read-only query against TEST D1"
        or len(data.get("operations", [])) != 1
        or set(operation) != {"operation_id", "operation_type", "status", "reservation_exists", "credits", "utc_day", "utc_minute", "reconciled"}
        or operation.get("operation_id") != "3c57c52fcc8083b8a2944752c9b3d40d"
        or operation.get("operation_type") != "quota.reserve"
        or operation.get("status") != "COMMITTED"
        or operation.get("reservation_exists") is not True
        or operation.get("credits") != 1
        or operation.get("utc_day") != "2026-10-14"
        or operation.get("utc_minute") != "2026-10-14T21:25Z"
        or operation.get("reconciled") is not True
        or not re.fullmatch(r"3c[0-9a-f]{30}", str(operation.get("operation_id", "")))
    ):
        raise SafeTestError("RECONCILED_TEST_BASELINE_INVALID")
    _GENERATED_OPERATION_IDS.add(operation["operation_id"])
    return operation


def assert_reconciled_test_baseline(*, admin_secret: str) -> dict[str, Any]:
    """Permit only the one user-confirmed prior reservation as existing TEST state."""
    operation = _load_reconciled_reservation()
    assert_remote_test_database_isolated(initially_empty=False, admin_secret=admin_secret)
    op_id = operation["operation_id"]
    sql = PREFLIGHT_SQL + f""",
 (SELECT operation_type FROM operations WHERE operation_id='{op_id}') AS reconciled_operation_type,
 (SELECT status FROM operations WHERE operation_id='{op_id}') AS reconciled_operation_status,
 (SELECT COUNT(*) FROM operations WHERE operation_id LIKE '3c%') AS synthetic_operation_count,
 (SELECT COUNT(*) FROM quota_reservation WHERE reservation_id LIKE '3c%') AS synthetic_reservation_count,
 (SELECT provider FROM quota_reservation WHERE reservation_id='{op_id}') AS reconciled_provider,
 (SELECT credits FROM quota_reservation WHERE reservation_id='{op_id}') AS reconciled_credits,
 (SELECT utc_day FROM quota_reservation WHERE reservation_id='{op_id}') AS reconciled_utc_day,
 (SELECT utc_minute FROM quota_reservation WHERE reservation_id='{op_id}') AS reconciled_utc_minute,
 (SELECT COUNT(*) FROM quota_bucket WHERE period='minute' AND bucket_key=strftime('%Y-%m-%dT%H:%MZ','now') AND used_credits>0) AS current_minute_usage_rows,
 (SELECT COUNT(*) FROM quota_bucket WHERE period='day' AND bucket_key=strftime('%Y-%m-%d','now') AND used_credits>0) AS current_day_usage_rows"""
    row = _wrangler_query(sql, operation_name="reconciled_quota_operation_lookup")
    expected = {
        "operation_type": "quota.reserve",
        "status": "COMMITTED",
        "operations": 1,
        "reservations": 1,
        "total_operations": 1,
        "total_reservations": 1,
        "runs": 0,
        "deliveries": 0,
        "audit": 0,
        "quota_buckets": 2,
        "provider": "twelve_data",
        "credits": 1,
        "utc_day": "2026-10-14",
        "utc_minute": "2026-10-14T21:25Z",
        "current_minute_usage_rows": 0,
        "current_day_usage_rows": 0,
    }
    observed = {
        "operation_type": row.get("reconciled_operation_type"),
        "status": row.get("reconciled_operation_status"),
        "operations": row.get("synthetic_operation_count"),
        "reservations": row.get("synthetic_reservation_count"),
        "total_operations": row.get("operation_count"),
        "total_reservations": row.get("reservation_count"),
        "runs": row.get("run_count"),
        "deliveries": row.get("delivery_count"),
        "audit": row.get("audit_count"),
        "quota_buckets": row.get("quota_bucket_count"),
        "provider": row.get("reconciled_provider"),
        "credits": row.get("reconciled_credits"),
        "utc_day": row.get("reconciled_utc_day"),
        "utc_minute": row.get("reconciled_utc_minute"),
        "current_minute_usage_rows": row.get("current_minute_usage_rows"),
        "current_day_usage_rows": row.get("current_day_usage_rows"),
    }
    if observed != expected:
        raise SafeTestError("RECONCILED_TEST_BASELINE_DOES_NOT_MATCH_REMOTE_TEST_D1")
    return operation


def assert_targeted_resume_baseline(*, admin_secret: str) -> dict[str, Any]:
    """Require the user-confirmed partial state; never rerun earlier test phases."""
    row = _wrangler_query(TARGETED_RESUME_SQL, operation_name="targeted_resume_baseline")
    expected_keys = {
        "schema_version", "cloud_quota_exclusive", "windows_sender_disabled", "cloud_delivery_enabled",
        "database_id", "worker_name", "mode", "attestation_hash", "synthetic_reservations",
        "reservations_oct14", "reservations_oct15", "quota_day_oct15_used", "max_oct15_minute_used",
        "synthetic_claims", "delivery_run_status", "synthetic_deliveries",
        "reconciled_status", "reconciled_attempt", "reconciled_safe_error", "reconciled_code",
        "reconciled_last_operation_id", "reconciled_operation_type", "reconciled_operation_status",
        "reconciled_audit_matches",
        "second_recipient_rows", "quota_operations", "claim_operations", "begin_operations",
        "outcome_operations", "reconcile_operations", "recovery_date_rows",
    }
    if set(row) != expected_keys or any(row.get(key) is None for key in expected_keys):
        raise SafeTestError("TARGETED_RESUME_SNAPSHOT_SHAPE_INVALID")
    numeric = expected_keys - {
        "database_id", "worker_name", "mode", "attestation_hash", "delivery_run_status",
        "reconciled_status", "reconciled_safe_error", "reconciled_code", "reconciled_last_operation_id",
        "reconciled_operation_type", "reconciled_operation_status",
    }
    if any(type(row[key]) is not int for key in numeric):
        raise SafeTestError("TARGETED_RESUME_SNAPSHOT_TYPES_INVALID")
    expected_attestation = hashlib.sha256(admin_secret.encode("utf-8")).hexdigest()
    if (row["database_id"], row["worker_name"], row["mode"]) != (TEST_DATABASE_ID, TEST_WORKER_NAME, TEST_MODE):
        raise SafeTestError("TARGETED_RESUME_TEST_IDENTITY_MISMATCH")
    if not isinstance(row["attestation_hash"], str) or not hmac.compare_digest(row["attestation_hash"], expected_attestation):
        raise SafeTestError("TARGETED_RESUME_ADMIN_ATTESTATION_MISMATCH")
    expected = {
        "schema_version": 1, "cloud_quota_exclusive": 0, "windows_sender_disabled": 0, "cloud_delivery_enabled": 0,
        "synthetic_reservations": 9, "reservations_oct14": 1, "reservations_oct15": 8,
        "quota_day_oct15_used": 8, "max_oct15_minute_used": 8,
        "synthetic_claims": 2, "delivery_run_status": "CLAIMED", "synthetic_deliveries": 1,
        "reconciled_status": "FAILED_SAFE", "reconciled_attempt": 1,
        "reconciled_safe_error": "SIMULATED_RESPONSE_LOSS",
        "reconciled_code": "OPERATOR_CONFIRMED_NOT_SENT",
        "reconciled_last_operation_id": RECONCILIATION_OPERATION_ID,
        "reconciled_operation_type": "delivery.reconcile", "reconciled_operation_status": "COMMITTED",
        "reconciled_audit_matches": 1,
        "second_recipient_rows": 0, "quota_operations": 9, "claim_operations": 2,
        "begin_operations": 2, "outcome_operations": 1, "reconcile_operations": 1,
        "recovery_date_rows": 0,
    }
    if any(row.get(key) != value for key, value in expected.items()):
        differing = sorted(key for key, value in expected.items() if row.get(key) != value)
        raise SafeTestError(
            "TARGETED_RESUME_REMOTE_STATE_MISMATCH_NO_MUTATIONS_SENT "
            f"failed_fields={','.join(differing)} "
            f"reconciliation_operation_id={_safe_id(RECONCILIATION_OPERATION_ID)}"
        )
    return row


def _assert_delivery_row(
    market_date: str,
    recipient: str,
    *,
    status: str,
    attempt: int,
    safe_error: str | None,
    reconciliation_code: str | None = None,
    operation_id: str | None = None,
) -> None:
    row = _wrangler_query(f"""
SELECT status, attempt, safe_error, reconciliation_code, last_operation_id FROM delivery_recipient
WHERE market_date='{market_date}' AND recipient_hmac='{recipient}'
""".strip(), operation_name="delivery_row_postcondition")
    expected = {
        "status": status,
        "attempt": attempt,
        "safe_error": safe_error,
        "reconciliation_code": reconciliation_code,
    }
    actual = {key: row.get(key) for key in expected}
    if operation_id is not None:
        expected["last_operation_id"] = operation_id
        actual["last_operation_id"] = row.get("last_operation_id")
    expected_keys = set(expected) | {"reconciliation_code", "last_operation_id"}
    if set(row) != expected_keys or actual != expected:
        safe_operation_id = _safe_id(operation_id)
        printable_expected = {**expected}
        printable_actual = {**actual}
        if "last_operation_id" in printable_expected:
            printable_expected["last_operation_id"] = _safe_id(printable_expected["last_operation_id"])
            printable_actual["last_operation_id"] = _safe_id(printable_actual["last_operation_id"])
        raise SafeTestError(
            "TARGETED_DELIVERY_POSTCONDITION_FAILED "
            f"operation=delivery.state_readback operation_id={safe_operation_id} "
            f"date={market_date} expected={json.dumps(printable_expected, sort_keys=True)} "
            f"actual={json.dumps(printable_actual, sort_keys=True)}"
        )


def run_targeted_delivery_continuation(api_secret: str, admin_secret: str, *, confirm_reconciled_baseline: bool) -> None:
    """Continue after validating the already committed reconciliation; never replay it."""
    if not confirm_reconciled_baseline:
        raise SafeTestError("EXPLICIT_RECONCILED_BASELINE_CONFIRMATION_REQUIRED")
    baseline = assert_targeted_resume_baseline(admin_secret=admin_secret)
    initial = state_snapshot(api_secret, market_date="2099-01-02")
    if initial.get("run", {}).get("status") != "CLAIMED":
        raise SafeTestError("TARGETED_DELIVERY_RUN_STATE_MISMATCH")
    print(
        "TARGETED_BASELINE=PASS "
        f"reconciliation=COMMITTED operation_id={_safe_id(RECONCILIATION_OPERATION_ID)} "
        f"audit_matches={baseline['reconciled_audit_matches']} "
        "original_safe_error=preserved prior_quota_and_claim_tests=not_replayed"
    )
    test_client = client(api_secret, admin_secret)
    slot_a, slot_b = TEST_RECIPIENTS
    delivery_date = "2099-01-02"

    # The exact prior reconciliation and audit row were validated in the baseline
    # query. Do not replay this operation or alter its audit/history.
    _assert_delivery_row(
        delivery_date,
        slot_a,
        status="FAILED_SAFE",
        attempt=1,
        safe_error="SIMULATED_RESPONSE_LOSS",
        reconciliation_code="OPERATOR_CONFIRMED_NOT_SENT",
        operation_id=RECONCILIATION_OPERATION_ID,
    )

    denied_retry_payload = {
        "market_date": delivery_date,
        "recipient_hmac": slot_a,
        "attempt_id": _id(),
        "reviewed_retry": False,
    }
    denied_retry_id = _id()
    _expect_error(
        lambda: test_client.call("delivery.begin", denied_retry_payload, operation_id=denied_retry_id),
        "DELIVERY_SLOT_NOT_SENDABLE",
        operation="delivery.begin",
        operation_id=denied_retry_id,
    )
    _assert_delivery_row(
        delivery_date,
        slot_a,
        status="FAILED_SAFE",
        attempt=1,
        safe_error="SIMULATED_RESPONSE_LOSS",
        reconciliation_code="OPERATOR_CONFIRMED_NOT_SENT",
        operation_id=RECONCILIATION_OPERATION_ID,
    )

    retry_payload = {"market_date": delivery_date, "recipient_hmac": slot_a, "attempt_id": _id(), "reviewed_retry": True}
    retry_id = _id()
    retry = test_client.call("delivery.begin", retry_payload, operation_id=retry_id)
    if retry.get("authorized") is not True:
        raise SafeTestError("TARGETED_REVIEWED_RETRY_NOT_AUTHORIZED")
    _assert_delivery_row(delivery_date, slot_a, status="SENDING", attempt=2, safe_error=None,
                         reconciliation_code="OPERATOR_CONFIRMED_NOT_SENT", operation_id=retry_id)
    _expect_non_authorizing_replay(
        lambda: test_client.call("delivery.begin", retry_payload, operation_id=retry_id),
        operation="delivery.begin",
        operation_id=retry_id,
    )
    _assert_delivery_row(delivery_date, slot_a, status="SENDING", attempt=2, safe_error=None,
                         reconciliation_code="OPERATOR_CONFIRMED_NOT_SENT", operation_id=retry_id)
    # This is a synthetic state transition only; no messaging client is present.
    outcome_a_id = _id()
    sent_a = test_client.call("delivery.outcome", {
        "market_date": delivery_date, "recipient_hmac": slot_a, "status": "SENT",
    }, operation_id=outcome_a_id)
    if sent_a.get("applied") is not True:
        raise SafeTestError("TARGETED_DELIVERY_OUTCOME_NOT_APPLIED")
    _assert_delivery_row(delivery_date, slot_a, status="SENT", attempt=2, safe_error=None,
                         reconciliation_code="OPERATOR_CONFIRMED_NOT_SENT", operation_id=outcome_a_id)

    begin_b_id = _id()
    second = test_client.call("delivery.begin", {
        "market_date": delivery_date, "recipient_hmac": slot_b,
        "attempt_id": _id(), "reviewed_retry": False,
    }, operation_id=begin_b_id)
    if second.get("authorized") is not True:
        raise SafeTestError("TARGETED_SECOND_RECIPIENT_BEGIN_FAILED")
    _assert_delivery_row(delivery_date, slot_b, status="SENDING", attempt=1, safe_error=None,
                         operation_id=begin_b_id)
    outcome_b_id = _id()
    sent_b = test_client.call("delivery.outcome", {
        "market_date": delivery_date, "recipient_hmac": slot_b, "status": "SENT",
    }, operation_id=outcome_b_id)
    if sent_b.get("applied") is not True:
        raise SafeTestError("TARGETED_DELIVERY_OUTCOME_NOT_APPLIED")
    _assert_delivery_row(delivery_date, slot_b, status="SENT", attempt=1, safe_error=None,
                         operation_id=outcome_b_id)

    # Independently launch a fresh Python process to verify durable delivery rows.
    child_env = os.environ.copy()
    child_env["_MARKETBOT_PHASE3C_CHILD_TOKEN"] = secrets.token_hex(32)
    child = subprocess.run([
        sys.executable, str(Path(__file__).resolve()), "--_child-verify-delivery",
        "--market-date", delivery_date,
    ], cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=60, check=False)
    if child.returncode != 0 or "DELIVERY_PERSISTENCE_CHILD=PASS" not in child.stdout:
        raise SafeTestError("TARGETED_DELIVERY_PROCESS_PERSISTENCE_FAILED")
    print("DELIVERY_UNKNOWN_RECONCILIATION=PASS unreviewed_retry=DENIED reviewed_retry=PASS per_recipient_isolation=PASS persistence=PASS")

    # Use a fresh synthetic date/run/operation IDs for interrupted-run recovery.
    recovery_date = "2099-01-03"
    claim = test_client.call("run.claim", {"market_date": recovery_date, "run_id": _id()}, operation_id=_id())
    if claim.get("claim_authorized") is not True:
        raise SafeTestError("TARGETED_RECOVERY_CLAIM_FAILED")
    run_id = claim["run_id"]
    transitioned = test_client.call("run.transition", {
        "market_date": recovery_date, "run_id": run_id, "from": "CLAIMED", "to": "RUNNING",
    }, operation_id=_id())
    if transitioned.get("applied") is not True:
        raise SafeTestError("TARGETED_RECOVERY_START_FAILED")
    recover_begin = {"market_date": recovery_date, "recipient_hmac": slot_a, "attempt_id": _id(), "reviewed_retry": False}
    if test_client.call("delivery.begin", recover_begin, operation_id=_id()).get("authorized") is not True:
        raise SafeTestError("TARGETED_RECOVERY_BEGIN_FAILED")
    inflight = _wrangler_query(f"""
SELECT r.status AS run_status, d.status AS delivery_status, d.attempt
FROM run_claim r JOIN delivery_recipient d ON d.market_date=r.market_date
WHERE r.market_date='{recovery_date}' AND r.run_id='{run_id}' AND d.recipient_hmac='{slot_a}'
""".strip(), operation_name="recovery_inflight_precondition")
    if inflight != {"run_status": "RUNNING", "delivery_status": "SENDING", "attempt": 1}:
        raise SafeTestError("TARGETED_RECOVERY_PRECONDITION_FAILED")
    recovery_id = _id()
    recovered_result = test_client.call("run.recover", {"market_date": recovery_date, "run_id": run_id}, operation_id=recovery_id)
    if recovered_result.get("applied") is not True or recovered_result.get("status") != "DELIVERY_UNKNOWN":
        raise SafeTestError("TARGETED_RECOVERY_OPERATION_NOT_APPLIED")
    recovery = _wrangler_query(f"""
SELECT r.status AS run_status, d.status AS delivery_status, d.attempt, d.safe_error
FROM run_claim r JOIN delivery_recipient d ON d.market_date=r.market_date
WHERE r.market_date='{recovery_date}' AND r.run_id='{run_id}' AND d.recipient_hmac='{slot_a}'
""".strip(), operation_name="recovery_postcondition")
    expected_recovery = {
        "run_status": "DELIVERY_UNKNOWN", "delivery_status": "UNKNOWN", "attempt": 1,
        "safe_error": "RECOVERED_IN_FLIGHT_AFTER_RESTART",
    }
    if recovery != expected_recovery:
        raise SafeTestError("TARGETED_INTERRUPTED_RUN_RECOVERY_POSTCONDITION_FAILED")
    blocked_id = _id()
    _expect_error(lambda: test_client.call("delivery.begin", {
        **recover_begin, "attempt_id": _id(),
    }, operation_id=blocked_id), "DELIVERY_SLOT_NOT_SENDABLE", operation="delivery.begin", operation_id=blocked_id)
    _assert_delivery_row(recovery_date, slot_a, status="UNKNOWN", attempt=1,
                         safe_error="RECOVERED_IN_FLIGHT_AFTER_RESTART", operation_id=recovery_id)

    final = _wrangler_query(TARGETED_RESUME_SQL, operation_name="targeted_final_gate_check")
    if any(final[key] != 0 for key in ("cloud_quota_exclusive", "windows_sender_disabled", "cloud_delivery_enabled")):
        raise SafeTestError("TEST_OWNERSHIP_GATES_CHANGED")
    print("INTERRUPTED_RUN_RECOVERY=PASS resend_authorized=false; FINAL_GATES=0,0,0; provider_calls=0; Telegram_calls=0")


def client(api_secret: str, admin_secret: str | None = None, session: Any = requests) -> Any:
    return JournaledClient(D1StateClient(TEST_URL, api_secret, admin_secret=admin_secret, session=session))


class JournaledClient:
    """Write-ahead journal for synthetic mutations; stores no credentials or payloads."""

    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped

    def call(self, op: str, payload: dict[str, Any], *, operation_id: str | None = None, admin: bool = False) -> dict[str, Any]:
        if op == "state.get":
            if operation_id is None and not admin:
                return self._wrapped.call(op, payload)
            return self._wrapped.call(op, payload, operation_id=operation_id, admin=admin)
        op_id = operation_id or _id()
        _append_operation_event({"event": "STARTED", "operation": op, "operation_id": op_id, **_safe_journal_context(payload)})
        try:
            result = self._wrapped.call(op, payload, operation_id=op_id, admin=admin)
        except D1StateError as exc:
            _append_operation_event({
                "event": "CLIENT_ERROR", "operation": op, "operation_id": op_id,
                "error_code": str(exc), "failure_category": exc.failure_category or "UNKNOWN",
                "http_status": exc.http_status, "may_have_committed": exc.may_have_committed,
            })
            raise
        safe_result = {key: result[key] for key in ("authorized", "applied", "claim_authorized", "replayed", "attempt", "status") if key in result}
        _append_operation_event({"event": "CLIENT_RETURN", "operation": op, "operation_id": op_id, **_safe_journal_context(payload), "result": safe_result})
        return result


def _safe_journal_context(payload: dict[str, Any]) -> dict[str, Any]:
    """Record enough synthetic context to reconcile after interruption, no raw payloads."""
    context: dict[str, Any] = {}
    if isinstance(payload.get("market_date"), str):
        context["market_date"] = payload["market_date"]
    if isinstance(payload.get("run_id"), str) and payload["run_id"].startswith("3c"):
        context["run_id"] = payload["run_id"]
    recipient = payload.get("recipient_hmac")
    if recipient in TEST_RECIPIENTS:
        context["recipient_slot"] = "A" if recipient == TEST_RECIPIENTS[0] else "B"
    for name in ("status", "from", "to"):
        if isinstance(payload.get(name), str) and re.fullmatch(r"[A-Z_]{2,40}", payload[name]):
            context[name] = payload[name]
    if isinstance(payload.get("confirmed_sent"), bool):
        context["confirmed_sent"] = payload["confirmed_sent"]
    return context


def _append_operation_event(event: dict[str, Any]) -> None:
    OPERATION_JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    row = {"recorded_at_utc": datetime.now(timezone.utc).isoformat(), **event}
    with _JOURNAL_LOCK:
        with OPERATION_JOURNAL_FILE.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def state_snapshot(api_secret: str, *, market_date: str | None = None) -> dict[str, Any]:
    try:
        state = client(api_secret).call("state.get", {"market_date": market_date} if market_date else {})
    except Exception:
        raise SafeTestError("TEST_STATE_GET_FAILED") from None
    ownership = state.get("ownership")
    if state.get("schema_version") != 1 or state.get("test_mutation_mode") is not True or not isinstance(ownership, dict):
        raise SafeTestError("TEST_WORKER_TEST_IDENTITY_UNVERIFIED")
    if ownership != {"cloud_quota_exclusive": False, "windows_sender_disabled": False, "cloud_delivery_enabled": False}:
        raise SafeTestError("TEST_OWNERSHIP_GATES_NOT_CLOSED")
    return state


def _id() -> str:
    while True:
        operation_id = "3c" + secrets.token_hex(15)
        if operation_id not in _GENERATED_OPERATION_IDS:
            _GENERATED_OPERATION_IDS.add(operation_id)
            return operation_id


def _safe_id(value: str | None) -> str:
    return f"{value[:4]}…{value[-4:]}" if value else "unavailable"


def _expect_error(call, code: str, *, operation: str = "unknown", operation_id: str | None = None) -> None:
    try:
        result = call()
    except (D1StateError, D1OperationUncertain) as exc:
        status = str(exc.http_status) if exc.http_status is not None else "none"
        print(
            f"[PHASE3C ASSERT] operation={operation} expected=error:{code} "
            f"actual=client_error:{type(exc).__name__}:{str(exc)} http_status={status} "
            f"operation_id={_safe_id(operation_id or exc.operation_id)}"
        )
        if str(exc) == code:
            return
        if code == "QUOTA_EXHAUSTED" and isinstance(exc, D1QuotaExhausted):
            return
        if code == "MARKET_DATE_ALREADY_CLAIMED" and str(exc).endswith(code):
            return
        raise SafeTestError("EXPECTED_TEST_ERROR_NOT_OBSERVED") from None
    safe_result = {key: result[key] for key in ("authorized", "applied", "replayed", "attempt", "status") if isinstance(result, dict) and key in result}
    print(
        f"[PHASE3C ASSERT] operation={operation} expected=error:{code} "
        f"actual=client_return:{json.dumps(safe_result, ensure_ascii=False, sort_keys=True)} "
        f"http_status=not_exposed operation_id={_safe_id(operation_id)}"
    )
    raise SafeTestError("EXPECTED_TEST_ERROR_NOT_OBSERVED")


def _expect_non_authorizing_replay(call, *, operation: str, operation_id: str) -> None:
    """A duplicate authorization-bearing mutation must fail closed on replay."""
    try:
        call()
    except D1OperationUncertain as exc:
        if (
            str(exc) == "D1_OPERATION_ALREADY_COMMITTED_NO_REPLAY_AUTHORIZATION"
            and exc.operation == operation
            and exc.operation_id == operation_id
            and exc.failure_category == "REPLAY_INDICATES_PRIOR_COMMIT"
            and exc.may_have_committed is True
        ):
            return
        raise SafeTestError("NON_AUTHORIZING_REPLAY_DIAGNOSTIC_INVALID") from None
    except D1StateError:
        raise SafeTestError("NON_AUTHORIZING_REPLAY_RETURNED_UNEXPECTED_ERROR") from None
    raise SafeTestError("NON_AUTHORIZING_REPLAY_WAS_NOT_BLOCKED")


class _DropFirstAck:
    """Send the first request, then simulate losing its response to the client."""

    def __init__(self) -> None:
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        response = requests.post(*args, **kwargs)
        if self.calls == 1:
            raise requests.Timeout("synthetic response loss")
        return response


def _child_read(api_secret: str, market_date: str) -> int:
    state = state_snapshot(api_secret, market_date=market_date)
    run = state.get("run")
    passed = isinstance(run, dict) and run.get("market_date") == market_date
    print("PERSISTENCE_CHILD=PASS" if passed else "PERSISTENCE_CHILD=FAIL")
    return 0 if passed else 3


def _child_verify_delivery(market_date: str) -> int:
    rows = _wrangler_query(f"""
SELECT
 (SELECT status FROM delivery_recipient WHERE market_date='{market_date}' AND recipient_hmac='{TEST_RECIPIENTS[0]}') AS slot_a_status,
 (SELECT attempt FROM delivery_recipient WHERE market_date='{market_date}' AND recipient_hmac='{TEST_RECIPIENTS[0]}') AS slot_a_attempt,
 (SELECT status FROM delivery_recipient WHERE market_date='{market_date}' AND recipient_hmac='{TEST_RECIPIENTS[1]}') AS slot_b_status,
 (SELECT attempt FROM delivery_recipient WHERE market_date='{market_date}' AND recipient_hmac='{TEST_RECIPIENTS[1]}') AS slot_b_attempt
""".strip(), operation_name="delivery_persistence_child_read")
    expected = {"slot_a_status": "SENT", "slot_a_attempt": 2, "slot_b_status": "SENT", "slot_b_attempt": 1}
    passed = rows == expected
    print("DELIVERY_PERSISTENCE_CHILD=PASS" if passed else "DELIVERY_PERSISTENCE_CHILD=FAIL")
    return 0 if passed else 3


def run_remote_tests(api_secret: str, admin_secret: str) -> None:
    assert_reconciled_test_baseline(admin_secret=admin_secret)
    initial = state_snapshot(api_secret)
    print("TEST_IDENTITY=PASS authenticated_worker=true database_marker=true schema=1 gates=0,0,0")
    test_client = client(api_secret, admin_secret)

    quota_op = _id()
    first = test_client.call("quota.reserve", {"provider": "twelve_data", "credits": 1}, operation_id=quota_op)
    _expect_non_authorizing_replay(
        lambda: test_client.call("quota.reserve", {"provider": "twelve_data", "credits": 1}, operation_id=quota_op),
        operation="quota.reserve",
        operation_id=quota_op,
    )
    if first.get("authorized") is not True:
        raise SafeTestError("QUOTA_IDEMPOTENCY_CHECK_FAILED")
    _expect_error(lambda: test_client.call("quota.reserve", {"provider": "twelve_data", "credits": 2}, operation_id=quota_op), "OPERATION_ID_REUSED_WITH_DIFFERENT_REQUEST", operation="quota.reserve", operation_id=quota_op)
    print("QUOTA_IDEMPOTENCY=PASS first_authorized=true replay=blocked_without_reauthorization changed_payload=conflict")

    drop_ack = _DropFirstAck()
    ambiguous = JournaledClient(D1StateClient(TEST_URL, api_secret, session=drop_ack))
    ambiguous_id = _id()
    _expect_error(lambda: ambiguous.call("quota.reserve", {"provider": "twelve_data", "credits": 1}, operation_id=ambiguous_id), "D1_OPERATION_ALREADY_COMMITTED_NO_REPLAY_AUTHORIZATION", operation="quota.reserve", operation_id=ambiguous_id)
    if drop_ack.calls != 2:
        raise SafeTestError("AMBIGUOUS_RETRY_COUNT_INVALID")
    print("AMBIGUOUS_RESPONSE=PASS same_operation_id_retried=true second_authorization=false")

    concurrent = [_id() for _ in range(7)]
    def reserve_one(op_id: str) -> str:
        try:
            value = test_client.call("quota.reserve", {"provider": "twelve_data", "credits": 1}, operation_id=op_id)
            return "AUTHORIZED" if value.get("authorized") is True else "DENIED"
        except D1QuotaExhausted:
            return "QUOTA_EXHAUSTED"
    with ThreadPoolExecutor(max_workers=7) as pool:
        quota_results = list(pool.map(reserve_one, concurrent))
    if quota_results.count("AUTHORIZED") != 6 or quota_results.count("QUOTA_EXHAUSTED") != 1:
        raise SafeTestError("CONCURRENT_QUOTA_RESERVATION_CHECK_FAILED")
    print("CONCURRENT_QUOTA=PASS authorized=6 exhausted=1 minute_cap=8")

    claim_date = "2099-01-01"
    claim_requests = [(_id(), _id()) for _ in range(8)]
    def claim_one(item: tuple[str, str]) -> str:
        op_id, run_id = item
        try:
            value = test_client.call("run.claim", {"market_date": claim_date, "run_id": run_id}, operation_id=op_id)
            return "CLAIMED" if value.get("claim_authorized") is True else "DENIED"
        except D1StateError:
            return "CONFLICT"
    with ThreadPoolExecutor(max_workers=8) as pool:
        claim_results = list(pool.map(claim_one, claim_requests))
    if claim_results.count("CLAIMED") != 1 or claim_results.count("CONFLICT") != 7:
        raise SafeTestError("CONCURRENT_RUN_CLAIM_CHECK_FAILED")
    print("CONCURRENT_RUN_CLAIM=PASS claims=1 conflicts=7")

    child_env = os.environ.copy()
    child_env["MARKETBOT_D1_TEST_HMAC_SECRET"] = api_secret
    child_env["_MARKETBOT_PHASE3C_CHILD_TOKEN"] = secrets.token_hex(32)
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--_child-read", "--market-date", claim_date], cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=30, check=False)
    if child.returncode != 0 or "PERSISTENCE_CHILD=PASS" not in child.stdout:
        raise SafeTestError("INDEPENDENT_PROCESS_PERSISTENCE_FAILED")
    print("INDEPENDENT_PROCESS_PERSISTENCE=PASS")

    delivery_date = "2099-01-02"
    test_client.call("run.claim", {"market_date": delivery_date, "run_id": _id()}, operation_id=_id())
    slot_a, slot_b = TEST_RECIPIENTS
    begin_id = _id()
    begin_payload = {"market_date": delivery_date, "recipient_hmac": slot_a, "attempt_id": _id(), "reviewed_retry": False}
    began = test_client.call("delivery.begin", begin_payload, operation_id=begin_id)
    _expect_non_authorizing_replay(
        lambda: test_client.call("delivery.begin", begin_payload, operation_id=begin_id),
        operation="delivery.begin",
        operation_id=begin_id,
    )
    if began.get("authorized") is not True:
        raise SafeTestError("DELIVERY_IDEMPOTENCY_CHECK_FAILED")
    test_client.call("delivery.outcome", {"market_date": delivery_date, "recipient_hmac": slot_a, "status": "UNKNOWN", "safe_error": "SIMULATED_RESPONSE_LOSS"}, operation_id=_id())
    blocked_retry_id = _id()
    _expect_error(lambda: test_client.call("delivery.begin", {**begin_payload, "attempt_id": _id(), "reviewed_retry": True}, operation_id=blocked_retry_id), "DELIVERY_SLOT_NOT_SENDABLE", operation="delivery.begin", operation_id=blocked_retry_id)
    test_client.call("delivery.reconcile", {"market_date": delivery_date, "recipient_hmac": slot_a, "reviewer_hmac": slot_b, "confirmed_sent": False}, operation_id=_id(), admin=True)
    reviewed = test_client.call("delivery.begin", {**begin_payload, "attempt_id": _id(), "reviewed_retry": True}, operation_id=_id())
    if reviewed.get("authorized") is not True:
        raise SafeTestError("REVIEWED_RETRY_CHECK_FAILED")
    test_client.call("delivery.outcome", {"market_date": delivery_date, "recipient_hmac": slot_a, "status": "SENT"}, operation_id=_id())
    second = test_client.call("delivery.begin", {"market_date": delivery_date, "recipient_hmac": slot_b, "attempt_id": _id(), "reviewed_retry": False}, operation_id=_id())
    if second.get("authorized") is not True:
        raise SafeTestError("PER_RECIPIENT_DELIVERY_ISOLATION_FAILED")
    test_client.call("delivery.outcome", {"market_date": delivery_date, "recipient_hmac": slot_b, "status": "SENT"}, operation_id=_id())
    print("DELIVERY_TRANSITIONS=PASS UNKNOWN_no_auto_resend=true explicit_reviewed_retry=true recipients_independent=true")

    recover_date = "2099-01-03"
    recover_run_id = _id()
    test_client.call("run.claim", {"market_date": recover_date, "run_id": recover_run_id}, operation_id=_id())
    test_client.call("run.transition", {"market_date": recover_date, "run_id": recover_run_id, "from": "CLAIMED", "to": "RUNNING"}, operation_id=_id())
    recover_begin = {"market_date": recover_date, "recipient_hmac": slot_a, "attempt_id": _id(), "reviewed_retry": False}
    test_client.call("delivery.begin", recover_begin, operation_id=_id())
    test_client.call("run.recover", {"market_date": recover_date, "run_id": recover_run_id}, operation_id=_id())
    recovered = test_client.call("state.get", {"market_date": recover_date})
    if recovered.get("run", {}).get("status") != "DELIVERY_UNKNOWN":
        raise SafeTestError("INTERRUPTED_RUN_RECOVERY_CHECK_FAILED")
    recovery_retry_id = _id()
    _expect_error(lambda: test_client.call("delivery.begin", {**recover_begin, "attempt_id": _id()}, operation_id=recovery_retry_id), "DELIVERY_SLOT_NOT_SENDABLE", operation="delivery.begin", operation_id=recovery_retry_id)
    print("INTERRUPTED_RUN_RECOVERY=PASS status=DELIVERY_UNKNOWN resend_authorized=false")

    final = state_snapshot(api_secret)
    if final["ownership"] != initial["ownership"]:
        raise SafeTestError("TEST_OWNERSHIP_GATES_CHANGED")
    assert_remote_test_database_isolated(initially_empty=False, admin_secret=admin_secret)
    print("FINAL_GATES=0,0,0; provider_calls=0; Telegram_calls=0")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-synthetic-mutations", action="store_true", help="perform synthetic writes against TEST Worker + D1")
    parser.add_argument("--resume-targeted-delivery", action="store_true", help="continue only from the user-reconciled Phase 3C partial delivery state")
    parser.add_argument("--diagnose-targeted-read", action="store_true", help="run only the read-only targeted baseline SQL through the pinned TEST Wrangler config")
    parser.add_argument("--confirm-reconciled-baseline", action="store_true", help="confirm the exact prior synthetic reconciliation is expected and must not be replayed")
    parser.add_argument("--database-id", help="identity assertion only; cannot override the pinned TEST database")
    parser.add_argument("--_child-read", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_child-verify-delivery", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--market-date", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        assert_test_target(args.database_id)
        if args._child_read:
            if len(os.environ.get("_MARKETBOT_PHASE3C_CHILD_TOKEN", "")) != 64:
                raise SafeTestError("INTERNAL_CHILD_INVOCATION_REJECTED")
            secret = os.environ.get("MARKETBOT_D1_TEST_HMAC_SECRET", "")
            if len(secret.encode("utf-8")) < 32 or not args.market_date:
                raise SafeTestError("INTERNAL_CHILD_INVOCATION_REJECTED")
            return _child_read(secret, args.market_date)
        if args._child_verify_delivery:
            if len(os.environ.get("_MARKETBOT_PHASE3C_CHILD_TOKEN", "")) != 64 or not args.market_date:
                raise SafeTestError("INTERNAL_CHILD_INVOCATION_REJECTED")
            return _child_verify_delivery(args.market_date)
        if args.diagnose_targeted_read:
            if args.execute_synthetic_mutations or args.resume_targeted_delivery:
                raise SafeTestError("SELECT_ONE_EXECUTION_MODE")
            if args.database_id != TEST_DATABASE_ID:
                raise SafeTestError("EXPLICIT_TEST_DATABASE_ASSERTION_REQUIRED")
            row = _wrangler_query(TARGETED_RESUME_SQL, operation_name="targeted_resume_baseline_diagnostic")
            print(f"TEST_D1_READONLY_DIAGNOSTIC=PASS fields={len(row)} target=marketbot-test/{TEST_DATABASE_ID[:4]}…{TEST_DATABASE_ID[-4:]}")
            print("REMOTE_MUTATIONS=0; worker_api_requests=0; provider_calls=0; Telegram_calls=0")
            return 0
        if not args.execute_synthetic_mutations and not args.resume_targeted_delivery:
            print("PLAN_ONLY: no network calls made; no remote writes made")
            print(f"Pinned target: {TEST_WORKER_NAME} / marketbot-test / {TEST_DATABASE_ID}")
            print("Full-suite precondition: initial/reconciled baseline only; do not use it to resume progressed TEST state")
            print("For the reconciled partial delivery run, use --resume-targeted-delivery with the TEST database assertion and --confirm-reconciled-baseline")
            return 0
        if args.database_id != TEST_DATABASE_ID:
            raise SafeTestError("EXPLICIT_TEST_DATABASE_ASSERTION_REQUIRED")
        if args.resume_targeted_delivery and args.execute_synthetic_mutations:
            raise SafeTestError("SELECT_ONE_EXECUTION_MODE")
        if args.resume_targeted_delivery and args.database_id != TEST_DATABASE_ID:
            raise SafeTestError("EXPLICIT_TEST_DATABASE_ASSERTION_REQUIRED")
        if args.confirm_reconciled_baseline and not args.resume_targeted_delivery:
            raise SafeTestError("RECONCILED_BASELINE_CONFIRMATION_REQUIRES_TARGETED_RESUME_MODE")
        if UNCERTAIN_OPERATION_RECORD.exists():
            raise SafeTestError("UNRESOLVED_REMOTE_OPERATION_REQUIRES_RECONCILIATION")
        api_secret, admin_secret = get_test_credentials()
        if args.resume_targeted_delivery:
            run_targeted_delivery_continuation(
                api_secret, admin_secret,
                confirm_reconciled_baseline=args.confirm_reconciled_baseline,
            )
        elif args.execute_synthetic_mutations:
            run_remote_tests(api_secret, admin_secret)
        else:
            raise SafeTestError("EXECUTION_MODE_REQUIRED")
        return 0
    except SafeTestError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except D1OperationUncertain as exc:
        record = _record_uncertain_operation(exc)
        print(_format_operation_failure(exc), file=sys.stderr)
        print(f"UNCERTAIN_OPERATION_RECORD={record}", file=sys.stderr)
        return 2
    except D1StateError as exc:
        print(_format_operation_failure(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"PHASE3C_TEST_FAILED:{type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
