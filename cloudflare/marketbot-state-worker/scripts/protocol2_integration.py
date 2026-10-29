"""Protocol 2 scenarios using the existing Phase 3C signing and journal helpers.

Default is plan-only. Local execution is called by local_d1_tests.mjs against
its disposable workerd database. Remote execution requires explicit test identity.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile

import remote_d1_phase3c_test as legacy
from persistence.d1_client import D1StateClient, D1StateError

LOCAL_URL = "http://127.0.0.1:8787/v1/state"
API_SECRET = "synthetic-test-secret-".ljust(48, "x")
ADMIN_SECRET = "synthetic-test-admin-secret-".ljust(48, "y")


class LocalTransport:
    """Rewrite only the pinned TEST URL to this harness's loopback listener."""
    def post(self, url, **kwargs):
        if url != legacy.TEST_URL + "/v1/state":
            raise ValueError("LOCAL_TARGET_REJECTED")
        return legacy.requests.post(LOCAL_URL, allow_redirects=False, **kwargs)


def run_suite(client):
    checks = 0
    slot, reviewer = legacy.TEST_RECIPIENTS

    def call(op, payload, **options):
        return client.call(op, payload, operation_id=legacy._id(), **options)

    def expect_error(code, op, payload, **options):
        nonlocal checks
        try:
            call(op, payload, **options)
        except D1StateError as exc:
            print(f"EXPECTED_REJECTION operation={op} expected={code} actual={str(exc)} http_status={exc.http_status}")
            assert str(exc) == code, (op, str(exc), code)
            checks += 1
        else:
            raise AssertionError(f"EXPECTED_ERROR_NOT_OBSERVED operation={op} expected={code}")

    def state(date=None):
        return call("state.get", {} if date is None else {"market_date": date, "recipient_hmac": slot})

    initial = state()
    assert initial["schema_version"] == 1 and initial["delivery_protocol_version"] == 2
    assert initial.get("test_mutation_mode") is True
    assert initial["ownership"] == dict.fromkeys(("cloud_quota_exclusive", "windows_sender_disabled", "cloud_delivery_enabled"), False)
    dates = [f"2099-02-{day:02}" for day in range(1, 4)]
    assert not any(date in initial["runs"] for date in dates), "TEST_DATES_ALREADY_USED_RECONCILE_FIRST"

    def claim(date, running=True):
        run_id = legacy._id()
        assert call("run.claim", {"market_date": date, "run_id": run_id})["claim_authorized"] is True
        if running:
            assert call("run.transition", {"market_date": date, "run_id": run_id, "from": "CLAIMED", "to": "RUNNING"})["applied"] is True
        return run_id

    def begin(date, **extra):
        return {"market_date": date, "recipient_hmac": slot, "attempt_id": legacy._id(), "reviewed_retry": False, **extra}

    def outcome(payload, status):
        return {key: payload[key] for key in ("market_date", "recipient_hmac", "attempt_id")} | {"status": status}

    first_date = dates[0]
    claim(first_date)
    attempt = begin(first_date)
    begin_operation = legacy._id()
    authorized = client.call("delivery.begin", attempt, operation_id=begin_operation)
    legacy._expect_non_authorizing_replay(lambda: client.call("delivery.begin", attempt, operation_id=begin_operation), operation="delivery.begin", operation_id=begin_operation)
    assert authorized["authorized"] is True and authorized["attempt_id"] == attempt["attempt_id"]
    expect_error("DELIVERY_SLOT_NOT_SENDABLE", "delivery.begin", begin(first_date))
    expect_error("ATTEMPT_ID_INVALID", "delivery.outcome", {"market_date": first_date, "recipient_hmac": slot, "status": "SENT"})
    assert call("delivery.outcome", outcome(attempt, "SENT"))["applied"] is True
    assert state(first_date)["delivery_recipient"]["status"] == "SENT"
    expect_error("DELIVERY_SLOT_NOT_SENDABLE", "delivery.begin", begin(first_date))

    date = dates[1]
    run_id = claim(date)
    attempt = begin(date)
    assert call("delivery.begin", attempt)["authorized"] is True
    assert call("delivery.outcome", outcome(attempt, "UNKNOWN"))["applied"] is True
    expect_error("DELIVERY_SLOT_NOT_SENDABLE", "delivery.begin", begin(date))
    retry = begin(date, reviewed_retry=True, sender_stopped=True)
    expect_error("AUTHENTICATION_FAILED", "delivery.begin", retry)
    expect_error("DELIVERY_SLOT_NOT_SENDABLE", "delivery.begin", retry, admin=True)
    assert state(date)["delivery_recipient"]["status"] == "UNKNOWN"
    assert call("delivery.reconcile", {"market_date": date, "recipient_hmac": slot, "reviewer_hmac": reviewer, "confirmed_sent": False}, admin=True)["applied"] is True
    missing_stop = {k: v for k, v in retry.items() if k != "sender_stopped"}
    expect_error("SENDER_STOP_CONFIRMATION_REQUIRED", "delivery.begin", missing_stop, admin=True)
    result = call("delivery.begin", retry, admin=True)
    assert result["authorized"] is True and result["attempt_id"] == retry["attempt_id"]
    expect_error("D1_WORKER_REQUEST_REJECTED", "delivery.outcome", outcome(attempt, "SENT"))
    row = state(date)["delivery_recipient"]
    assert row["status"] == "SENDING" and row["attempt"] == 2 and row["attempt_id"] == retry["attempt_id"]
    recover = {"market_date": date, "run_id": run_id, "sender_stopped": True}
    expect_error("AUTHENTICATION_FAILED", "run.recover", recover)
    expect_error("SENDER_STOP_CONFIRMATION_REQUIRED", "run.recover", {**recover, "sender_stopped": False}, admin=True)
    expect_error("RUN_NOT_RECOVERABLE", "run.recover", {**recover, "run_id": legacy._id()}, admin=True)
    assert state(date)["delivery_recipient"] == row
    assert call("run.recover", recover, admin=True)["status"] == "DELIVERY_UNKNOWN"
    recovered = state(date)
    assert recovered["run"]["status"] == "DELIVERY_UNKNOWN"
    assert recovered["delivery_recipient"]["status"] == "UNKNOWN"
    assert recovered["delivery_recipient"]["attempt"] == 2

    date = dates[2]
    run_id = claim(date, running=False)
    assert call("run.recover", {"market_date": date, "run_id": run_id, "sender_stopped": True}, admin=True)["status"] == "FAILED"
    expect_error("MARKET_DATE_ALREADY_CLAIMED", "run.claim", {"market_date": date, "run_id": legacy._id()})
    expect_error("TEST_ADMIN_OPERATION_FORBIDDEN", "ownership.set", dict.fromkeys(("cloud_quota_exclusive", "windows_sender_disabled", "cloud_delivery_enabled"), True), admin=True)
    final = state()
    assert final["ownership"] == initial["ownership"]
    assert all(value == 0 for value in final["integrity"].values())
    print(f"PROTOCOL2_INTEGRATION=PASS negative_checks={checks} schema=1 protocol=2 gates=0,0,0 provider_calls=0 telegram_calls=0")


