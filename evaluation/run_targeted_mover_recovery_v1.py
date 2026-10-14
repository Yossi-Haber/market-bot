"""Quota-safe, resumable rebuild of Aug 19 and Sep 22 mover artifacts only.

Live scope is fixed to two dates. All state and recovered data stay beneath
``output/evaluation_extended_v2/30_day_milestone/targeted_recovery_v1``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import shutil
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import movers
from collectors.market import TwelveDataClient
from collectors.rate_limit import CreditRateLimitController, QuotaPauseRequired
from evaluation.quota_runner import TwelveDataRequestLedger, atomic_json
from utils.windows_power import prevent_system_sleep

_BASE_MOVER_OUTPUT = movers._mover_output

MILESTONE = ROOT / "output/evaluation_extended_v2/30_day_milestone"
RECOVERY = MILESTONE / "targeted_recovery_v1"
SOURCE_ROOT = RECOVERY / "source_rebuild"
TARGET_DATES = ("2026-08-19", "2026-09-22")
SELECTED_DATES_PATH = MILESTONE / "selected_dates.csv"
ROOT_LEDGER = RECOVERY / "recovery_request_ledger.json"
ROOT_SUMMARY_JSON = RECOVERY / "recovery_summary.json"
ROOT_SUMMARY_MD = RECOVERY / "recovery_summary.md"
REQUEST_LEDGER_PATH = SOURCE_ROOT / "twelve_data_request_ledger.json"
LIMITER_STATE_PATH = SOURCE_ROOT / "twelve_data_limiter_state.json"
BUDGET_BASELINE_PATH = SOURCE_ROOT / "budget_baseline.json"
TOP_GAINERS = 5
TOP_LOSERS = 5


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def _atomic_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _dates() -> list[str]:
    with SELECTED_DATES_PATH.open(encoding="utf-8-sig", newline="") as handle:
        return [row["date"] for row in csv.DictReader(handle)]


def _checkpoint(date_value: str) -> tuple[Path, dict[str, Any]]:
    path = SOURCE_ROOT / date_value / ".resume" / "mover_checkpoint.json"
    payload = _read_json(path)
    if not isinstance(payload, dict) or payload.get("market_date") != date_value:
        return path, {"market_date": date_value, "valid_results": [], "errors": [], "batch_states": []}
    return path, payload


def _valid_results(date_value: str) -> dict[str, dict[str, Any]]:
    _, checkpoint = _checkpoint(date_value)
    return {
        str(row.get("symbol", "")).upper(): row
        for row in checkpoint.get("valid_results", [])
        if isinstance(row, dict) and row.get("verified") is True
    }


def _missing_symbols(date_value: str) -> list[str]:
    all_symbols = [item.symbol for item in movers.load_universe()]
    valid = set(_valid_results(date_value))
    return [symbol for symbol in all_symbols if symbol not in valid]


def _record_with_source_data(result: dict[str, Any], company_name: str) -> dict[str, Any]:
    """Keep the production mover schema and add durable point-in-time evidence."""
    # ``movers._mover_output`` is temporarily replaced with this enrichment
    # wrapper during recovery, so retain and call the original formatter.
    public = _BASE_MOVER_OUTPUT(result, company_name)
    bars = result.get("completed_bars")
    if not isinstance(bars, list) or len(bars) < 2:
        raise ValueError(f"MOVER_SOURCE_BARS_MISSING:{result.get('symbol', 'UNKNOWN')}")
    target_bar, previous_bar = bars[0], bars[1]
    target_date = str(result.get("latest_bar_datetime", ""))[:10]
    previous_date = str(previous_bar.get("datetime", ""))[:10]
    if target_date != target_bar.get("datetime", "")[:10] or not previous_date or previous_date >= target_date:
        raise ValueError(f"MOVER_SOURCE_BAR_DATE_INVALID:{result.get('symbol', 'UNKNOWN')}")
    return {
        **public,
        "target_date": target_date,
        "target_close": result["close"],
        "previous_valid_session_date": previous_date,
        "previous_close": result["previous_close"],
        "computed_move": result["change"],
        "computed_move_pct": result["change_pct"],
        "provider_status": "SUCCESS",
        "validation_status": "PASS",
        "completed_bars": bars,
        "retrieval_metadata": {
            "provider": "Twelve Data",
            "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
            "interval": "1day",
            "requested_market_date": target_date,
            "selection": "latest completed bar on target date plus immediately previous completed bar",
        },
    }


def _scope_preflight() -> dict[str, Any]:
    import build_historical_market_cache as historical

    if len(movers.load_universe()) != 120:
        raise RuntimeError("MOVER_UNIVERSE_SIZE_NOT_120")
    market_checks: dict[str, bool] = {}
    for day in TARGET_DATES:
        path = MILESTONE / "history" / day / "market_snapshot.json"
        market = _read_json(path)
        market_checks[day] = bool(market and historical._validate_historical_market(market, day))
    if not all(market_checks.values()):
        raise RuntimeError(f"TARGET_DATE_MARKET_SNAPSHOT_INVALID:{market_checks}")
    return {"target_dates": list(TARGET_DATES), "symbols_per_date": 120, "market_snapshots_valid": market_checks}


def _archive_preflight() -> None:
    archive = RECOVERY / "preflight_before_live"
    if (archive / "recovery_summary.json").exists():
        return
    archive.mkdir(parents=True, exist_ok=True)
    for name in ("missing_symbols_2026-08-19.csv", "missing_symbols_2026-09-22.csv", "recovery_request_ledger.json", "recovery_summary.json", "recovery_summary.md"):
        source = RECOVERY / name
        if source.is_file():
            shutil.copy2(source, archive / name)
    prior_replay = RECOVERY / "replay"
    if prior_replay.is_dir():
        shutil.copytree(prior_replay, archive / "replay", dirs_exist_ok=True)


def _main_limiter_usage() -> int:
    """Read today's existing project limiter count without changing it."""
    path = MILESTONE / "resume_after_dxy" / "twelve_data_limiter_state.json"
    state = _read_json(path)
    if not isinstance(state, dict) or state.get("quota_day_utc") != datetime.now(timezone.utc).date().isoformat():
        return 0
    return max(0, int(state.get("daily_requests", 0) or 0))


