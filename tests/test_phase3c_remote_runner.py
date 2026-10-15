"""Offline safety tests for the Phase 3C remote test runner."""

import importlib.util
import inspect
import json
import sqlite3
from pathlib import Path

import pytest
from persistence.d1_client import D1DeliveryDenied, D1OperationUncertain


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "cloudflare" / "marketbot-state-worker" / "scripts" / "remote_d1_phase3c_test.py"
SPEC = importlib.util.spec_from_file_location("phase3c_remote_runner", RUNNER_PATH)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(runner)


def test_target_validation_pins_test_config_and_database() -> None:
    runner.assert_test_target()


def test_production_database_id_supplied_to_execution_is_rejected_before_network(monkeypatch, capsys) -> None:
    def unexpected(*args, **kwargs):
        raise AssertionError("a production identity must fail before any remote client is constructed")

    monkeypatch.setattr(runner, "get_test_credentials", unexpected)
    assert runner.main(["--execute-synthetic-mutations", "--database-id", runner.PRODUCTION_DATABASE_ID]) == 2
    assert "TEST_DATABASE_ASSERTION_MISMATCH" in capsys.readouterr().err


def test_production_database_id_supplied_to_targeted_resume_is_rejected_before_network(monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        raise AssertionError("production target must be rejected before credentials or remote calls")

    monkeypatch.setattr(runner, "get_test_credentials", unexpected)
    assert runner.main([
        "--resume-targeted-delivery", "--database-id", runner.PRODUCTION_DATABASE_ID,
        "--confirm-reconciled-baseline",
    ]) == 2
    assert "TEST_DATABASE_ASSERTION_MISMATCH" in capsys.readouterr().err


def test_default_run_is_plan_only_and_makes_no_remote_client(monkeypatch, capsys) -> None:
    def unexpected(*args, **kwargs):
        raise AssertionError("remote client must not be constructed in plan mode")

    monkeypatch.setattr(runner, "D1StateClient", unexpected)
    assert runner.main([]) == 0
    output = capsys.readouterr().out
    assert "PLAN_ONLY" in output
    assert "no network calls made" in output


def test_internal_child_mode_cannot_run_without_ephemeral_parent_token(monkeypatch, capsys) -> None:
    monkeypatch.delenv("_MARKETBOT_PHASE3C_CHILD_TOKEN", raising=False)
    assert runner.main(["--_child-read"]) == 2
    assert "INTERNAL_CHILD_INVOCATION_REJECTED" in capsys.readouterr().err


def test_test_read_uses_fixed_test_endpoint_and_accepts_closed_gates(monkeypatch) -> None:
    seen = {}

    class FakeClient:
        def __init__(self, url, secret, **kwargs):
            seen["url"] = url
            seen["secret"] = secret

        def call(self, op, payload):
            assert op == "state.get"
            return {
                "schema_version": 1,
                "generation": 4,
                "runs": {},
                "test_mutation_mode": True,
                "ownership": {
                    "cloud_quota_exclusive": False,
                    "windows_sender_disabled": False,
                    "cloud_delivery_enabled": False,
                },
            }

    monkeypatch.setattr(runner, "D1StateClient", FakeClient)
    snapshot = runner.state_snapshot("test-only-secret-".ljust(40, "x"))
    assert seen["url"] == runner.TEST_URL
    assert seen["url"] != runner.PRODUCTION_URL
    assert snapshot["schema_version"] == 1
    assert snapshot["runs"] == {}


def test_nonclosed_gate_state_stops_read_validation(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def call(self, op, payload):
            return {"schema_version": 1, "generation": 1, "runs": {}, "test_mutation_mode": True, "ownership": {
                "cloud_quota_exclusive": True,
                "windows_sender_disabled": False,
                "cloud_delivery_enabled": False,
            }}

    monkeypatch.setattr(runner, "D1StateClient", FakeClient)
    with pytest.raises(runner.SafeTestError, match="TEST_OWNERSHIP_GATES_NOT_CLOSED"):
        runner.state_snapshot("test-only-secret-".ljust(40, "x"))


def test_secret_equal_to_non_test_credential_is_rejected(monkeypatch):
    monkeypatch.setenv("MARKETBOT_D1_STATE_HMAC_SECRET", "same-test-secret-value".ljust(40, "x"))
    monkeypatch.setenv("MARKETBOT_D1_TEST_HMAC_SECRET", "same-test-secret-value".ljust(40, "x"))
    monkeypatch.setenv("MARKETBOT_D1_TEST_ADMIN_HMAC_SECRET", "different-test-admin-secret".ljust(40, "y"))
    with pytest.raises(runner.SafeTestError, match="TEST_SECRET_MATCHES_NON_TEST_CREDENTIAL"):
        runner.get_test_credentials()


def test_test_credentials_must_be_independent(monkeypatch):
    monkeypatch.setenv("MARKETBOT_D1_TEST_HMAC_SECRET", "synthetic-shared-secret-value".ljust(40, "x"))
    monkeypatch.setenv("MARKETBOT_D1_TEST_ADMIN_HMAC_SECRET", "synthetic-shared-secret-value".ljust(40, "x"))
    with pytest.raises(runner.SafeTestError, match="TEST_SECRETS_MUST_BE_INDEPENDENT"):
        runner.get_test_credentials()


def test_test_database_assertion_cannot_override_pinned_test_target():
    with pytest.raises(runner.SafeTestError, match="TEST_DATABASE_ASSERTION_MISMATCH"):
        runner.assert_test_target(runner.PRODUCTION_DATABASE_ID)


def test_preflight_requires_exact_closed_gate_identity_and_empty_test_state(monkeypatch):
    expected = {
        "schema_version": 1,
        "cloud_quota_exclusive": 0,
        "windows_sender_disabled": 0,
        "cloud_delivery_enabled": 0,
        "operation_count": 0,
        "reservation_count": 0,
        "run_count": 0,
        "delivery_count": 0,
        "audit_count": 0,
        "quota_bucket_count": 0,
        "database_id": runner.TEST_DATABASE_ID,
        "worker_name": runner.TEST_WORKER_NAME,
        "mode": runner.TEST_MODE,
        "attestation_hash": __import__("hashlib").sha256("synthetic-test-admin".encode()).hexdigest(),
        "minute_used": 0,
        "day_used": 0,
    }
    monkeypatch.setattr(runner, "_wrangler_query", lambda sql, **kwargs: expected)
    assert runner.assert_remote_test_database_isolated(initially_empty=True, admin_secret="synthetic-test-admin") == expected
    monkeypatch.setattr(runner, "_wrangler_query", lambda sql, **kwargs: {**expected, "database_id": runner.PRODUCTION_DATABASE_ID})
    with pytest.raises(runner.SafeTestError, match="TEST_D1_IDENTITY_MISMATCH"):
        runner.assert_remote_test_database_isolated(initially_empty=True, admin_secret="synthetic-test-admin")


def test_preflight_rejects_wrong_admin_attestation_and_existing_business_rows(monkeypatch):
    row = {
        "schema_version": 1,
        "cloud_quota_exclusive": 0,
        "windows_sender_disabled": 0,
        "cloud_delivery_enabled": 0,
        "operation_count": 0,
        "reservation_count": 0,
        "run_count": 0,
        "delivery_count": 0,
        "audit_count": 0,
        "quota_bucket_count": 0,
        "database_id": runner.TEST_DATABASE_ID,
        "worker_name": runner.TEST_WORKER_NAME,
        "mode": runner.TEST_MODE,
        "attestation_hash": __import__("hashlib").sha256(b"other-admin-secret").hexdigest(),
        "minute_used": 0,
        "day_used": 0,
    }
    monkeypatch.setattr(runner, "_wrangler_query", lambda sql, **kwargs: row)
    with pytest.raises(runner.SafeTestError, match="TEST_D1_SECRET_ATTESTATION_MISMATCH"):
        runner.assert_remote_test_database_isolated(initially_empty=True, admin_secret="synthetic-test-admin")
    row["operation_count"] = 1
    with pytest.raises(runner.SafeTestError, match="TEST_D1_BUSINESS_STATE_NOT_EMPTY_CLEANUP_REQUIRED"):
        runner.assert_remote_test_database_isolated(initially_empty=True, admin_secret="other-admin-secret")


def test_worker_state_read_must_prove_test_mode_and_closed_gates(monkeypatch):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def call(self, op, payload):
            return {"schema_version": 1, "test_mutation_mode": False, "ownership": {
                "cloud_quota_exclusive": False,
                "windows_sender_disabled": False,
                "cloud_delivery_enabled": False,
            }}

    monkeypatch.setattr(runner, "D1StateClient", FakeClient)
    with pytest.raises(runner.SafeTestError, match="TEST_WORKER_TEST_IDENTITY_UNVERIFIED"):
        runner.state_snapshot("x" * 40)


def test_uncertain_operation_diagnostic_and_manifest_are_safe_and_reconcilable(tmp_path, monkeypatch):
    record_path = tmp_path / "unresolved_remote_operation.json"
    monkeypatch.setattr(runner, "UNCERTAIN_OPERATION_RECORD", record_path)
    operation_id = "3c" + "a" * 26 + "beef"
    exc = D1OperationUncertain(
        "D1_WORKER_RESPONSE_UNCERTAIN",
        operation="quota.reserve",
        operation_id=operation_id,
        failure_category="HTTP_TIMEOUT",
        may_have_committed=True,
        attempts=2,
    )
    line = runner._format_operation_failure(exc)
    assert "operation=quota.reserve" in line
    assert f"operation_id={operation_id[:4]}…{operation_id[-4:]}" in line
    assert "http_status=none" in line
    assert "category=HTTP_TIMEOUT" in line
    assert "may_have_committed=true" in line
    assert operation_id not in line

    assert runner._record_uncertain_operation(exc) == str(record_path)
    data = json.loads(record_path.read_text(encoding="utf-8"))
    assert data["operation_id"] == operation_id
    assert data["target"]["database"] == "marketbot-test"
    assert data["reconciled"] is False
    assert runner._record_uncertain_operation(exc) == "EXISTING_UNRESOLVED_RECORD_PRESERVED"


def test_existing_uncertain_operation_blocks_another_remote_run(tmp_path, monkeypatch, capsys):
    record_path = tmp_path / "unresolved_remote_operation.json"
    record_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(runner, "UNCERTAIN_OPERATION_RECORD", record_path)
    monkeypatch.setattr(runner, "get_test_credentials", lambda: (_ for _ in ()).throw(AssertionError("must stop before credentials or remote access")))
    assert runner.main(["--execute-synthetic-mutations", "--database-id", runner.TEST_DATABASE_ID]) == 2
    assert "UNRESOLVED_REMOTE_OPERATION_REQUIRES_RECONCILIATION" in capsys.readouterr().err


def test_runner_accepts_client_replay_refusal_as_expected_idempotency_result():
    operation_id = "3c" + "1" * 26 + "beef"

    def replay():
        raise D1OperationUncertain(
            "D1_OPERATION_ALREADY_COMMITTED_NO_REPLAY_AUTHORIZATION",
            operation="quota.reserve",
            operation_id=operation_id,
            http_status=200,
            failure_category="REPLAY_INDICATES_PRIOR_COMMIT",
            may_have_committed=True,
            attempts=2,
        )

    runner._expect_non_authorizing_replay(replay, operation="quota.reserve", operation_id=operation_id)

    with pytest.raises(runner.SafeTestError, match="NON_AUTHORIZING_REPLAY_WAS_NOT_BLOCKED"):
        runner._expect_non_authorizing_replay(lambda: {"authorized": False}, operation="quota.reserve", operation_id=operation_id)


def test_targeted_reconcile_postcondition_matches_worker_contract(monkeypatch):
    operation_id = "3cb759039c7fea61eff99cfe8a930e3e"
    monkeypatch.setattr(runner, "_wrangler_query", lambda sql, **kwargs: {
        "status": "FAILED_SAFE",
        "attempt": 1,
        "safe_error": "SIMULATED_RESPONSE_LOSS",
        "reconciliation_code": "OPERATOR_CONFIRMED_NOT_SENT",
        "last_operation_id": operation_id,
    })

    # delivery.reconcile records its decision separately and intentionally keeps
    # the original UNKNOWN safe_error for audit/history.
    runner._assert_delivery_row(
        "2099-01-02",
        runner.TEST_RECIPIENTS[0],
        status="FAILED_SAFE",
        attempt=1,
        safe_error="SIMULATED_RESPONSE_LOSS",
        reconciliation_code="OPERATOR_CONFIRMED_NOT_SENT",
        operation_id=operation_id,
    )


def test_targeted_postcondition_failure_reports_safe_context_and_masks_id(monkeypatch):
    operation_id = "3cb759039c7fea61eff99cfe8a930e3e"
    monkeypatch.setattr(runner, "_wrangler_query", lambda sql, **kwargs: {
        "status": "UNKNOWN",
        "attempt": 1,
        "safe_error": "SIMULATED_RESPONSE_LOSS",
        "reconciliation_code": None,
        "last_operation_id": "prior-operation-id",
    })

    with pytest.raises(runner.SafeTestError) as exc_info:
        runner._assert_delivery_row(
            "2099-01-02",
            runner.TEST_RECIPIENTS[0],
            status="FAILED_SAFE",
            attempt=1,
            safe_error="SIMULATED_RESPONSE_LOSS",
            reconciliation_code="OPERATOR_CONFIRMED_NOT_SENT",
            operation_id=operation_id,
        )

    message = str(exc_info.value)
    assert "TARGETED_DELIVERY_POSTCONDITION_FAILED" in message
    assert "operation=delivery.state_readback" in message
    assert "date=2099-01-02" in message
    assert f"operation_id={operation_id[:4]}…{operation_id[-4:]}" in message
    assert operation_id not in message


def test_reconciled_remote_baseline_allows_only_the_confirmed_prior_reservation(tmp_path, monkeypatch):
    record_path = tmp_path / "reconciled_operations.json"
    record_path.write_text((ROOT / "output/qa/github_actions_migration_phase3c/reconciled_operations.json").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(runner, "RECONCILED_OPERATIONS_FILE", record_path)
    empty_preflight = {
        "schema_version": 1,
        "cloud_quota_exclusive": 0,
        "windows_sender_disabled": 0,
        "cloud_delivery_enabled": 0,
        "operation_count": 1,
        "reservation_count": 1,
        "run_count": 0,
        "delivery_count": 0,
        "audit_count": 0,
        "quota_bucket_count": 2,
        "database_id": runner.TEST_DATABASE_ID,
        "worker_name": runner.TEST_WORKER_NAME,
        "mode": runner.TEST_MODE,
        "attestation_hash": __import__("hashlib").sha256(b"synthetic-test-admin").hexdigest(),
        "minute_used": 0,
        "day_used": 0,
    }
    detail = {
        "reconciled_operation_type": "quota.reserve",
        "reconciled_operation_status": "COMMITTED",
        "synthetic_operation_count": 1,
        "synthetic_reservation_count": 1,
        "reconciled_provider": "twelve_data",
        "reconciled_credits": 1,
        "reconciled_utc_day": "2026-10-14",
        "reconciled_utc_minute": "2026-10-14T21:25Z",
        "current_minute_usage_rows": 0,
        "current_day_usage_rows": 0,
    }
    monkeypatch.setattr(runner, "_wrangler_query", lambda sql, **kwargs: empty_preflight if sql == runner.PREFLIGHT_SQL else {**empty_preflight, **detail})
    runner._GENERATED_OPERATION_IDS.clear()
    reconciled = runner.assert_reconciled_test_baseline(admin_secret="synthetic-test-admin")
    assert reconciled["operation_id"] == "3c57c52fcc8083b8a2944752c9b3d40d"
    assert runner._id() != reconciled["operation_id"]

    bad_detail = {**detail, "synthetic_operation_count": 2}
    monkeypatch.setattr(runner, "_wrangler_query", lambda sql, **kwargs: empty_preflight if sql == runner.PREFLIGHT_SQL else {**empty_preflight, **bad_detail})
    with pytest.raises(runner.SafeTestError, match="RECONCILED_TEST_BASELINE_DOES_NOT_MATCH_REMOTE_TEST_D1"):
        runner.assert_reconciled_test_baseline(admin_secret="synthetic-test-admin")


def test_expected_error_diagnostic_includes_operation_expected_actual_status_and_safe_id(capsys):
    operation_id = "3c" + "2" * 26 + "abcd"

    def denied():
        raise D1DeliveryDenied(
            "DELIVERY_SLOT_NOT_SENDABLE",
            operation="delivery.begin",
            operation_id=operation_id,
            http_status=409,
            failure_category="DELIVERY_STATE_DENIAL",
            may_have_committed=True,
            attempts=1,
        )

    runner._expect_error(denied, "DELIVERY_SLOT_NOT_SENDABLE", operation="delivery.begin", operation_id=operation_id)
    output = capsys.readouterr().out
    assert "operation=delivery.begin" in output
    assert "expected=error:DELIVERY_SLOT_NOT_SENDABLE" in output
    assert "actual=client_error:D1DeliveryDenied:DELIVERY_SLOT_NOT_SENDABLE" in output
    assert "http_status=409" in output
    assert f"operation_id={operation_id[:4]}…{operation_id[-4:]}" in output
    assert operation_id not in output


def test_journaled_client_write_ahead_preserves_operation_id_without_payload_or_secrets(tmp_path, monkeypatch):
    journal = tmp_path / "operations.jsonl"
    monkeypatch.setattr(runner, "OPERATION_JOURNAL_FILE", journal)

    class FakeClient:
        def call(self, op, payload, *, operation_id=None, admin=False):
            assert payload["secret_like_payload"] == "never-journal-this"
            return {"authorized": False, "replayed": True}

    operation_id = "3c" + "3" * 26 + "cafe"
    result = runner.JournaledClient(FakeClient()).call(
        "quota.reserve", {"secret_like_payload": "never-journal-this"}, operation_id=operation_id
    )
    rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in rows] == ["STARTED", "CLIENT_RETURN"]
    assert all(row["operation_id"] == operation_id for row in rows)
    assert "never-journal-this" not in journal.read_text(encoding="utf-8")
    assert result["authorized"] is False


def test_operation_journal_keeps_resume_context_without_sensitive_payload(tmp_path, monkeypatch):
    journal = tmp_path / "operations.jsonl"
    monkeypatch.setattr(runner, "OPERATION_JOURNAL_FILE", journal)

    class FakeClient:
        def call(self, op, payload, *, operation_id=None, admin=False):
            return {"applied": True}

    runner.JournaledClient(FakeClient()).call(
        "delivery.reconcile",
        {"market_date": "2099-01-02", "recipient_hmac": runner.TEST_RECIPIENTS[0],
         "reviewer_hmac": runner.TEST_RECIPIENTS[1], "confirmed_sent": False,
         "private_text": "do-not-journal"},
        operation_id="3c" + "9" * 30,
        admin=True,
    )
    body = journal.read_text(encoding="utf-8")
    records = [json.loads(line) for line in body.splitlines()]
    assert all(row["market_date"] == "2099-01-02" and row["recipient_slot"] == "A" for row in records)
    assert all("private_text" not in row and "reviewer_hmac" not in row for row in records)
    assert "do-not-journal" not in body


def _targeted_resume_row(admin_secret="synthetic-test-admin"):
    import hashlib

    return {
        "schema_version": 1, "cloud_quota_exclusive": 0, "windows_sender_disabled": 0,
        "cloud_delivery_enabled": 0, "database_id": runner.TEST_DATABASE_ID,
        "worker_name": runner.TEST_WORKER_NAME, "mode": runner.TEST_MODE,
        "attestation_hash": hashlib.sha256(admin_secret.encode()).hexdigest(),
        "synthetic_reservations": 9, "reservations_oct14": 1, "reservations_oct15": 8,
        "quota_day_oct15_used": 8, "max_oct15_minute_used": 8,
        "synthetic_claims": 2, "delivery_run_status": "CLAIMED", "synthetic_deliveries": 1,
        "reconciled_status": "FAILED_SAFE", "reconciled_attempt": 1,
        "reconciled_safe_error": "SIMULATED_RESPONSE_LOSS",
        "reconciled_code": "OPERATOR_CONFIRMED_NOT_SENT",
        "reconciled_last_operation_id": runner.RECONCILIATION_OPERATION_ID,
        "reconciled_operation_type": "delivery.reconcile",
        "reconciled_operation_status": "COMMITTED", "reconciled_audit_matches": 1,
        "second_recipient_rows": 0,
        "quota_operations": 9, "claim_operations": 2, "begin_operations": 2,
        "outcome_operations": 1, "reconcile_operations": 1, "recovery_date_rows": 0,
    }


def test_targeted_resume_requires_exact_reconciled_test_snapshot(monkeypatch):
    row = _targeted_resume_row()
    monkeypatch.setattr(runner, "_wrangler_query", lambda sql, **kwargs: row)
    assert runner.assert_targeted_resume_baseline(admin_secret="synthetic-test-admin") == row

    changed = {**row, "reconciled_attempt": 2}
    monkeypatch.setattr(runner, "_wrangler_query", lambda sql, **kwargs: changed)
    with pytest.raises(runner.SafeTestError, match="TARGETED_RESUME_REMOTE_STATE_MISMATCH_NO_MUTATIONS_SENT"):
        runner.assert_targeted_resume_baseline(admin_secret="synthetic-test-admin")


def test_targeted_resume_rejects_missing_reconciled_baseline_confirmation_before_remote_read(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("confirmation gate must run before any remote operation")

    monkeypatch.setattr(runner, "assert_targeted_resume_baseline", unexpected)
    with pytest.raises(runner.SafeTestError, match="EXPLICIT_RECONCILED_BASELINE_CONFIRMATION_REQUIRED"):
        runner.run_targeted_delivery_continuation("x" * 32, "y" * 32, confirm_reconciled_baseline=False)


def test_targeted_resume_requires_exact_committed_reconciliation_and_audit(monkeypatch):
    row = _targeted_resume_row()
    seen = {}

    def read(sql, **kwargs):
        seen["sql"] = sql
        return row

    monkeypatch.setattr(runner, "_wrangler_query", read)
    assert runner.assert_targeted_resume_baseline(admin_secret="synthetic-test-admin")["reconciled_audit_matches"] == 1
    assert runner.RECONCILIATION_OPERATION_ID in seen["sql"]
    assert "FROM audit_event" in seen["sql"]
    assert "OPERATOR_CONFIRMED_NOT_SENT" in seen["sql"]
    assert "reconciliation_code" in seen["sql"]
    assert "delivery.reconcile" not in inspect.getsource(runner.run_targeted_delivery_continuation)

    for key, invalid in (
        ("reconciled_operation_status", "PENDING"),
        ("reconciled_audit_matches", 0),
        ("reconciled_last_operation_id", "different-operation"),
        ("reconciled_safe_error", ""),
    ):
        changed = {**row, key: invalid}
        monkeypatch.setattr(runner, "_wrangler_query", lambda sql, **kwargs: changed)
        with pytest.raises(runner.SafeTestError, match="TARGETED_RESUME_REMOTE_STATE_MISMATCH_NO_MUTATIONS_SENT"):
            runner.assert_targeted_resume_baseline(admin_secret="synthetic-test-admin")


def test_phase3c_cleanup_preserves_non_synthetic_reservation_and_shared_buckets():
    connection = sqlite3.connect(":memory:")
    migration = (ROOT / "cloudflare/marketbot-state-worker/migrations/0001_initial.sql").read_text(encoding="utf-8")
    connection.executescript(migration)
    connection.execute("CREATE TABLE test_environment_identity(singleton INTEGER PRIMARY KEY, database_id TEXT, worker_name TEXT, mode TEXT, attestation_hash TEXT)")
    connection.execute(
        "INSERT INTO test_environment_identity VALUES(1, ?, ?, ?, ?)",
        (runner.TEST_DATABASE_ID, runner.TEST_WORKER_NAME, runner.TEST_MODE, "a" * 64),
    )
    connection.execute(
        "INSERT INTO quota_reservation VALUES(?, 'twelve_data', '2026-10-15', '2026-10-15T12:34Z', 2, '2026-10-15T12:34:00Z')",
        ("3c" + "a" * 30,),
    )
    connection.execute(
        "INSERT INTO quota_reservation VALUES('external-reservation', 'twelve_data', '2026-10-15', '2026-10-15T12:34Z', 1, '2026-10-15T12:34:01Z')"
    )
    connection.execute(
        "INSERT INTO operations VALUES('external-operation', 'run.claim', ?, '2026-10-15T12:34:00Z', 'COMMITTED')",
        ("b" * 64,),
    )
    connection.execute(
        "INSERT INTO run_claim(market_date,run_id,status,claim_operation_id,last_operation_id,claimed_at) VALUES('2026-10-15','external-run','CLAIMED','external-operation','external-operation','2026-10-15T12:34:00Z')"
    )
    connection.execute(
        "INSERT INTO delivery_recipient(market_date,recipient_hmac,status,attempt,attempt_id,last_operation_id,send_started_at,updated_at) VALUES('2026-10-15',?,'SENT',1,'external-attempt','external-operation','2026-10-15T12:34:00Z','2026-10-15T12:34:00Z')",
        ("hmac256:" + "f" * 64,),
    )
    connection.execute(
        "INSERT INTO audit_event VALUES('external-audit','EXTERNAL','external-subject','external-actor','2026-10-15T12:34:00Z')"
    )
    connection.execute("INSERT INTO quota_bucket VALUES('day', '2026-10-15', 3, 800)")
    connection.execute("INSERT INTO quota_bucket VALUES('minute', '2026-10-15T12:34Z', 3, 8)")
    cleanup = (ROOT / "cloudflare/marketbot-state-worker/scripts/cleanup_phase3c_test_data.sql").read_text(encoding="utf-8")
    connection.executescript(cleanup.replace("__TEST_ADMIN_SECRET_SHA256__", "a" * 64))

    assert connection.execute("SELECT reservation_id FROM quota_reservation").fetchall() == [("external-reservation",)]
    assert connection.execute("SELECT period, used_credits FROM quota_bucket ORDER BY period").fetchall() == [("day", 1), ("minute", 1)]
    assert connection.execute("SELECT operation_id FROM operations").fetchall() == [("external-operation",)]
    assert connection.execute("SELECT market_date, run_id FROM run_claim").fetchall() == [("2026-10-15", "external-run")]
    assert connection.execute("SELECT market_date, status FROM delivery_recipient").fetchall() == [("2026-10-15", "SENT")]
    assert connection.execute("SELECT audit_id FROM audit_event").fetchall() == [("external-audit",)]
    assert connection.execute("SELECT COUNT(*) FROM test_environment_identity").fetchone()[0] == 0


def _populated_phase3c_cleanup_db():
    """Build the reported Phase 3C residue plus unrelated shared-bucket data."""
    connection = sqlite3.connect(":memory:")
    migration = (ROOT / "cloudflare/marketbot-state-worker/migrations/0001_initial.sql").read_text(encoding="utf-8")
    connection.executescript(migration)
    connection.execute(
        "CREATE TABLE test_environment_identity(singleton INTEGER PRIMARY KEY, database_id TEXT, worker_name TEXT, mode TEXT, attestation_hash TEXT)"
    )
    connection.execute(
        "INSERT INTO test_environment_identity VALUES(1, ?, ?, ?, ?)",
        (runner.TEST_DATABASE_ID, runner.TEST_WORKER_NAME, runner.TEST_MODE, "a" * 64),
    )
    now = "2026-10-15T12:34:00Z"
    synthetic_operation_ids = [f"3c{i:030x}" for i in range(25)]
    for operation_id in synthetic_operation_ids:
        connection.execute(
            "INSERT INTO operations VALUES(?, 'quota.reserve', ?, ?, 'COMMITTED')",
            (operation_id, "b" * 64, now),
        )

    # Nine reservations span two minutes. Add one unrelated reservation
    # sharing the first bucket to prove only synthetic credits are subtracted.
    reservation_ids = synthetic_operation_ids[:9]
    for index, reservation_id in enumerate(reservation_ids):
        day = "2026-10-15"
        minute = "2026-10-15T12:34Z" if index < 7 else "2026-10-15T12:35Z"
        connection.execute(
            "INSERT INTO quota_reservation VALUES(?, 'twelve_data', ?, ?, 1, ?)",
            (reservation_id, day, minute, now),
        )
    connection.execute(
        "INSERT INTO quota_reservation VALUES('unrelated-reservation', 'twelve_data', '2026-10-15', '2026-10-15T12:34Z', 1, ?) ",
        (now,),
    )
    connection.execute("INSERT INTO quota_bucket VALUES('day','2026-10-15',10,800)")
    connection.execute("INSERT INTO quota_bucket VALUES('minute','2026-10-15T12:34Z',8,8)")
    connection.execute("INSERT INTO quota_bucket VALUES('minute','2026-10-15T12:35Z',2,8)")

    for index, market_date in enumerate(("2099-01-01", "2099-01-02", "2099-01-03")):
        run_id = f"3c-run-{index}"
        op_id = synthetic_operation_ids[9 + index]
        connection.execute(
            "INSERT INTO run_claim(market_date,run_id,status,claim_operation_id,last_operation_id,claimed_at) VALUES(?,?,'CLAIMED',?,?,?)",
            (market_date, run_id, op_id, op_id, now),
        )
    recipients = (
        "hmac256:ac00581af76fae2942fefc401985c751c927c4412b3e8486f54cda8e4d420419",
        "hmac256:ad4e4df2c02723b74f014769278cd2c1edac43e7f17751f1b9048f40b11aa692",
    )
    for index, (market_date, recipient, status) in enumerate((
        ("2099-01-01", recipients[0], "SENT"),
        ("2099-01-02", recipients[0], "FAILED_SAFE"),
        ("2099-01-02", recipients[1], "UNKNOWN"),
    )):
        connection.execute(
            "INSERT INTO delivery_recipient(market_date,recipient_hmac,status,attempt,attempt_id,last_operation_id,send_started_at,updated_at,safe_error,reconciliation_code) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (market_date, recipient, status, 1, f"3c-attempt-{index}", synthetic_operation_ids[12 + index], now, now,
             "SIMULATED_RESPONSE_LOSS" if status == "UNKNOWN" else None,
             "OPERATOR_CONFIRMED_NOT_SENT" if status == "FAILED_SAFE" else None),
        )
    connection.execute("INSERT INTO audit_event VALUES('3c-audit-1','delivery.reconcile','synthetic','operator',?)", (now,))
    # Non-synthetic rows must survive, including operational records outside
    # the 3c operation prefix and delivery cleanup scope.
    connection.execute("INSERT INTO operations VALUES('unrelated-operation','run.claim',?,?, 'COMMITTED')", ("c" * 64, now))
    connection.execute(
        "INSERT INTO run_claim(market_date,run_id,status,claim_operation_id,last_operation_id,claimed_at) VALUES('2026-10-15','unrelated-run','CLAIMED','unrelated-operation','unrelated-operation',?)",
        (now,),
    )
    connection.execute(
        "INSERT INTO delivery_recipient(market_date,recipient_hmac,status,attempt,attempt_id,last_operation_id,send_started_at,updated_at) VALUES('2026-10-15',?,'SENT',1,'unrelated-attempt','unrelated-operation',?,?)",
        ("hmac256:" + "f" * 64, now, now),
    )
    connection.execute("INSERT INTO audit_event VALUES('unrelated-audit','EXTERNAL','outside-scope','operator',?)", (now,))
    return connection


def _phase3c_cleanup_counts(connection):
    return {
        "operations": connection.execute("SELECT COUNT(*) FROM operations WHERE operation_id LIKE '3c%' ").fetchone()[0],
        "reservations": connection.execute("SELECT COUNT(*) FROM quota_reservation WHERE reservation_id LIKE '3c%' ").fetchone()[0],
        "runs": connection.execute("SELECT COUNT(*) FROM run_claim WHERE market_date BETWEEN '2090-01-01' AND '2099-12-31' AND run_id LIKE '3c%' ").fetchone()[0],
        "deliveries": connection.execute("SELECT COUNT(*) FROM delivery_recipient WHERE market_date BETWEEN '2090-01-01' AND '2099-12-31' AND recipient_hmac IN (?,?)", (
            "hmac256:ac00581af76fae2942fefc401985c751c927c4412b3e8486f54cda8e4d420419",
            "hmac256:ad4e4df2c02723b74f014769278cd2c1edac43e7f17751f1b9048f40b11aa692",
        )).fetchone()[0],
        "audits": connection.execute("SELECT COUNT(*) FROM audit_event WHERE audit_id LIKE '3c%' ").fetchone()[0],
        "identity": connection.execute("SELECT COUNT(*) FROM test_environment_identity").fetchone()[0],
    }


def test_populated_cleanup_wrong_attestation_is_successful_sql_noop_then_correct_hash_cleans_safely():
    connection = _populated_phase3c_cleanup_db()
    cleanup = (ROOT / "cloudflare/marketbot-state-worker/scripts/cleanup_phase3c_test_data.sql").read_text(encoding="utf-8")
    expected_before = {"operations": 25, "reservations": 9, "runs": 3, "deliveries": 3, "audits": 1, "identity": 1}
    assert _phase3c_cleanup_counts(connection) == expected_before

    # This reproduces the remote symptom: SQL is valid and reports completion,
    # but every guarded mutation is a no-op when the entered secret is wrong.
    connection.executescript(cleanup.replace("__TEST_ADMIN_SECRET_SHA256__", "d" * 64))
    assert _phase3c_cleanup_counts(connection) == expected_before

    # The matching attestation removes only Phase 3C records.
    connection.executescript(cleanup.replace("__TEST_ADMIN_SECRET_SHA256__", "a" * 64))
    assert _phase3c_cleanup_counts(connection) == {"operations": 0, "reservations": 0, "runs": 0, "deliveries": 0, "audits": 0, "identity": 0}
    assert connection.execute("SELECT operation_id FROM operations").fetchall() == [("unrelated-operation",)]
    assert connection.execute("SELECT reservation_id FROM quota_reservation").fetchall() == [("unrelated-reservation",)]
    assert connection.execute("SELECT run_id FROM run_claim").fetchall() == [("unrelated-run",)]
    assert connection.execute("SELECT status FROM delivery_recipient").fetchall() == [("SENT",)]
    assert connection.execute("SELECT audit_id FROM audit_event").fetchall() == [("unrelated-audit",)]
    assert connection.execute("SELECT period,bucket_key,used_credits FROM quota_bucket ORDER BY period,bucket_key").fetchall() == [
        ("day", "2026-10-15", 1), ("minute", "2026-10-15T12:34Z", 1),
    ]
    assert connection.execute("SELECT cloud_quota_exclusive,windows_sender_disabled,cloud_delivery_enabled FROM ownership").fetchone() == (0, 0, 0)


def test_populated_cleanup_is_safe_to_resume_after_partial_deletion():
    connection = _populated_phase3c_cleanup_db()
    cleanup = (ROOT / "cloudflare/marketbot-state-worker/scripts/cleanup_phase3c_test_data.sql").read_text(encoding="utf-8")
    # Simulate an earlier partial cleanup while retaining the identity guard.
    connection.execute("DELETE FROM delivery_recipient WHERE market_date='2099-01-01'")
    connection.execute("DELETE FROM quota_reservation WHERE reservation_id=(SELECT reservation_id FROM quota_reservation WHERE reservation_id LIKE '3c%' LIMIT 1)")
    connection.commit()
    connection.executescript(cleanup.replace("__TEST_ADMIN_SECRET_SHA256__", "a" * 64))
    assert _phase3c_cleanup_counts(connection) == {"operations": 0, "reservations": 0, "runs": 0, "deliveries": 0, "audits": 0, "identity": 0}
    assert connection.execute("SELECT cloud_quota_exclusive,windows_sender_disabled,cloud_delivery_enabled FROM ownership").fetchone() == (0, 0, 0)


def test_cleanup_readback_checks_synthetic_residue_without_requiring_empty_database():
    script = (ROOT / "cloudflare/marketbot-state-worker/scripts/seed_phase3c_test_identity.ps1").read_text(encoding="utf-8")
    assert "synthetic_operation_count" in script
    assert "synthetic_reservation_count" in script
    assert "synthetic_run_count" in script
    assert "synthetic_delivery_count" in script
    assert "synthetic_audit_count" in script
    assert "quota_bucket_underflow_count" in script
    assert "AS quota_bucket_count" not in script
    assert "$row.operation_count -ne 0" not in script
    assert "TEST_CLEANUP_VALIDATION_FAILED failed_fields=" in script
    assert "TEST_CLEANUP_IDENTITY_ATTESTATION_MISMATCH" in script
    assert script.index("TEST_CLEANUP_IDENTITY_PREFLIGHT=PASS") < script.index("TEST_CLEANUP_SQL_START")
    assert "failed_fields={0} observed={1}" in script
    assert "identityQuery" in script and "attestation_hash" in script
    assert "attestation_hash=$hashHex" not in script


@pytest.mark.parametrize(
    ("failure", "category", "expected_detail"),
    [
        ("timeout", "WRANGLER_TIMEOUT", "timeout_seconds=45"),
        ("spawn", "WRANGLER_PROCESS_START", "error_class=FileNotFoundError"),
        ("exit", "WRANGLER_REMOTE_HTTP_OR_AUTH", "http_status=401"),
    ],
)
def test_wrangler_read_failure_has_safe_actionable_diagnostics(monkeypatch, tmp_path, failure, category, expected_detail):
    wrangler = tmp_path / "wrangler.js"
    wrangler.touch()
    monkeypatch.setattr(runner, "WRANGLER_JS", wrangler)
    monkeypatch.setattr(runner.shutil, "which", lambda name: "node.exe" if name == "node" else None)

    def process(*args, **kwargs):
        if failure == "timeout":
            raise runner.subprocess.TimeoutExpired(args[0], kwargs["timeout"], stderr="token=super-secret")
        if failure == "spawn":
            raise FileNotFoundError("node executable missing")
        return runner.subprocess.CompletedProcess(args[0], 1, "", "Wrangler HTTP 401 token=super-secret")

    monkeypatch.setattr(runner.subprocess, "run", process)
    with pytest.raises(runner.SafeTestError) as caught:
        runner._wrangler_query("SELECT 1", operation_name="targeted_resume_baseline")
    message = str(caught.value)
    assert "operation=targeted_resume_baseline" in message
    assert f"category={category}" in message
    assert expected_detail in message
    assert "target=marketbot-test/8049…6f70" in message
    assert "super-secret" not in message


def test_targeted_read_failure_happens_before_worker_request_or_mutation(monkeypatch):
    def failed_read(sql, *, operation_name):
        assert sql == runner.TARGETED_RESUME_SQL
        assert operation_name == "targeted_resume_baseline"
        raise runner.SafeTestError("TEST_D1_READ_FAILED operation=targeted_resume_baseline category=WRANGLER_TIMEOUT")

    def worker_must_not_be_contacted(*args, **kwargs):
        raise AssertionError("Worker call must not occur after baseline SQL failure")

    monkeypatch.setattr(runner, "_wrangler_query", failed_read)
    monkeypatch.setattr(runner, "state_snapshot", worker_must_not_be_contacted)
    with pytest.raises(runner.SafeTestError, match="WRANGLER_TIMEOUT"):
        runner.run_targeted_delivery_continuation("x" * 32, "y" * 32, confirm_reconciled_baseline=True)


def test_readonly_diagnostic_mode_never_prompts_or_mutates(monkeypatch, capsys):
    calls = []

    def read(sql, *, operation_name):
        calls.append((sql, operation_name))
        return {"schema_version": 1}

    def no_credentials():
        raise AssertionError("diagnostic mode must not load secrets")

    monkeypatch.setattr(runner, "_wrangler_query", read)
    monkeypatch.setattr(runner, "get_test_credentials", no_credentials)
    result = runner.main(["--diagnose-targeted-read", "--database-id", runner.TEST_DATABASE_ID])
    output = capsys.readouterr().out
    assert result == 0
    assert calls == [(runner.TARGETED_RESUME_SQL, "targeted_resume_baseline_diagnostic")]
    assert "REMOTE_MUTATIONS=0" in output
    assert "TEST_D1_READONLY_DIAGNOSTIC=PASS" in output
