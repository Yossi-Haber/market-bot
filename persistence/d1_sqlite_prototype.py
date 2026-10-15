"""Isolated SQLite prototype for evaluating a D1-backed state ledger.

This module is not wired into Market Bot production. It uses only SQLite and
synthetic state, with SQL kept within the SQLite/D1 common subset.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


class PrototypeQuotaExceeded(RuntimeError):
    """The prototype quota ledger refuses the requested reservation."""


class PrototypeClaimDenied(RuntimeError):
    """The run or recipient already has a durable non-sendable claim."""


class D1SQLitePrototype:
    """Local transactional model of the proposed D1 tables and operations."""

    RESERVATION_RETENTION_DAYS = 7
    RESOLVED_DELIVERY_RETENTION_DAYS = 400

    def __init__(self, path: Path, *, daily_limit: int = 800, minute_limit: int = 8,
                 clock=lambda: datetime.now(timezone.utc)) -> None:
        self.path = Path(path)
        self.daily_limit = daily_limit
        self.minute_limit = minute_limit
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS quota_buckets (
                    period TEXT NOT NULL CHECK(period IN ('day','minute')),
                    bucket_key TEXT NOT NULL,
                    used_credits INTEGER NOT NULL CHECK(used_credits >= 0),
                    limit_credits INTEGER NOT NULL CHECK(limit_credits > 0),
                    CHECK (used_credits <= limit_credits),
                    PRIMARY KEY(period, bucket_key)
                );
                CREATE TABLE IF NOT EXISTS quota_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    day_key TEXT NOT NULL,
                    minute_key TEXT NOT NULL,
                    credits INTEGER NOT NULL CHECK(credits > 0),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS quota_reservations_created
                    ON quota_reservations(created_at);
                CREATE TABLE IF NOT EXISTS run_claims (
                    market_date TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN
                      ('CLAIMED','RUNNING','COMPLETE','FAILED','DELIVERY_UNKNOWN')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    market_date TEXT NOT NULL,
                    recipient_hmac TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                      ('SENDING','SENT','UNKNOWN','FAILED_SAFE')),
                    attempt INTEGER NOT NULL CHECK(attempt > 0),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(market_date, recipient_hmac)
                );
                CREATE INDEX IF NOT EXISTS deliveries_updated
                    ON deliveries(updated_at);
            """)

    def reserve(self, credits: int = 1, *, fail_after_day_update: bool = False) -> str:
        """Atomically charge day+minute budgets before an external request."""
        if not isinstance(credits, int) or credits <= 0:
            raise ValueError("QUOTA_CREDITS_INVALID")
        now = self._now()
        day_key, minute_key = now.date().isoformat(), now.strftime("%Y-%m-%dT%H:%MZ")
        reservation_id = uuid.uuid4().hex
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            for period, key, limit in (("day", day_key, self.daily_limit),
                                        ("minute", minute_key, self.minute_limit)):
                db.execute("""
                    INSERT INTO quota_buckets(period,bucket_key,used_credits,limit_credits)
                    VALUES(?,?,?,?)
                    ON CONFLICT(period,bucket_key) DO UPDATE SET
                      used_credits=quota_buckets.used_credits+excluded.used_credits
                """, (period, key, credits, limit))
                if period == "day" and fail_after_day_update:
                    raise sqlite3.OperationalError("injected write failure")
            db.execute("INSERT INTO quota_reservations VALUES(?,?,?,?,?)",
                       (reservation_id, day_key, minute_key, credits, now.isoformat()))
            db.commit()
            return reservation_id
        except sqlite3.IntegrityError as exc:
            db.rollback()
            if "used_credits <= limit_credits" in str(exc):
                raise PrototypeQuotaExceeded("QUOTA_EXHAUSTED") from None
            raise
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def claim_run(self, market_date: str, run_id: str) -> None:
        """Create a unique date claim; concurrent duplicate claims fail closed."""
        now = self._now().isoformat()
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO run_claims VALUES(?,?,?,?,?)",
                           (market_date, run_id, "CLAIMED", now, now))
                db.commit()
        except sqlite3.IntegrityError:
            raise PrototypeClaimDenied("MARKET_DATE_ALREADY_CLAIMED") from None

    def recipient_slot(self, chat_id: str, key: bytes) -> str:
        """Return the same opaque HMAC identity shape; never store chat_id."""
        if len(key) < 32:
            raise ValueError("HMAC_KEY_TOO_SHORT")
        digest = hmac.new(key, str(chat_id).encode(), hashlib.sha256).hexdigest()
        return f"hmac256:{digest}"

    def begin_delivery(self, market_date: str, recipient_hmac: str,
                       *, reviewed_retry: bool = False) -> None:
        """Persist SENDING atomically before caller invokes Telegram."""
        now = self._now().isoformat()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT status,attempt FROM deliveries WHERE market_date=? AND recipient_hmac=?",
                (market_date, recipient_hmac),
            ).fetchone()
            if existing is None:
                db.execute("INSERT INTO deliveries VALUES(?,?,?,?,?)",
                           (market_date, recipient_hmac, "SENDING", 1, now))
            elif existing[0] == "FAILED_SAFE" and reviewed_retry:
                db.execute("UPDATE deliveries SET status='SENDING',attempt=?,updated_at=? "
                           "WHERE market_date=? AND recipient_hmac=?",
                           (existing[1] + 1, now, market_date, recipient_hmac))
            else:
                raise PrototypeClaimDenied(f"DELIVERY_NOT_SENDABLE:{existing[0]}")
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def record_delivery(self, market_date: str, recipient_hmac: str, status: str) -> None:
        if status not in {"SENT", "UNKNOWN", "FAILED_SAFE"}:
            raise ValueError("DELIVERY_OUTCOME_INVALID")
        with self._connect() as db:
            cursor = db.execute("UPDATE deliveries SET status=?,updated_at=? "
                                "WHERE market_date=? AND recipient_hmac=? AND status='SENDING'",
                                (status, self._now().isoformat(), market_date, recipient_hmac))
            if cursor.rowcount != 1:
                raise PrototypeClaimDenied("DELIVERY_NOT_IN_SENDING_STATE")

    def recover_interrupted_deliveries(self) -> int:
        """Make all in-flight attempts UNKNOWN; never authorize automatic resend."""
        with self._connect() as db:
            cursor = db.execute("UPDATE deliveries SET status='UNKNOWN',updated_at=? WHERE status='SENDING'",
                                (self._now().isoformat(),))
            return cursor.rowcount

    def reconcile_unknown(self, market_date: str, recipient_hmac: str,
                          *, confirmed_sent: bool, reviewer_ref: str) -> None:
        if not reviewer_ref.startswith("hmac256:"):
            raise ValueError("OPAQUE_REVIEWER_REF_REQUIRED")
        new_status = "SENT" if confirmed_sent else "FAILED_SAFE"
        with self._connect() as db:
            cursor = db.execute("UPDATE deliveries SET status=?,updated_at=? "
                                "WHERE market_date=? AND recipient_hmac=? AND status='UNKNOWN'",
                                (new_status, self._now().isoformat(), market_date, recipient_hmac))
            if cursor.rowcount != 1:
                raise PrototypeClaimDenied("DELIVERY_NOT_UNKNOWN")

    def prune(self) -> dict[str, int]:
        """Bound reservation history; retain unresolved claims/deliveries."""
        now = self._now()
        reservation_cutoff = (now - timedelta(days=self.RESERVATION_RETENTION_DAYS)).isoformat()
        delivery_cutoff = (now - timedelta(days=self.RESOLVED_DELIVERY_RETENTION_DAYS)).isoformat()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            reservations = db.execute("DELETE FROM quota_reservations WHERE created_at < ?",
                                      (reservation_cutoff,)).rowcount
            db.execute("DELETE FROM quota_buckets WHERE (period='day' AND bucket_key < ?) "
                       "OR (period='minute' AND bucket_key < ?)",
                       ((now.date() - timedelta(days=2)).isoformat(),
                        (now - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%MZ")))
            deliveries = db.execute("DELETE FROM deliveries WHERE status IN ('SENT','FAILED_SAFE') "
                                     "AND updated_at < ?", (delivery_cutoff,)).rowcount
            runs = db.execute("DELETE FROM run_claims WHERE status IN ('COMPLETE','FAILED') "
                              "AND updated_at < ?", (delivery_cutoff,)).rowcount
            db.commit()
            return {"reservations": reservations, "deliveries": deliveries, "run_claims": runs}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10, isolation_level=None)

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("TIMEZONE_REQUIRED")
        return value.astimezone(timezone.utc)