def _configure_limiter(ledger: TwelveDataRequestLedger) -> tuple[CreditRateLimitController, int]:
    state = _read_json(LIMITER_STATE_PATH)
    baseline = _read_json(BUDGET_BASELINE_PATH)
    main_used = _main_limiter_usage()
    today = datetime.now(timezone.utc).date().isoformat()
    if not isinstance(baseline, dict):
        recovery_used = int(state.get("daily_requests", 0) or 0) if isinstance(state, dict) and state.get("quota_day_utc") == today else 0
        recovery_credits = ledger.totals()
        recovery_delta = recovery_credits.get("2026-08-19", 0) + recovery_credits.get("2026-09-22", 0)
        baseline_used = max(0, recovery_used - recovery_delta) if recovery_used else main_used
        baseline = {"quota_day_utc": today, "main_daily_requests_at_start": baseline_used}
        atomic_json(BUDGET_BASELINE_PATH, baseline)
    baseline_used = int(baseline.get("main_daily_requests_at_start", 0) or 0) if baseline.get("quota_day_utc") == today else main_used
    recovery_used = int(state.get("daily_requests", 0) or 0) if isinstance(state, dict) and state.get("quota_day_utc") == today else baseline_used
    recovery_delta = max(0, recovery_used - baseline_used)
    effective_used = main_used + recovery_delta
    limiter = CreditRateLimitController(
        state_path=LIMITER_STATE_PATH,
        request_recorder=ledger.record,
        initial_daily_requests=effective_used,
        max_blocking_wait_seconds=30.0,
    )
    limiter.daily_requests = effective_used
    limiter._persist_state()
    return limiter, main_used


def _symbol_attempts(ledger: TwelveDataRequestLedger, day: str) -> dict[str, int]:
    attempts: dict[str, int] = {}
    for row in ledger.rows:
        if row.get("historical_date") != day:
            continue
        for symbol in str(row.get("symbol", "")).split(","):
            symbol = symbol.strip().upper()
            if symbol:
                attempts[symbol] = attempts.get(symbol, 0) + 1
    return attempts