def main(argv=None):
    if not __debug__:
        raise ValueError("OPTIMIZED_PYTHON_NOT_ALLOWED_FOR_TEST_ASSERTIONS")
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--local", action="store_true")
    mode.add_argument("--execute-isolated-remote", action="store_true")
    parser.add_argument("--database-id")
    parser.add_argument("--worker-url", default=legacy.TEST_URL)
    parser.add_argument("--journal-dir", type=Path)
    args = parser.parse_args(argv)
    # Validate targets even in plan mode, before credentials or networking.
    if args.worker_url != legacy.TEST_URL or args.database_id not in (None, legacy.TEST_DATABASE_ID):
        raise ValueError("TEST_TARGET_REJECTED")
    legacy.assert_test_target(args.database_id)
    if not args.local and not args.execute_isolated_remote:
        print("PLAN_ONLY: TEST Worker/D1 only; no requests")
        return 0
    if args.local:
        directory = args.journal_dir.resolve() if args.journal_dir else None
        if directory is None or Path(tempfile.gettempdir()).resolve() not in directory.parents:
            raise ValueError("LOCAL_JOURNAL_MUST_BE_TEMPORARY")
        api, admin = API_SECRET, ADMIN_SECRET
        session = LocalTransport()
    else:
        if args.database_id != legacy.TEST_DATABASE_ID:
            raise ValueError("EXPLICIT_TEST_DATABASE_REQUIRED")
        api, admin = legacy.get_test_credentials()
        legacy.assert_remote_test_database_isolated(initially_empty=True, admin_secret=admin)
        directory = legacy.ROOT / "output/qa/protocol2_integration"
        session = legacy.requests.Session()
        # No redirects to any other host, including production.
        original_post = session.post
        session.post = lambda *a, **kw: original_post(*a, allow_redirects=False, **kw)
    directory.mkdir(parents=True, exist_ok=True)
    legacy.OPERATION_JOURNAL_FILE = directory / "operations.jsonl"
    run_suite(legacy.JournaledClient(D1StateClient(legacy.TEST_URL, api, admin_secret=admin, session=session)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # The durable journal retains exact IDs. Never dump request/response data.
        print(f"PROTOCOL2_INTEGRATION=FAIL category={type(exc).__name__}; STOP_AND_RECONCILE_JOURNAL")
        sys.exit(1)
