"""Cloud adapter for the shared pipeline; live execution is opt-in and gated."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from app import BASE_DIR, OUTPUT_DIR
from market_calendar import MarketCalendarError, calendar_version, resolve_market_date
from persistence.github_state import GitHubStateStore, StateUnavailable

NY = ZoneInfo("America/New_York")
MANIFEST_DIR = OUTPUT_DIR / "cloud_operations"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Guarded cloud adapter for the existing Market Bot pipeline.")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--calendar-check", action="store_true", help="Offline calendar-only validation; no state/provider access.")
    modes.add_argument("--dry-run", action="store_true", help="Validate state/date and write a sanitized manifest without claims or providers.")
    modes.add_argument("--execute", action="store_true", help="Run the shared production pipeline after ownership and state gates.")
    parser.add_argument("--market-date", help="Explicit completed NYSE date; intended for reviewed reruns.")
    parser.add_argument("--send-telegram", action="store_true", help="Request Telegram only when the separate cloud-send gate is enabled.")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    args = parse_args(argv)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        print("CLOUD_CLOCK_TIMEZONE_REQUIRED")
        return 1
    if args.calendar_check:
        try:
            day = resolve_market_date(now=current, explicit_date=args.market_date)
        except MarketCalendarError as exc:
            print(f"CLOUD_CALENDAR_BLOCKED: {exc}")
            return 1
        print(f"CLOUD_CALENDAR_OK market_date={day} timezone=America/New_York calendar_version={calendar_version()}")
        _write_manifest({"status": "CALENDAR_OK", "market_date": day, "timezone": "America/New_York", "calendar_version": calendar_version(), "provider_calls": 0, "telegram_sends": 0})
        return 0

    load_dotenv(BASE_DIR / ".env")
    try:
        store = _state_store()
        # A cloud dry-run still verifies the currently published state tip;
        # it never writes state or reaches market/news/Telegram providers.
        state = store.read(fetch=True)
        date_key = resolve_market_date(
            now=current,
            explicit_date=args.market_date,
            completed_dates={key for key, value in state["runs"].items() if value.get("status") == "COMPLETE"},
            start_date=str(state["created_at"])[:10],
        )
    except Exception as exc:
        if isinstance(exc, MarketCalendarError) and str(exc) == "LATEST_COMPLETED_SESSION_ALREADY_CLAIMED" and not args.market_date:
            _write_manifest({"status": "NO_NEW_TRADING_SESSION", "reason": str(exc), "provider_calls": 0, "telegram_sends": 0})
            print("CLOUD_NOOP: no unclaimed completed NYSE session")
            return 0
        if not isinstance(exc, (StateUnavailable, MarketCalendarError)) and type(exc).__name__ != "D1StateError":
            raise
        _write_manifest({"status": "BLOCKED", "reason": str(exc), "mode": "DRY_RUN" if args.dry_run else "EXECUTE"})
        print(f"CLOUD_PREFLIGHT_BLOCKED: {exc}")
        return 1

    if args.dry_run:
        prior = state["runs"].get(date_key)
        result = "ALREADY_CLAIMED" if prior else "READY_NO_PROVIDER_CALLS"
        _write_manifest({"status": result, "market_date": date_key, "state_generation": state["generation"], "calendar_version": calendar_version(), "provider_calls": 0, "telegram_sends": 0})
        print(f"CLOUD_DRY_RUN {result} market_date={date_key}; provider_calls=0 telegram_sends=0")
        return 0 if not prior else 1

    if os.getenv("MARKETBOT_CLOUD_LIVE_ENABLED") != "1":
        _write_manifest({"status": "BLOCKED", "market_date": date_key, "reason": "CLOUD_LIVE_EXECUTION_DISABLED"})
        print("CLOUD_EXECUTION_BLOCKED: CLOUD_LIVE_EXECUTION_DISABLED")
        return 1
    if state["ownership"].get("cloud_quota_exclusive") is not True or state["ownership"].get("windows_sender_disabled") is not True:
        _write_manifest({"status": "BLOCKED", "market_date": date_key, "reason": "CLOUD_OWNERSHIP_NOT_EXCLUSIVE"})
        print("CLOUD_EXECUTION_BLOCKED: CLOUD_OWNERSHIP_NOT_EXCLUSIVE")
        return 1
    if args.send_telegram and os.getenv("MARKETBOT_CLOUD_TELEGRAM_ENABLED") != "1":
        _write_manifest({"status": "BLOCKED", "market_date": date_key, "reason": "CLOUD_TELEGRAM_DISABLED"})
        print("CLOUD_EXECUTION_BLOCKED: CLOUD_TELEGRAM_DISABLED")
        return 1
    if args.send_telegram and state["ownership"].get("cloud_delivery_enabled") is not True:
        _write_manifest({"status": "BLOCKED", "market_date": date_key, "reason": "CLOUD_DELIVERY_OWNERSHIP_NOT_EXCLUSIVE"})
        print("CLOUD_EXECUTION_BLOCKED: CLOUD_DELIVERY_OWNERSHIP_NOT_EXCLUSIVE")
        return 1

    prior = state["runs"].get(date_key)
    if prior and prior.get("status") == "RUNNING":
        started_at = _parse_utc(prior.get("started_at"))
        if started_at and current.astimezone(timezone.utc) - started_at >= timedelta(minutes=30):
            try:
                _recover_interrupted_run(store, date_key, started_at)
            except StateUnavailable as exc:
                _write_manifest({"status": "BLOCKED", "market_date": date_key, "reason": str(exc)})
                print(f"CLOUD_RECOVERY_BLOCKED: {exc}")
                return 1

    run_id = uuid.uuid4().hex
    try:
        _claim_run(store, date_key, run_id)
    except Exception as exc:
        if not isinstance(exc, (StateUnavailable, RuntimeError)) and type(exc).__name__ != "D1StateError":
            raise
        _write_manifest({"status": "BLOCKED", "market_date": date_key, "reason": str(exc)})
        print(f"CLOUD_CLAIM_BLOCKED: {exc}")
        return 1

    os.environ["MARKETBOT_PERSISTENT_QUOTA_MODE"] = "1"
    if args.send_telegram:
        os.environ["MARKETBOT_PERSISTENT_DELIVERY_MODE"] = "1"
    try:
        from run_daily import run as run_existing_pipeline

        pipeline_args = ["--market-date", date_key]
        if not args.send_telegram:
            pipeline_args.append("--skip-telegram")
        exit_code = run_existing_pipeline(pipeline_args)
        _finish_run(store, date_key, "COMPLETE" if exit_code == 0 and not args.send_telegram else _delivery_status(exit_code, date_key))
        _write_manifest({"status": "COMPLETE" if exit_code == 0 else "FAILED", "market_date": date_key, "run_id": run_id, "provider_calls": "see quota reservations", "telegram_sends": "per-recipient ledger" if args.send_telegram else 0})
        return int(exit_code)
    except Exception:
        # A failure to publish final status leaves the durable claim non-sendable;
        # an operator must review it rather than relying on lease expiration.
        _write_manifest({"status": "RUN_OUTCOME_UNKNOWN", "market_date": date_key, "run_id": run_id, "reason": "PIPELINE_OR_FINAL_STATE_FAILURE"})
        print("CLOUD_RUN_OUTCOME_UNKNOWN: operator review required")
        return 1


def _state_store() -> Any:
    backend = os.getenv("MARKETBOT_STATE_BACKEND", "github").strip().lower()
    if backend == "d1":
        from persistence.d1_client import D1StateClient
        return D1StateClient.from_env()
    if backend != "github":
        raise StateUnavailable("CLOUD_STATE_BACKEND_INVALID")
    repo = os.getenv("MARKETBOT_STATE_REPO", "").strip()
    if not repo:
        raise StateUnavailable("CLOUD_STATE_REPOSITORY_REQUIRED")
    return GitHubStateStore(Path(repo))


def _claim_run(store: GitHubStateStore, market_date: str, run_id: str) -> None:
    if type(store).__name__ == "D1StateClient":
        store.claim_run(market_date, run_id)
        store.transition_run(market_date, run_id, "CLAIMED", "RUNNING")
        return
    now = datetime.now(timezone.utc).isoformat()

    def mutate(state: dict[str, Any]) -> None:
        if state["ownership"].get("cloud_quota_exclusive") is not True or state["ownership"].get("windows_sender_disabled") is not True:
            raise RuntimeError("CLOUD_OWNERSHIP_NOT_EXCLUSIVE")
        if market_date in state["runs"]:
            raise RuntimeError("MARKET_DATE_ALREADY_CLAIMED_REQUIRES_REVIEW")
        state["runs"][market_date] = {"status": "CLAIMED", "run_id": run_id, "claimed_at": now}

    store.update(mutate, fetch=True)

    def running(state: dict[str, Any]) -> None:
        item = state["runs"].get(market_date)
        if not item or item.get("run_id") != run_id or item.get("status") != "CLAIMED":
            raise RuntimeError("MARKET_DATE_CLAIM_CHANGED")
        item["status"] = "RUNNING"
        item["started_at"] = datetime.now(timezone.utc).isoformat()

    store.update(running, fetch=True)


def _finish_run(store: GitHubStateStore, market_date: str, status: str) -> None:
    if type(store).__name__ == "D1StateClient":
        run_id = _run_id_for_date(store, market_date)
        store.transition_run(market_date, run_id, "RUNNING", status)
        return
    def mutate(state: dict[str, Any]) -> None:
        item = state["runs"].get(market_date)
        if not item or item.get("status") != "RUNNING":
            raise RuntimeError("MARKET_DATE_RUNNING_CLAIM_MISSING")
        item.update({"status": status, "finished_at": datetime.now(timezone.utc).isoformat()})
    store.update(mutate, fetch=True)


def _run_id_for_date(store: Any, market_date: str) -> str:
    state = store.read()
    run = state.get("runs", {}).get(market_date)
    run_id = run.get("run_id") if isinstance(run, dict) else None
    if not isinstance(run_id, str):
        raise StateUnavailable("MARKET_DATE_RUN_ID_MISSING")
    return run_id


def _recover_interrupted_run(store: GitHubStateStore, market_date: str, started_at: datetime) -> None:
    """Mark an old in-flight sender UNKNOWN; never clear or retry its claim."""
    if type(store).__name__ == "D1StateClient":
        state = store.read()
        run = state.get("runs", {}).get(market_date, {})
        run_id = run.get("run_id")
        if not run_id or run.get("status") != "RUNNING" or _parse_utc(run.get("started_at")) != started_at:
            raise RuntimeError("INTERRUPTED_RUN_STATE_CHANGED")
        store.recover_run(market_date, run_id)
        return
    def mutate(state: dict[str, Any]) -> None:
        run_record = state["runs"].get(market_date)
        if not run_record or run_record.get("status") != "RUNNING" or _parse_utc(run_record.get("started_at")) != started_at:
            raise RuntimeError("INTERRUPTED_RUN_STATE_CHANGED")
        found_sending = False
        for recipient in state["deliveries"].get(market_date, {}).get("recipients", {}).values():
            if recipient.get("status") == "SENDING":
                recipient.update({"status": "UNKNOWN", "updated_at": datetime.now(timezone.utc).isoformat(), "safe_error": "RECOVERED_IN_FLIGHT_AFTER_RESTART"})
                found_sending = True
        run_record.update({
            "status": "DELIVERY_UNKNOWN" if found_sending else "FAILED",
            "reason_code": "INTERRUPTED_SEND_REQUIRES_REVIEW" if found_sending else "INTERRUPTED_RUN_REQUIRES_REVIEW",
        })
    store.update(mutate, fetch=True)


def _parse_utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _delivery_status(exit_code: int, market_date: str) -> str:
    try:
        value = json.loads((OUTPUT_DIR / "daily_report_delivery.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "FAILED" if exit_code else "REPORT_READY"
    if value.get("report_date") != market_date:
        return "FAILED"
    return {"SENT": "COMPLETE", "PARTIAL": "PARTIAL_DELIVERY", "UNKNOWN": "DELIVERY_UNKNOWN"}.get(value.get("status"), "FAILED")


def _write_manifest(payload: dict[str, Any]) -> Path:
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    path = MANIFEST_DIR / "latest.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


if __name__ == "__main__":
    sys.exit(run())