def _write_date_progress(day: str, status: str, ledger: TwelveDataRequestLedger, reason: str = "") -> dict[str, Any]:
    folder = SOURCE_ROOT / day
    checkpoint_path, checkpoint = _checkpoint(day)
    valid = _valid_results(day)
    errors = checkpoint.get("errors", [])
    attempts = _symbol_attempts(ledger, day)
    error_by_symbol = {str(row.get("symbol", "")).upper(): row for row in errors if isinstance(row, dict)}
    universe = {item.symbol: item.display_name for item in movers.load_universe()}
    rows = []
    for symbol, company_name in universe.items():
        item = valid.get(symbol)
        error = error_by_symbol.get(symbol, {})
        rows.append({
            "date": day,
            "symbol": symbol,
            "company_name": company_name,
            "target_date": item.get("target_date", day) if item else day,
            "target_close": item.get("target_close", item.get("close", "")) if item else "",
            "previous_valid_session_date": item.get("previous_valid_session_date", "") if item else "",
            "previous_close": item.get("previous_close", "") if item else "",
            "computed_move": item.get("computed_move", item.get("change", "")) if item else "",
            "computed_move_pct": item.get("computed_move_pct", item.get("change_pct", "")) if item else "",
            "provider_status": "SUCCESS" if item else (error.get("error", "PENDING")),
            "validation_status": "PASS" if item else "PENDING",
            "retry_count": max(0, attempts.get(symbol, 0) - 1),
            "retrieved_at_utc": (item.get("retrieval_metadata") or {}).get("retrieved_at_utc", "") if item else "",
            "reason": "" if item else error.get("error", "NOT_REQUESTED_YET"),
        })
    fields = ["date", "symbol", "company_name", "target_date", "target_close", "previous_valid_session_date", "previous_close", "computed_move", "computed_move_pct", "provider_status", "validation_status", "retry_count", "retrieved_at_utc", "reason"]
    _atomic_csv(RECOVERY / f"full_refetch_{day}.csv", rows, fields)
    valid_count = len(valid)
    failure_count = sum(symbol not in valid and symbol in error_by_symbol for symbol in universe)
    pending_count = 120 - valid_count - failure_count
    progress = {
        "market_date": day,
        "status": status,
        "reason": reason,
        "symbols_requested": 120,
        "symbols_succeeded": valid_count,
        "symbols_failed": failure_count,
        "symbols_pending": pending_count,
        "checkpoint": str(checkpoint_path),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    atomic_json(folder / "progress.json", progress)
    if checkpoint_path.is_file():
        atomic_json(folder / "per_symbol_records.json", valid)
    return progress


def _ledger_summary(ledger: TwelveDataRequestLedger, progress: dict[str, dict[str, Any]], *, status: str, quota_pause: dict[str, Any] | None = None, scope: dict[str, Any] | None = None) -> dict[str, Any]:
    totals = ledger.totals()
    rows = []
    for day in TARGET_DATES:
        attempts = _symbol_attempts(ledger, day)
        rows.append({
            "date": day,
            "symbols_requested": 120,
            "symbols_attempted_unique": len(attempts),
            "symbols_succeeded": progress.get(day, {}).get("symbols_succeeded", len(_valid_results(day))),
            "symbols_failed": progress.get(day, {}).get("symbols_failed", 0),
            "symbols_pending": progress.get(day, {}).get("symbols_pending", 120 - len(_valid_results(day))),
            "http_attempts": sum(1 for row in ledger.rows if row.get("historical_date") == day),
            "twelve_data_credits_used": int(totals.get(day, 0)),
            "status": progress.get(day, {}).get("status", "NOT_STARTED"),
        })
    return {
        "schema_version": 1,
        "status": status,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": scope or {"target_dates": list(TARGET_DATES), "symbols_per_date": 120},
        "dates": rows,
        "total_incremental_twelve_data_credits": sum(int(totals.get(day, 0)) for day in TARGET_DATES),
        "finnhub_requests": 0,
        "official_verification_requests": 0,
        "tier2_requests": 0,
        "fmp_requests": 0,
        "sep14_live_requests": 0,
        "quota_pause": quota_pause,
        "source_ready_count": None,
        "replay_performed": False,
        "final_replay": None,
    }


def _write_summary(ledger: TwelveDataRequestLedger, progress: dict[str, dict[str, Any]], *, status: str, quota_pause: dict[str, Any] | None = None, scope: dict[str, Any] | None = None) -> dict[str, Any]:
    summary = _ledger_summary(ledger, progress, status=status, quota_pause=quota_pause, scope=scope)
    atomic_json(ROOT_LEDGER, {
        "schema_version": 1,
        "updated_at_utc": summary["updated_at_utc"],
        "request_ledger_path": str(REQUEST_LEDGER_PATH),
        "quota_state_path": str(LIMITER_STATE_PATH),
        "request_count": sum(row["http_attempts"] for row in summary["dates"]),
        "requests": ledger.rows,
        "dates": summary["dates"],
        "total_incremental_twelve_data_credits": summary["total_incremental_twelve_data_credits"],
        "non_market_provider_requests": {"finnhub": 0, "official": 0, "tier2": 0, "fmp": 0, "sep14": 0},
    })
    atomic_json(ROOT_SUMMARY_JSON, summary)
    markdown = [
        "# Targeted 30-Day Replay Completion V1 — Recovery Status",
        "",
        f"Status: **{status}**. Only Aug 19 and Sep 22 are authorized for live Twelve Data collection.",
        "",
        "| Date | Symbols succeeded / 120 | Failed | Pending | HTTP attempts | Twelve Data credits | Status |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in summary["dates"]:
        markdown.append(f"| {row['date']} | {row['symbols_succeeded']} / 120 | {row['symbols_failed']} | {row['symbols_pending']} | {row['http_attempts']} | {row['twelve_data_credits_used']} | {row['status']} |")
    markdown += [
        "",
        f"Incremental Twelve Data credits: **{summary['total_incremental_twelve_data_credits']}**. Sep 14 live requests: **0**.",
        "Finnhub=0; official verification=0; Tier2=0; FMP=0.",
        "",
        "The original history files, Oct 5 baseline, caches, and human adjudications are not written by this recovery runner.",
        "",
    ]
    _atomic_text(ROOT_SUMMARY_MD, "\n".join(markdown))
    return summary


def _source_snapshot(day: str) -> Path:
    if day in TARGET_DATES:
        return SOURCE_ROOT / day / "movers_snapshot.json"
    return MILESTONE / "history" / day / "movers_snapshot.json"


def _mover_source_is_valid(day: str, path: Path, *, recovered: bool) -> bool:
    """Validate a replay input before allowing it to outrank another source."""
    snapshot = _read_json(path)
    if not isinstance(snapshot, dict) or snapshot.get("market_date") != day:
        return False
    proof = snapshot.get("historical_integrity")
    if not isinstance(proof, dict) or proof.get("requested_market_date") != day:
        return False
    if proof.get("integrity_status") != "PASS" or proof.get("future_data_leak_detected") is not False:
        return False
    if (proof.get("max_source_date") or "") > day or snapshot.get("collection_status") != "SUCCESS":
        return False
    requested, valid, failed = (snapshot.get(key) for key in ("symbols_requested", "symbols_valid", "symbols_failed"))
    if not isinstance(requested, int) or requested <= 0 or valid != requested or failed != 0:
        return False
    if recovered and (requested != 120 or valid != 120):
        return False
    if not recovered and requested not in (40, 120):
        return False
    if len(snapshot.get("top_gainers") or []) != TOP_GAINERS or len(snapshot.get("top_losers") or []) != TOP_LOSERS:
        return False
    try:
        cached = movers.load_valid_movers_cache(
            day,
            path=path,
            prefilter_enabled=False if recovered else None,
            require_historical_integrity=True,
        )
    except TypeError as exc:
        if "require_historical_integrity" not in str(exc):
            raise
        cached = movers.load_valid_movers_cache(day, path=path, prefilter_enabled=False if recovered else None)
    return cached is not None


def _resolve_replay_mover_source(day: str) -> tuple[Path, str]:
    """Resolve one validated mover source for the isolated historical replay.

    Authorized recovered snapshots outrank legacy history only when their
    date, full-universe integrity, ranked output, and cache contract validate.
    The resolver is confined to the historical recovery/evaluation path.
    """
    history = MILESTONE / "history" / day / "movers_snapshot.json"
    if day in TARGET_DATES:
        recovered = SOURCE_ROOT / day / "movers_snapshot.json"
        if _mover_source_is_valid(day, recovered, recovered=True):
            return recovered, "RECOVERED"
    if _mover_source_is_valid(day, history, recovered=False):
        return history, "VALIDATED_EXISTING" if day == "2026-09-14" else "HISTORY"
    raise RuntimeError(f"NO_VALID_REPLAY_MOVER_SOURCE:{day}")


def _validate_recovered_date(day: str) -> tuple[bool, str]:
    from build_historical_market_cache import _validate_historical_market

    market = _read_json(MILESTONE / "history" / day / "market_snapshot.json")
    mover_path = _source_snapshot(day)
    snapshot = _read_json(mover_path)
    if not market or not _validate_historical_market(market, day):
        return False, "INVALID_HISTORICAL_MARKET_ARTIFACT"
    if not isinstance(snapshot, dict) or snapshot.get("market_date") != day:
        return False, "MOVER_DATE_MISMATCH_OR_MISSING"
    proof = snapshot.get("historical_integrity") or {}
    if proof.get("requested_market_date") != day or proof.get("integrity_status") != "PASS" or proof.get("future_data_leak_detected") is True or (proof.get("max_source_date") or "") > day:
        return False, "HISTORICAL_MOVER_INTEGRITY_FAILED"
    if snapshot.get("collection_status") != "SUCCESS":
        return False, "MOVER_COLLECTION_NOT_SUCCESS"
    requested = snapshot.get("symbols_requested")
    valid_symbols = snapshot.get("symbols_valid")
    failed_symbols = snapshot.get("symbols_failed")
    if day in TARGET_DATES:
        # The two authorized recovery dates deliberately require the full
        # 120-symbol universe; a partial response must never become ground truth.
        if requested != 120 or valid_symbols != 120 or failed_symbols != 0:
            return False, "MOVER_COVERAGE_NOT_120_OF_120"
    else:
        # Existing milestone dates may have been built in the approved
        # 40-symbol candidate mode. Keep that intact, while rejecting any
        # partial collection; the canonical cache loader checks the candidate
        # pool metadata and universe fingerprint below.
        if not isinstance(requested, int) or valid_symbols != requested or failed_symbols != 0:
            return False, "MOVER_COLLECTION_INCOMPLETE"
        if requested not in (40, 120):
            return False, "MOVER_COVERAGE_UNEXPECTED_SIZE"
    if len(snapshot.get("top_gainers") or []) != TOP_GAINERS or len(snapshot.get("top_losers") or []) != TOP_LOSERS:
        return False, "RANKED_MOVERS_MISSING"
    valid = movers.load_valid_movers_cache(day, path=mover_path, prefilter_enabled=False if day in TARGET_DATES else None, require_historical_integrity=True)
    if day not in TARGET_DATES and valid is None:
        valid = movers.load_valid_movers_cache(day, path=mover_path, require_historical_integrity=True)
    if valid is None:
        return False, "MOVER_CACHE_CONTRACT_FAILED"
    return True, ""


def _preflight_all_dates() -> list[dict[str, Any]]:
    import build_historical_market_cache as historical

    rows = []
    for day in _dates():
        valid, reason = _validate_recovered_date(day)
        driver = _read_json(MILESTONE / "history" / day / "market_driver_snapshot.json")
        market_news = historical._market_news_state(day)
        rows.append({
            "date": day,
            "market_snapshot": "VALID" if historical._validate_historical_market(_read_json(MILESTONE / "history" / day / "market_snapshot.json"), day) else "INVALID",
            "movers_snapshot": "VALID" if valid else "INVALID",
            "market_driver_snapshot": "AVAILABLE" if driver and driver.get("market_date") == day else "MISSING",
            "market_news_cache": market_news,
            "source_status": "SOURCE_READY" if valid and driver and driver.get("market_date") == day else "SOURCE_BLOCKED",
            "reason": reason,
        })
    return rows


def _archive_preflight_replay() -> None:
    replay = RECOVERY / "replay"
    if not replay.is_dir() or not (replay / "replay_summary.json").is_file():
        return
    archive = RECOVERY / "replay_attempts" / datetime.now(timezone.utc).strftime("before_rerun_%Y%m%d_%H%M%S_%f")
    archive.mkdir(parents=True, exist_ok=False)
    for item in replay.iterdir():
        target = archive / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        elif item.is_file():
            shutil.copy2(item, target)


def _copy_date(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _redirect_loaded_cache_roots(cache_root: Path) -> None:
    """Redirect legacy module-level cache constants into the replay copy."""
    import importlib

    module_roots = {
        "evaluate_market_drivers": "",
        "news.alpha_vantage": "alpha_vantage",
        "news.catalyst": "news",
        "news.discovery": "news_discovery",
        "news.marketaux": "marketaux",
        "news.official": "official_news",
        "news.providers.finnhub": "finnhub",
        "news.targeted_search": "targeted_search",
        "news.verify": "verification",
        "news.verification_acquisition": "verification_v2",
    }
    for module_name, relative_root in module_roots.items():
        module = importlib.import_module(module_name)
        if hasattr(module, "CACHE_DIR"):
            module.CACHE_DIR = cache_root / relative_root if relative_root else cache_root
        if module_name == "news.verification_acquisition":
            module.OFFICIAL_DISCOVERY_CACHE_DIR = cache_root / "verification_official_domains"


def _run_offline_replay(source_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Run a new 30-day replay against copied source/cache inputs only."""
    replay_root = RECOVERY / "replay"
    eval_dir = replay_root / "evaluation"
    cache_source = ROOT / "cache"
    cache_copy = replay_root / "cache"
    _archive_preflight_replay()
    if cache_copy.exists():
        shutil.rmtree(cache_copy)
    shutil.copytree(cache_source, cache_copy)
    for day in _dates():
        source_history = MILESTONE / "history" / day
        target_history = eval_dir / "history" / day
        for name in ("market_snapshot.json", "market_driver_snapshot.json"):
            _copy_date(source_history / name, target_history / name)
        replay_mover_source, _source_kind = _resolve_replay_mover_source(day)
        _copy_date(replay_mover_source, target_history / "movers_snapshot.json")

    prior_cache_override = os.environ.get("MARKET_BOT_CACHE_DIR")
    os.environ["MARKET_BOT_CACHE_DIR"] = str(cache_copy)
    try:
        import evaluate_market_drivers as replay_engine
        import evaluate_market_drivers_product as product
        import build_historical_evaluation_batch as batch

        # Several older cache readers bind their path at import time. Ensure
        # they all see the copied, isolated cache tree rather than project cache.
        _redirect_loaded_cache_roots(cache_copy)

        dates = _dates()
        with replay_engine.offline_network_guard():
            result = product.evaluate_product(dates[0], dates[-1], market_dates=dates, output_dir=eval_dir)
        days = result.get("days", [])
        skipped = result.get("skipped_dates", [])
        for day in days:
            day["integrity"] = batch.evaluate_integrity(day, output_dir=eval_dir)
            if day["integrity"].get("integrity_status") == "FAIL" and day.get("status") == "READY":
                day["status"] = "PARTIAL"
        # The product evaluator owns serialization/output generation; the
        # replay engine provides the offline guard and JSON-safe conversion.
        product._write_outputs(result, replay_root)
        readiness_rows = [{
            "date": day,
            "readiness": next((item.get("status") for item in days if item.get("market_date") == day), "SKIPPED"),
            "missing_components": ",".join(next((item.get("missing_components", []) for item in days if item.get("market_date") == day), [])),
        } for day in dates]
        integrity_rows = []
        parity = leaks = future_violations = 0
        for day in days:
            audit = day.get("stock_consistency_audit", {})
            mismatch = int(audit.get("mismatch_count", 0) or 0)
            leak = int(audit.get("leak_count", 0) or 0)
            future = 0
            for record in day.get("stock_records", []):
                selected = record.get("selected_event")
                selected_date = str(record.get("selected_published_at") or "")[:10]
                if selected and (record.get("event_time_relation") == "FUTURE_EVENT" or (selected_date and selected_date > day["market_date"])):
                    future += 1
            parity += mismatch
            leaks += leak
            future_violations += future
            integrity = day.get("integrity", {})
            integrity_rows.append({
                "date": day["market_date"],
                "readiness": day.get("status"),
                "integrity": integrity.get("integrity_status", "FAIL"),
                "reasons": ";".join(integrity.get("integrity_reasons", [])),
                "stock_parity_mismatches": mismatch,
                "cross_symbol_candidate_leaks": leak,
                "future_event_violations": future,
                "offline_network_calls": int(day.get("metrics", {}).get("offline_network_attempts_blocked", 0) or 0),
            })
        for skipped_day in skipped:
            integrity_rows.append({
                "date": skipped_day.get("market_date"), "readiness": "FAILED", "integrity": "FAIL",
                "reasons": ";".join(skipped_day.get("missing_components", [])),
                "stock_parity_mismatches": 0, "cross_symbol_candidate_leaks": 0,
                "future_event_violations": 0, "offline_network_calls": 0,
            })
        _atomic_csv(replay_root / "readiness_report.csv", readiness_rows, ["date", "readiness", "missing_components"])
        _atomic_csv(replay_root / "integrity_report.csv", integrity_rows, ["date", "readiness", "integrity", "reasons", "stock_parity_mismatches", "cross_symbol_candidate_leaks", "future_event_violations", "offline_network_calls"])
        counts = {
            "sessions": len(dates),
            "days_replayed": len(days),
            "days_skipped": len(skipped),
            "ready": sum(day.get("status") == "READY" for day in days),
            "partial": sum(day.get("status") == "PARTIAL" for day in days),
            "failed": len(skipped),
            "integrity_pass": sum(day.get("integrity", {}).get("integrity_status") == "PASS" for day in days),
            "integrity_fail": sum(day.get("integrity", {}).get("integrity_status") == "FAIL" for day in days) + len(skipped),
            "offline_network_calls": sum(int(day.get("metrics", {}).get("offline_network_attempts_blocked", 0) or 0) for day in days),
            "fmp_requests": sum(int(day.get("metrics", {}).get("fmp_requests", 0) or 0) for day in days),
            "stock_parity_mismatches": parity,
            "cross_symbol_candidate_leaks": leaks,
            "future_event_violations": future_violations,
        }
        formal = counts["sessions"] == 30 and counts["days_replayed"] == 30 and counts["days_skipped"] == 0 and counts["ready"] == 30 and counts["partial"] == 0 and counts["failed"] == 0 and counts["integrity_pass"] == 30 and counts["integrity_fail"] == 0 and counts["offline_network_calls"] == 0 and counts["fmp_requests"] == 0 and parity == 0 and leaks == 0 and future_violations == 0
        result_payload = replay_engine.to_json_safe(result)
        replay_id = datetime.now(timezone.utc).strftime("targeted_recovery_%Y%m%d_%H%M%S")
        summary = {"status": "COMPLETE" if formal else "REPLAY_VALIDATION_FAILED", "replay_id": replay_id, "counts": counts, "formal_completion": formal, "selected_dates": dates, "provider_calls_during_replay": {"offline_network_calls": counts["offline_network_calls"], "fmp_requests": counts["fmp_requests"], "finnhub": 0, "official": 0, "tier2": 0}}
        atomic_json(replay_root / "replay_summary.json", summary)
        _atomic_text(replay_root / "replay_summary.md", "# Targeted 30-Day Offline Replay\n\n" + f"Status: **{summary['status']}**. Formal completion: **{formal}**.\n\n" + "\n".join(f"- {key}: {value}" for key, value in counts.items()) + "\n")
        if formal:
            _write_derived_qa(days, result, replay_id)
        return {"summary": summary, "result": result_payload, "days": days, "skipped": skipped}
    finally:
        if prior_cache_override is None:
            os.environ.pop("MARKET_BOT_CACHE_DIR", None)
        else:
            os.environ["MARKET_BOT_CACHE_DIR"] = prior_cache_override


def _write_derived_qa(days: list[dict[str, Any]], result: dict[str, Any], replay_id: str) -> None:
    """Refresh review queues only after every formal replay gate passes."""
    import build_historical_evaluation_batch as batch

    destination = RECOVERY / "derived_qa_v1"
    market_rows = batch._market_rows(days)
    stock_rows = [row for row in batch._stock_rows(days) if abs(float(row.get("move_pct") or 0)) >= 4.0]
    labels_dir = ROOT / "output/evaluation/manual_qa_v1"
    batch._write_csv(destination / "market_day_review_queue.csv", market_rows, list(market_rows[0]) if market_rows else ["date"], ("date",), preserve_path=labels_dir / "market_day_review_queue_manual_qa_v1.csv")
    batch._write_csv(destination / "stock_review_queue.csv", stock_rows, list(stock_rows[0]) if stock_rows else ["date", "symbol"], ("date", "symbol"), preserve_path=labels_dir / "stock_review_queue_manual_qa_v1.csv")
    baseline_path = MILESTONE / "resume_after_dxy/replays/resume_20261005_160401/evaluation/market_driver_multiday.json"
    baseline = _read_json(baseline_path) or {}
    before_days = {row.get("market_date"): row for row in baseline.get("days", []) if isinstance(row, dict)}
    new_days = {row.get("market_date"): row for row in days}
    market_labels = _read_csv_rows(labels_dir / "market_day_review_queue_manual_qa_v1.csv")
    stock_labels = _read_csv_rows(labels_dir / "stock_review_queue_manual_qa_v1.csv")
    reviewed_market = {str(row.get("date")) for row in market_labels if row.get("market_manual_label")}
    reviewed_stocks = {(str(row.get("date")), str(row.get("symbol", "")).upper()) for row in stock_labels if row.get("manual_label")}
    diffs = []
    for day in sorted(reviewed_market):
        before = before_days.get(day, {}).get("main_market_driver") or {}
        after = new_days.get(day, {}).get("main_market_driver") or {}
        fields = {"driver_type": (before.get("driver_type"), after.get("driver_type")), "canonical_event_title": (before.get("canonical_event_title"), after.get("canonical_event_title")), "verification_status": (before.get("verification_status"), after.get("verification_status"))}
        changed = {key: {"before": a, "after": b} for key, (a, b) in fields.items() if a != b}
        if changed:
            diffs.append({"date": day, "scope": "MARKET", "changes": changed})
    for day, symbol in sorted(reviewed_stocks):
        before = next((row for row in before_days.get(day, {}).get("stock_records", []) if str(row.get("symbol", "")).upper() == symbol), {})
        after = next((row for row in new_days.get(day, {}).get("stock_records", []) if str(row.get("symbol", "")).upper() == symbol), {})
        fields = ("move_pct", "overall_explanation_status", "selected_event", "final_driver_status")
        changed = {key: {"before": before.get(key), "after": after.get(key)} for key in fields if before.get(key) != after.get(key)}
        if changed:
            diffs.append({"date": day, "symbol": symbol, "scope": "STOCK", "changes": changed})
    atomic_json(destination / "reviewed_row_output_differences.json", {"replay_id": replay_id, "reviewed_rows_checked": len(reviewed_market) + len(reviewed_stocks), "changed_rows": diffs, "human_labels_modified": False})
    atomic_json(destination / "derived_qa_summary.json", {"replay_id": replay_id, "market_review_rows": len(market_rows), "stock_abs_move_ge_4_rows": len(stock_rows), "reviewed_rows_changed": len(diffs), "human_labels_modified": False})


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _archive_original_hashes() -> dict[str, str]:
    hashes: dict[str, str] = {}
    for day in TARGET_DATES:
        path = MILESTONE / "history" / day / "movers_snapshot.json"
        hashes[day] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = SOURCE_ROOT / "original_artifact_hashes.json"
    if manifest.is_file():
        prior = _read_json(manifest)
        if prior != hashes:
            raise RuntimeError("ORIGINAL_PARTIAL_MOVER_ARTIFACT_CHANGED")
    else:
        atomic_json(manifest, hashes)
    return hashes


def execute(*, supervise: bool = True) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    key = os.getenv("TWELVE_DATA_API_KEY", "").strip()
    if not key or key == "PASTE_API_KEY_HERE":
        raise RuntimeError("TWELVE_DATA_API_KEY_MISSING")
    scope = _scope_preflight()
    _archive_preflight()
    _archive_original_hashes()
    SOURCE_ROOT.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("targeted_mover_recovery_v1")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(SOURCE_ROOT / "recovery.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream)
    ledger = TwelveDataRequestLedger(REQUEST_LEDGER_PATH)
    limiter, baseline_daily_used = _configure_limiter(ledger)
    client = TwelveDataClient(key, rate_limiter=limiter)
    progress: dict[str, dict[str, Any]] = {}
    logger.info("RECOVERY_SCOPE dates=%s symbols_per_date=120 daily_quota=%d persisted_used=%d available=%d", ",".join(TARGET_DATES), limiter.limits.daily_quota, limiter.daily_requests, max(0, limiter.limits.daily_quota - limiter.daily_requests))
    logger.info("Provider scope: Twelve Data only; other providers disabled")

    from evaluation.resume_extended_dataset_after_dxy import _convert_sigterm_to_interrupt

    with _convert_sigterm_to_interrupt(), prevent_system_sleep(logger=logger):
        for day in TARGET_DATES:
            while True:
                valid_existing, _ = _validate_recovered_date(day)
                if valid_existing:
                    progress[day] = _write_date_progress(day, "COMPLETE", ledger)
                    break
                missing = _missing_symbols(day)
                remaining_all = sum(len(_missing_symbols(target)) for target in TARGET_DATES)
                # Reserve one bounded HTTP retry per remaining symbol as well
                # as the first request, preserving headroom under the daily cap.
                required_reserve = remaining_all * 2
                available = max(0, limiter.limits.daily_quota - limiter.daily_requests)
                if required_reserve > available:
                    next_reset = datetime.combine(datetime.now(timezone.utc).date() + timedelta(days=1), wall_time.min, tzinfo=timezone.utc).isoformat()
                    progress[day] = _write_date_progress(day, "DAILY_QUOTA_PAUSED", ledger, "DAILY_QUOTA_PAUSED")
                    pause = {"reason": "DAILY_QUOTA_PAUSED", "required_reserve": required_reserve, "available": available, "next_eligible_at": next_reset}
                    summary = _write_summary(ledger, progress, status="DAILY_QUOTA_PAUSED", quota_pause=pause, scope=scope)
                    logger.warning("QUOTA_PAUSED reason=DAILY_QUOTA_PAUSED date=%s required_reserve=%d available=%d next_eligible_at=%s", day, required_reserve, available, next_reset)
                    return summary
                logger.info("RECOVERY_DATE_START date=%s valid_checkpoint_symbols=%d remaining=%d", day, len(_valid_results(day)), len(missing))
                folder = SOURCE_ROOT / day
                folder.mkdir(parents=True, exist_ok=True)
                checkpoint_path, _ = _checkpoint(day)
                original_output = movers.OUTPUT_DIR
                original_shadow = movers.run_shadow_benchmark
                original_formatter = movers._mover_output
                previous_checkpoint = getattr(client, "mover_checkpoint_path", None)
                movers.OUTPUT_DIR = folder
                movers.run_shadow_benchmark = lambda *args, **kwargs: logger.info("RECOVERY_SHADOW_BENCHMARK_DISABLED")
                movers._mover_output = _record_with_source_data
                client.mover_checkpoint_path = checkpoint_path
                try:
                    mover_path = movers.run_movers_snapshot(
                        client, logger, day, force_refresh=True,
                        prefilter_enabled=False, historical=True,
                    )
                    snapshot = _read_json(mover_path)
                    if isinstance(snapshot, dict):
                        successful = set(_valid_results(day))
                        snapshot["errors"] = [row for row in snapshot.get("errors", []) if str(row.get("symbol", "")).upper() not in successful]
                        snapshot["symbols_failed"] = 120 - len(successful)
                        if isinstance(snapshot.get("universe"), dict):
                            snapshot["universe"].update({"total_symbols": 120, "processed": 120, "valid": len(successful), "failed": 120 - len(successful)})
                        snapshot["recovery_provenance"] = {"recovery_version": 1, "source_of_truth": "Twelve Data historical 1day", "full_universe_refetch": True, "original_artifact_preserved": True, "requested_market_date": day}
                        atomic_json(mover_path, snapshot)
                except QuotaPauseRequired as exc:
                    progress[day] = _write_date_progress(day, "QUOTA_PAUSED", ledger, "MINUTE_QUOTA_PAUSED")
                    pause = {"reason": "MINUTE_QUOTA_PAUSED", "market_date": day, "next_eligible_at": exc.next_eligible_at, "required_wait_seconds": exc.required_wait_seconds, "context": exc.context}
                    summary = _write_summary(ledger, progress, status="QUOTA_PAUSED", quota_pause=pause, scope=scope)
                    logger.warning("QUOTA_PAUSED reason=MINUTE_QUOTA_PAUSED date=%s next_eligible_at=%s required_wait_seconds=%.1f", day, exc.next_eligible_at, exc.required_wait_seconds)
                    if not supervise:
                        return summary
                    wait_seconds = max(0.0, (datetime.fromisoformat(exc.next_eligible_at.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds())
                    if wait_seconds > 90:
                        return summary
                    from evaluation.auto_resume_phase_b import _wait_until
                    _wait_until(exc.next_eligible_at, wait_seconds, reason="TARGETED_RECOVERY_MINUTE_QUOTA")
                    continue
                except KeyboardInterrupt:
                    progress[day] = _write_date_progress(day, "INTERRUPTED", ledger, "KEYBOARD_INTERRUPT")
                    _write_summary(ledger, progress, status="INTERRUPTED", scope=scope)
                    logger.warning("RECOVERY_INTERRUPTED date=%s checkpoint_preserved=true", day)
                    raise
                except movers.MoversCollectionFailure as exc:
                    progress[day] = _write_date_progress(day, "PARTIAL", ledger, str(exc))
                    logger.error("RECOVERY_DATE_INCOMPLETE date=%s reason=%s", day, exc)
                    _write_summary(ledger, progress, status="PARTIAL", scope=scope)
                    return _write_summary(ledger, progress, status="PARTIAL", scope=scope)
                finally:
                    movers.OUTPUT_DIR = original_output
                    movers.run_shadow_benchmark = original_shadow
                    movers._mover_output = original_formatter
                    if previous_checkpoint is None:
                        if hasattr(client, "mover_checkpoint_path"):
                            delattr(client, "mover_checkpoint_path")
                    else:
                        client.mover_checkpoint_path = previous_checkpoint

                valid, reason = _validate_recovered_date(day)
                progress[day] = _write_date_progress(day, "COMPLETE" if valid else "PARTIAL", ledger, reason)
                if not valid:
                    summary = _write_summary(ledger, progress, status="PARTIAL", scope=scope)
                    logger.error("RECOVERY_DATE_INCOMPLETE date=%s reason=%s", day, reason)
                    return summary
                logger.info("RECOVERY_DATE_COMPLETE date=%s valid=120/120", day)
                break

    # Verify the immutable source artifacts were not altered during recovery.
    _archive_original_hashes()
    source_rows = _preflight_all_dates()
    _atomic_csv(RECOVERY / "replay/readiness_report.csv", source_rows, ["date", "market_snapshot", "movers_snapshot", "market_driver_snapshot", "market_news_cache", "source_status", "reason"])
    source_ready = sum(row["source_status"] == "SOURCE_READY" for row in source_rows)
    if source_ready != 30:
        summary = _write_summary(ledger, progress, status="SOURCE_PREFLIGHT_FAILED", scope=scope)
        summary["source_ready_count"] = source_ready
        atomic_json(ROOT_SUMMARY_JSON, summary)
        return summary
    replay_result = _run_offline_replay(source_rows)
    summary = _write_summary(ledger, progress, status=replay_result["summary"]["status"], scope=scope)
    summary["source_ready_count"] = source_ready
    summary["final_replay"] = replay_result["summary"]
    summary["replay_performed"] = True
    summary["reviewed_row_output_changes"] = _read_json(RECOVERY / "derived_qa_v1/reviewed_row_output_differences.json") if replay_result["summary"]["formal_completion"] else None
    atomic_json(ROOT_SUMMARY_JSON, summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Perform the authorized two-date Twelve Data recovery.")
    parser.add_argument("--once", action="store_true", help="Persist QUOTA_PAUSED and return instead of resuming after minute-window waits.")
    args = parser.parse_args()
    if not args.execute:
        parser.error("--execute is required; live scope is restricted to 2026-08-19 and 2026-09-22")
    try:
        summary = execute(supervise=not args.once)
    except KeyboardInterrupt:
        print("RECOVERY_INTERRUPTED checkpoint_preserved=true")
        return 130
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("status") == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
