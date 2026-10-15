from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from persistence.d1_sqlite_prototype import (
    D1SQLitePrototype,
    PrototypeClaimDenied,
    PrototypeQuotaExceeded,
)


def test_concurrent_reservations_never_exceed_minute_or_day_limit(tmp_path):
    now = datetime(2026, 10, 14, 12, 5, tzinfo=timezone.utc)
    store = D1SQLitePrototype(tmp_path / "state.sqlite", daily_limit=8, minute_limit=4, clock=lambda: now)

    def reserve_one(_):
        try:
            store.reserve(1)
            return "accepted"
        except PrototypeQuotaExceeded:
            return "blocked"

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(reserve_one, range(20)))

    assert results.count("accepted") == 4
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT used_credits FROM quota_buckets WHERE period='minute'").fetchone()[0] == 4
        assert db.execute("SELECT COUNT(*) FROM quota_reservations").fetchone()[0] == 4


def test_quota_day_and_minute_changes_are_one_atomic_update(tmp_path):
    now = datetime(2026, 10, 14, 12, 5, tzinfo=timezone.utc)
    store = D1SQLitePrototype(tmp_path / "state.sqlite", daily_limit=5, minute_limit=5, clock=lambda: now)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        store.reserve(2, fail_after_day_update=True)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM quota_buckets").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM quota_reservations").fetchone()[0] == 0
    store.reserve(5)
    with pytest.raises(PrototypeQuotaExceeded):
        store.reserve(1)


def test_run_claim_is_unique_per_market_date_under_concurrency(tmp_path):
    store = D1SQLitePrototype(tmp_path / "state.sqlite")

    def claim(i):
        try:
            store.claim_run("2026-10-14", f"run-{i}")
            return "accepted"
        except PrototypeClaimDenied:
            return "blocked"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(8)))
    assert results.count("accepted") == 1
    assert results.count("blocked") == 7


def test_delivery_duplicate_and_ambiguous_outcome_never_auto_resend(tmp_path):
    store = D1SQLitePrototype(tmp_path / "state.sqlite")
    slot = store.recipient_slot("synthetic-recipient-123", b"x" * 32)
    store.begin_delivery("2026-10-14", slot)
    with pytest.raises(PrototypeClaimDenied):
        store.begin_delivery("2026-10-14", slot)

    assert store.recover_interrupted_deliveries() == 1
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT status FROM deliveries").fetchone()[0] == "UNKNOWN"
    with pytest.raises(PrototypeClaimDenied):
        store.begin_delivery("2026-10-14", slot)
    store.reconcile_unknown("2026-10-14", slot, confirmed_sent=False, reviewer_ref="hmac256:" + "a" * 64)
    store.begin_delivery("2026-10-14", slot, reviewed_retry=True)


def test_delivery_unknown_after_ambiguous_telegram_response_is_non_sendable(tmp_path):
    store = D1SQLitePrototype(tmp_path / "state.sqlite")
    slot = store.recipient_slot("synthetic-recipient-456", b"y" * 32)
    store.begin_delivery("2026-10-14", slot)
    store.record_delivery("2026-10-14", slot, "UNKNOWN")
    with pytest.raises(PrototypeClaimDenied):
        store.begin_delivery("2026-10-14", slot, reviewed_retry=True)


def test_failed_delivery_write_does_not_authorize_duplicate_attempt(tmp_path):
    store = D1SQLitePrototype(tmp_path / "state.sqlite")
    slot = store.recipient_slot("synthetic-recipient-789", b"z" * 32)
    store.begin_delivery("2026-10-14", slot)
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TRIGGER fail_sent BEFORE UPDATE OF status ON deliveries "
                   "WHEN NEW.status='SENT' BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.record_delivery("2026-10-14", slot, "SENT")
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT status FROM deliveries").fetchone()[0] == "SENDING"
    assert store.recover_interrupted_deliveries() == 1


def test_retention_prunes_old_reservations_but_keeps_unknown_delivery(tmp_path):
    now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    clock = [now]
    store = D1SQLitePrototype(tmp_path / "state.sqlite", clock=lambda: clock[0])
    store.reserve()
    slot = store.recipient_slot("synthetic-recipient", b"q" * 32)
    store.begin_delivery("2026-10-14", slot)
    store.record_delivery("2026-10-14", slot, "UNKNOWN")
    clock[0] = now + timedelta(days=401)
    store.reserve()
    pruned = store.prune()
    assert pruned["reservations"] == 1
    assert pruned["deliveries"] == 0
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT status FROM deliveries").fetchone()[0] == "UNKNOWN"
        assert db.execute("SELECT COUNT(*) FROM quota_reservations").fetchone()[0] == 1


def test_resolved_delivery_and_run_claims_are_retained_then_pruned(tmp_path):
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    clock = [now]
    store = D1SQLitePrototype(tmp_path / "state.sqlite", clock=lambda: clock[0])
    store.claim_run("2026-10-14", "synthetic-run")
    slot = store.recipient_slot("synthetic-recipient", b"r" * 32)
    store.begin_delivery("2026-10-14", slot)
    store.record_delivery("2026-10-14", slot, "SENT")
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE run_claims SET status='COMPLETE'")
    clock[0] = now + timedelta(days=401)
    assert store.prune() == {"reservations": 0, "deliveries": 1, "run_claims": 1}
