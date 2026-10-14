"""Resume the existing 30-session dataset after the DXY-only cache repair."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import signal
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app
import build_historical_market_cache as historical
import movers
from dotenv import load_dotenv
from drivers import runtime as driver_runtime
from evaluation import run_extended_dataset_v2 as runner
from evaluation.quota_runner import TwelveDataRequestLedger
from collectors.rate_limit import QuotaPauseRequired
from utils.windows_power import prevent_system_sleep

MILESTONE = runner.MILESTONE
RESUME_DIR = MILESTONE / "resume_after_dxy"
RELIABILITY_DIR = RESUME_DIR / "runner_reliability_v1"
LIMITER_STATE_PATH = RESUME_DIR / "twelve_data_limiter_state.json"
REQUEST_LEDGER_PATH = RESUME_DIR / "twelve_data_request_ledger.json"


@contextmanager
def _convert_sigterm_to_interrupt():
    """Translate SIGTERM into the same cleanup path used for Ctrl+C."""
    previous_handler = signal.getsignal(signal.SIGTERM)

    def interrupt_on_sigterm(signum: int, frame: Any) -> None:
        del signum, frame
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt_on_sigterm)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


def _atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    runner.atomic_csv(path, rows, list(dict.fromkeys(key for row in rows for key in row)) or ["date"])


def _atomic_json(path: Path, payload: Any) -> None:
    runner.atomic_json(path, payload)


def _artifact_states(market_date: str) -> dict[str, Any]:
    """Inspect the staged component state without network calls."""
    directory = MILESTONE / "history" / market_date
    market_path = directory / "market_snapshot.json"
    movers_path = directory / "movers_snapshot.json"
    driver_path = directory / "market_driver_snapshot.json"
    market = runner.load_object(market_path)
    market_valid = bool(market and historical._validate_historical_market(market, market_date))
    mover_snapshot = runner.load_object(movers_path)
    movers_valid = movers.load_valid_movers_cache(market_date, path=movers_path, require_historical_integrity=True) is not None
    market_news = historical._market_news_state(market_date)
    symbols = historical._mover_symbols(mover_snapshot if movers_valid else None)
    finnhub_hits, finnhub_total = historical._finnhub_cache_status(market_date, symbols)
    driver = runner.load_object(driver_path)
    driver_valid = bool(
        driver
        and driver.get("market_date") == market_date
        and isinstance(driver.get("driver_analysis"), dict)
        and isinstance(driver.get("metrics"), dict)
    )
    stock_news_status = "DEFERRED" if not movers_valid else (
        "HIT" if finnhub_total == 0 or finnhub_hits >= finnhub_total else
        "MISS" if finnhub_hits == 0 else "PARTIAL"
    )
    stock_news_incomplete = movers_valid and stock_news_status != "HIT"
    verification_status = "DEFERRED" if not movers_valid or stock_news_incomplete else ("READY" if driver_valid else "MISSING")
    if not movers_valid:
        snapshot_status = "STALE_MOVER_DEPENDENCY" if driver_valid else "DEFERRED"
    elif stock_news_incomplete:
        snapshot_status = "STALE_STOCK_NEWS_DEPENDENCY" if driver_valid else "DEFERRED"
    else:
        snapshot_status = "HIT" if driver_valid else "MISSING"

    if not market_valid:
        actions = ["market"]
        deferred = ["movers", "market_news", "stock_news", "verification", "market_driver_snapshot"]
    elif not movers_valid:
        actions = ["movers"]
        deferred = ["market_news", "stock_news", "verification", "market_driver_snapshot"]
    else:
        actions = []
        deferred = []
        if market_news != "HIT":
            actions.append("market_news")
        if stock_news_status != "HIT":
            actions.append("stock_news")
        if verification_status != "READY":
            actions.append("verification")
        if snapshot_status != "HIT":
            actions.append("market_driver_snapshot")
    return {
        "market_date": market_date,
        "market_data_status": "HIT" if market_valid else ("INVALID" if market else "MISSING"),
        "movers_status": "HIT" if movers_valid else ("INVALID" if mover_snapshot else "MISSING"),
        "market_news_status": market_news,
        "stock_news_status": stock_news_status,
        "verification_status": verification_status,
        "snapshot_status": snapshot_status,
        "mover_symbols_required": len(symbols),
        "finnhub_cache_hits": finnhub_hits,
        "finnhub_cache_total": finnhub_total,
        "remaining_actions": ",".join(actions) if actions else "none",
        "deferred": ",".join(deferred) if deferred else "none",
        "_actions": actions,
        "_market": market,
        "_movers": mover_snapshot,
        "_driver": driver,
    }


def _reused_credits(state: dict[str, Any]) -> int:
    """Sum Twelve Data credits attributed to valid market/mover artifacts."""
    market = state.get("_market") or {}
    movers_snapshot = state.get("_movers") or {}
    market_credit = int(((market.get("providers") or {}).get("Twelve Data") or {}).get("requested", 0) or 0) if state["market_data_status"] == "HIT" else 0
    mover_credit = int(((movers_snapshot.get("request_statistics") or {}).get("twelve_data_credits_used", 0) or 0)) if state["movers_status"] == "HIT" else 0
    return market_credit + mover_credit


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_resume_state(progress: dict[str, dict[str, Any]], incremental: dict[str, dict[str, Any]], batch_number: int | None = None, batch_dates: list[str] | None = None) -> None:
    dates = runner.load_dates()
    progress_rows = [progress[day] for day in dates if day in progress]
    usage_rows = [incremental[day] for day in dates if day in incremental]
    if progress_rows:
        _atomic_csv(RESUME_DIR / "build_progress.csv", progress_rows)
    if usage_rows:
        _atomic_csv(RESUME_DIR / "provider_usage_incremental.csv", usage_rows)
    _atomic_json(RESUME_DIR / "checkpoint.json", {
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "batch_number": batch_number,
        "batch_dates": batch_dates or [],
        "progress": progress_rows,
        "provider_usage_incremental": usage_rows,
    })


def _logger() -> logging.Logger:
    RESUME_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("resume_after_dxy")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(RESUME_DIR / "resume.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream)
    return logger


def _new_cache_manifest() -> set[Path]:
    from cache_config import cache_root
    root = cache_root() / "finnhub"
    return set(root.glob("*/*.json")) if root.exists() else set()


def _date_incremental_usage(
    market_date: str,
    before: dict[str, Any],
    after_market: dict[str, Any] | None,
    after_movers: dict[str, Any] | None,
    driver: dict[str, Any] | None,
    news_result: dict[str, Any] | None,
    cache_state: dict[str, Any],
    finnhub_before: set[Path],
    finnhub_after: set[Path],
) -> dict[str, Any]:
    after_market = after_market or {}
    after_movers = after_movers or {}
    metrics = (driver or {}).get("metrics", {})
    news_metrics = (news_result or {}).get("provider_metrics", {})
    newly_cached = sorted(finnhub_after - finnhub_before)
    incremental_td = 0
    if before["market_data_status"] != "HIT":
        incremental_td += int(((after_market.get("providers") or {}).get("Twelve Data") or {}).get("requested", 0) or 0)
    if before["movers_status"] != "HIT":
        incremental_td += int((after_movers.get("request_statistics") or {}).get("twelve_data_credits_used", 0) or 0)
    reused = _reused_credits(before)
    hit_count = sum(value == "HIT" for value in (
        before.get("market_data_status"), before.get("movers_status"), before.get("market_news_status"),
        before.get("stock_news_status"), before.get("verification_status"), before.get("snapshot_status"),
    ))
    build_count = 0
    return {
        "date": market_date,
        "incremental_twelve_data_credits": incremental_td,
        "reused_twelve_data_artifact_credits": reused,
        "total_artifact_recorded_credits": incremental_td + reused,
        "incremental_finnhub_requests": int(metrics.get("finnhub_live_requests", 0) or 0),
        "unique_symbols_fetched": ",".join(sorted({path.parent.name.upper() for path in newly_cached})),
        "unique_symbols_fetched_count": len({path.parent.name.upper() for path in newly_cached}),
        "incremental_marketaux_requests": int(news_metrics.get("marketaux_requests", 0) or 0),
        "incremental_official_requests": int(metrics.get("official_requests", 0) or 0),
        "incremental_tier2_requests": int(metrics.get("tier2_requests", 0) or 0),
        "fmp_requests": int(metrics.get("fmp_requests", 0) or 0),
        "cache_hits": hit_count,
        "cache_builds": build_count,
        "cache_failures": sum(value == "FAILED" for value in cache_state.values() if isinstance(value, str)),
        "runtime_seconds": 0.0,
    }


def _status_from_artifacts(market_date: str, previous: dict[str, Any]) -> dict[str, Any]:
    state = _artifact_states(market_date)
    errors = []
    if state["market_data_status"] != "HIT":
        errors.append("MARKET_DATA")
    if state["movers_status"] != "HIT":
        errors.append("MOVERS")
    status = "COMPLETE" if not errors else "PARTIAL"
    return {
        "date": market_date,
        "status": status,
        "reason": ";".join(errors),
        "market_data": state["market_data_status"],
        "movers": state["movers_status"],
        "market_news": state["market_news_status"],
        "stock_news_and_verification": "HIT" if state["snapshot_status"] == "HIT" else "DEFERRED",
        "runtime_seconds": 0.0,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def select_work_dates(dates: list[str], states: list[dict[str, Any]], max_dates: int | None) -> list[str]:
    """Select dates requiring work, optionally limiting this invocation."""
    pending = [day for day, state in zip(dates, states) if state.get("_actions")]
    return pending if max_dates is None else pending[:max_dates]


def build_status_counts(progress: dict[str, dict[str, Any]]) -> dict[str, int]:
    """Keep quota pauses and interrupts distinct from actual failures."""
    return {status: sum(row.get("status") == status for row in progress.values()) for status in ("COMPLETE", "PARTIAL", "QUOTA_PAUSED", "INTERRUPTED", "FAILED")}


def _run_offline_replay(dates: list[str], progress: dict[str, dict[str, Any]], incremental: dict[str, dict[str, Any]], logger: logging.Logger) -> dict[str, Any]:
    """Replay all selected dates offline and write only resume-scoped reports."""
    from cache_config import cache_root
    import evaluate_market_drivers as replay
    import evaluate_market_drivers_product as product
    import build_historical_evaluation_batch as batch
    from evaluation import generate_extended_dataset_outputs as output_builder

    replay_id = datetime.now(timezone.utc).strftime("resume_%Y%m%d_%H%M%S")
    replay_root = RESUME_DIR / "replays" / replay_id
    evaluation_dir = replay_root / "evaluation"
    for market_date in dates:
        source = MILESTONE / "history" / market_date
        target = evaluation_dir / "history" / market_date
        target.mkdir(parents=True, exist_ok=True)
        for name in ("market_snapshot.json", "movers_snapshot.json", "market_driver_snapshot.json"):
            runner.stage_copy(source / name, target / name)
    source_news = cache_root() / "market_driver_news"
    target_news = replay_root / "cache" / "market_driver_news"
    for market_date in dates:
        if source_news.exists():
            for candidate in source_news.glob(f"*{market_date}*.json"):
                runner.stage_copy(candidate, target_news / candidate.name)
        runner.stage_copy(source_news / f"{market_date}.json", target_news / f"{market_date}.json")
    with replay.offline_network_guard():
        result = product.evaluate_product(dates[0], dates[-1], market_dates=dates, output_dir=evaluation_dir)
    days, skipped = result.get("days", []), result.get("skipped_dates", [])
    for day in days:
        day["integrity"] = batch.evaluate_integrity(day, output_dir=evaluation_dir)
        if day["integrity"].get("integrity_status") == "FAIL" and day.get("status") == "READY":
            day["status"] = "PARTIAL"
    runner.atomic_json(evaluation_dir / "market_driver_multiday.json", replay.to_json_safe(result))
    runner.atomic_text(evaluation_dir / "market_driver_multiday.md", result.get("markdown", "") or "Offline replay completed; see JSON and review queues.")
    output_builder.generate(RESUME_DIR, replay_id, dates, days, skipped, progress, incremental, result)
    # Keep this new run's replay reference and summary separate from the prior
    # Phase B summary files in the milestone root.
    _write_resume_summary(dates, progress, incremental, result, replay_id, skipped)
    return {"result": result, "replay_id": replay_id, "days": days, "skipped": skipped}


def _write_resume_summary(dates: list[str], progress: dict[str, dict[str, Any]], incremental: dict[str, dict[str, Any]], replay_result: dict[str, Any], replay_id: str, skipped: list[dict[str, Any]]) -> dict[str, Any]:
    dataset_summary = json.loads((RESUME_DIR / "dataset_summary.json").read_text(encoding="utf-8")) if (RESUME_DIR / "dataset_summary.json").is_file() else {}
    dataset_metrics = dataset_summary.get("metrics", {})
    readiness_counts = dataset_summary.get("readiness_counts", {})
    metrics = dict(replay_result.get("metrics", {}))
    metrics["ready_days_evaluated"] = int(readiness_counts.get("READY", 0))
    metrics["partial_days_evaluated"] = int(readiness_counts.get("PARTIAL", 0))
    metrics["skipped_days"] = len(skipped)
    totals = {}
    for field in ("incremental_twelve_data_credits", "reused_twelve_data_artifact_credits", "total_artifact_recorded_credits", "incremental_finnhub_requests", "unique_symbols_fetched_count", "incremental_official_requests", "incremental_tier2_requests", "incremental_marketaux_requests", "fmp_requests", "cache_hits", "cache_builds", "cache_failures"):
        totals[field] = sum(int(row.get(field, 0) or 0) for row in incremental.values())
    # Preserve the exact 30-date sample in progress even when a date was an
    # all-cache skip.
    for market_date in dates:
        progress.setdefault(market_date, _status_from_artifacts(market_date, {}))
        incremental.setdefault(market_date, {"date": market_date, "incremental_twelve_data_credits": 0, "reused_twelve_data_artifact_credits": 0, "total_artifact_recorded_credits": 0, "incremental_finnhub_requests": 0, "unique_symbols_fetched_count": 0, "incremental_official_requests": 0, "incremental_tier2_requests": 0, "fmp_requests": 0, "cache_hits": 0, "cache_builds": 0, "cache_failures": 0, "runtime_seconds": 0.0})
    # Recompute after filling skipped dates.
    totals = {field: sum(int(row.get(field, 0) or 0) for row in incremental.values()) for field in totals}
    status_after = build_status_counts(progress)
    summary = {
        "date_range": {"from": dates[0], "to": dates[-1]}, "selected_dates": dates,
        "build_status_counts": status_after, "replay_readiness": metrics,
        "replay_skipped_dates": skipped, "incremental_usage_totals": totals,
        "runtime_seconds": round(sum(float(row.get("runtime_seconds", 0) or 0) for row in incremental.values()), 3),
        "fmp_requests": totals["fmp_requests"], "offline_network_calls": int(metrics.get("offline_network_calls", 0) or 0),
        "replay_id": replay_id,
        "original_10_day_regression": metrics.get("original_10_day_regression"),
        "readiness_counts": readiness_counts,
        "integrity_counts": dataset_summary.get("integrity_counts", {}),
        "main_stock_review_rows_abs_ge_4": int(dataset_metrics.get("stock_review_rows_abs_move_ge_4pct", 0)),
        "all_stock_review_rows_if_applicable": len(_read_csv(RESUME_DIR / "stock_review_queue.csv")),
        "diagnostic_rows": int(dataset_metrics.get("diagnostic_event_sample_rows", 0)),
        "new_unlabeled_market_rows": int(dataset_metrics.get("new_unlabeled_market_rows", 0)),
        "new_unlabeled_stock_rows": int(dataset_metrics.get("new_unlabeled_stock_rows", 0)),
        "stock_parity_mismatches": int(dataset_metrics.get("stock_parity_mismatches", 0)),
        "cross_symbol_candidate_leaks": int(dataset_metrics.get("cross_symbol_candidate_leaks", 0)),
        "future_event_violations": int(dataset_metrics.get("future_event_violations", 0)),
        "remaining_blockers": [row for row in progress.values() if row.get("status") in {"PARTIAL", "FAILED", "QUOTA_PAUSED"}],
    }
    _atomic_json(RESUME_DIR / "resume_summary.json", summary)
    lines = [
        "# Phase B Resume After DXY — Summary", "",
        f"- Window: {dates[0]} through {dates[-1]} ({len(dates)} dates).",
        f"- Build states: COMPLETE={status_after['COMPLETE']}, PARTIAL={status_after['PARTIAL']}, FAILED={status_after['FAILED']}, QUOTA_PAUSED={status_after['QUOTA_PAUSED']}.",
        f"- Offline replay (integrity-authoritative): READY={readiness_counts.get('READY', 0)}, PARTIAL={readiness_counts.get('PARTIAL', 0)}, skipped/missing={len(skipped)}.",
        f"- Incremental Twelve Data credits={totals['incremental_twelve_data_credits']}; reused artifact credits={totals['reused_twelve_data_artifact_credits']}; combined recorded={totals['total_artifact_recorded_credits']}.",
        f"- Finnhub requests={totals['incremental_finnhub_requests']}; new symbols={totals['unique_symbols_fetched_count']}; official={totals['incremental_official_requests']}; Tier2={totals['incremental_tier2_requests']}; FMP={totals['fmp_requests']}.",
        f"- Offline network calls={summary['offline_network_calls']}; replay id={replay_id}.",
        f"- Review counts: stock abs(move) >=4%={summary['main_stock_review_rows_abs_ge_4']}; all stock rows={summary['all_stock_review_rows_if_applicable']}; diagnostic rows={summary['diagnostic_rows']}; market rows={summary['new_unlabeled_market_rows']}.",
        "",
    ]
    if summary["remaining_blockers"]:
        lines.extend(["## Remaining blockers", "", *[f"- {row.get('date')}: {row.get('reason') or row.get('status')}" for row in summary["remaining_blockers"]], ""])
    runner.atomic_text(RESUME_DIR / "resume_summary.md", "\n".join(lines))
    return summary


def write_runner_reliability_report() -> dict[str, Any]:
    """Reconcile readiness from offline integrity outputs and write isolated diagnostics."""
    RELIABILITY_DIR.mkdir(parents=True, exist_ok=True)
    dataset_path = RESUME_DIR / "dataset_summary.json"
    resume_path = RESUME_DIR / "resume_summary.json"
    integrity_path = RESUME_DIR / "integrity_report.csv"
    if not all(path.is_file() for path in (dataset_path, resume_path, integrity_path)):
        raise FileNotFoundError("OFFLINE_READINESS_REPORTS_MISSING")
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    resume_summary = json.loads(resume_path.read_text(encoding="utf-8"))
    dates = list(dataset.get("selected_dates", []))
    skipped_days = dataset.get("skipped_dates", [])
    skipped_dates = {str(item.get("market_date")) for item in skipped_days if isinstance(item, dict)}
    with integrity_path.open(encoding="utf-8-sig", newline="") as handle:
        integrity_rows = list(csv.DictReader(handle))
    integrity_by_date = {str(row.get("date")): row for row in integrity_rows}
    readiness = {
        "READY": sum(integrity_by_date.get(day, {}).get("readiness") == "READY" for day in dates),
        "PARTIAL": sum(integrity_by_date.get(day, {}).get("readiness") == "PARTIAL" for day in dates),
        "MISSING": sum(day in skipped_dates for day in dates),
    }
    if sum(readiness.values()) != len(dates):
        raise RuntimeError("READINESS_COVERAGE_INCOMPLETE")

    pre_resume = {row.get("market_date"): row for row in _read_csv(RESUME_DIR / "pre_resume_status.csv")}
    status_rows: list[dict[str, Any]] = []
    for day in dates:
        integrity = integrity_by_date.get(day)
        if day in skipped_dates:
            replay_state, integrity_state = "SKIPPED_MISSING", "FAIL"
            reason = ";".join(next((item.get("missing_components", []) for item in skipped_days if item.get("market_date") == day), [])) or "MISSING_REQUIRED_CACHE"
        elif integrity:
            replay_state = integrity.get("readiness") or "PARTIAL"
            integrity_state = integrity.get("integrity") or "FAIL"
            reason = integrity.get("reasons", "")
        else:
            replay_state, integrity_state, reason = "MISSING_REPORT_ROW", "FAIL", "INTEGRITY_ROW_MISSING"
        pre = pre_resume.get(day, {})
        status_rows.append({
            "date": day, "readiness": replay_state, "integrity": integrity_state,
            "reason": reason, "market_data_status": pre.get("market_data_status", ""),
            "movers_status": pre.get("movers_status", ""),
            "market_news_status": pre.get("market_news_status", ""),
            "stock_news_status": pre.get("stock_news_status", ""),
            "verification_status": pre.get("verification_status", ""),
            "snapshot_status": pre.get("snapshot_status", ""),
        })
    _atomic_csv(RELIABILITY_DIR / "current_checkpoint_status.csv", status_rows)

    # Fix the prior 12/1 discrepancy at its source of truth (integrity-derived
    # dataset summary), while preserving unrelated summary fields.
    canonical = {"READY": readiness["READY"], "PARTIAL": readiness["PARTIAL"], "FAILED": readiness["MISSING"]}
    resume_summary["readiness_counts"] = canonical
    replay_metrics = resume_summary.setdefault("replay_readiness", {})
    replay_metrics["ready_days_evaluated"] = readiness["READY"]
    replay_metrics["partial_days_evaluated"] = readiness["PARTIAL"]
    replay_metrics["skipped_days"] = readiness["MISSING"]
    resume_summary["integrity_counts"] = dataset.get("integrity_counts", {})
    runner.atomic_json(resume_path, resume_summary)
    summary_md = RESUME_DIR / "resume_summary.md"
    if summary_md.exists():
        lines = summary_md.read_text(encoding="utf-8").splitlines()
        lines = [
            f"- Offline replay (integrity-authoritative): READY={readiness['READY']}, PARTIAL={readiness['PARTIAL']}, skipped/missing={readiness['MISSING']}."
            if line.startswith("- Offline replay:") or line.startswith("- Offline replay (integrity-authoritative):") else line
            for line in lines
        ]
        runner.atomic_text(summary_md, "\n".join(lines) + "\n")

    integrity_readiness = {
        "READY": sum(row.get("readiness") == "READY" for row in status_rows),
        "PARTIAL": sum(row.get("readiness") == "PARTIAL" for row in status_rows),
        "MISSING": sum(row.get("readiness") == "SKIPPED_MISSING" for row in status_rows),
    }
    report_rows = [
        {"source": "dataset_summary", **readiness, "total": len(dates)},
        {"source": "resume_summary", "READY": canonical["READY"], "PARTIAL": canonical["PARTIAL"], "MISSING": canonical["FAILED"], "total": len(dates)},
        {"source": "integrity_report_plus_skipped", **integrity_readiness, "total": len(dates)},
    ]
    consistent = all((row["READY"], row["PARTIAL"], row["MISSING"], row["total"]) == (readiness["READY"], readiness["PARTIAL"], readiness["MISSING"], len(dates)) for row in report_rows)
    _atomic_csv(RELIABILITY_DIR / "readiness_consistency_check.csv", report_rows)

    ledger = TwelveDataRequestLedger(REQUEST_LEDGER_PATH)
    incremental = _read_csv(RESUME_DIR / "provider_usage_incremental.csv")
    legacy_credits = sum(int(row.get("legacy_incremental_twelve_data_credits", row.get("incremental_twelve_data_credits", 0)) or 0) for row in incremental)
    result = {
        "date_range": {"from": dates[0], "to": dates[-1]}, "selected_dates": len(dates),
        "readiness": readiness, "reports_consistent": consistent,
        "incremental_ledger_path": str(REQUEST_LEDGER_PATH),
        "ledger_new_twelve_data_credits": sum(int(row.get("credits", 0) or 0) for row in ledger.rows),
        "legacy_pre_ledger_incremental_twelve_data_credits": legacy_credits,
        "live_twelve_data_credits_used_by_reliability_task": 0,
        "fmp_requests": int(dataset.get("metrics", {}).get("provider_usage_totals", {}).get("fmp_requests", 0) or 0),
        "offline_network_calls": int(dataset.get("metrics", {}).get("offline_network_calls", 0) or 0),
        "main_stock_review_rows_abs_ge_4": int(dataset.get("metrics", {}).get("stock_review_rows_abs_move_ge_4pct", 0) or 0),
        "all_stock_review_rows_if_applicable": len(_read_csv(RESUME_DIR / "stock_review_queue.csv")),
        "diagnostic_rows": int(dataset.get("metrics", {}).get("diagnostic_event_sample_rows", 0) or 0),
        "resume_command": "python -m evaluation.resume_extended_dataset_after_dxy --execute --max-dates 1",
    }
    _atomic_json(RELIABILITY_DIR / "runner_reliability_summary.json", result)
    markdown = [
        "# Quota-Safe Resumable Runner V1", "",
        f"- Selected dates: {len(dates)} ({dates[0]} through {dates[-1]}).",
        f"- Canonical offline readiness: READY={readiness['READY']}, PARTIAL={readiness['PARTIAL']}, skipped/missing={readiness['MISSING']}; reports consistent={consistent}.",
        f"- Legacy pre-ledger Twelve Data credits={legacy_credits}; new ledger credits={result['ledger_new_twelve_data_credits']}; task live credits=0.",
        f"- FMP requests={result['fmp_requests']}; offline network calls={result['offline_network_calls']}.",
        f"- Review counts: stock abs(move) >=4%={result['main_stock_review_rows_abs_ge_4']}; all stock rows={result['all_stock_review_rows_if_applicable']}; diagnostic rows={result['diagnostic_rows']}.",
        f"- Incremental request ledger: `{REQUEST_LEDGER_PATH}`.",
        f"- One-date command: `{result['resume_command']}`.", "",
    ]
    runner.atomic_text(RELIABILITY_DIR / "runner_reliability_summary.md", "\n".join(markdown))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-only", action="store_true", help="Write and print the no-network pre-resume table.")
    parser.add_argument("--execute", action="store_true", help="Resume missing components in five-date batches, then replay offline.")
    parser.add_argument("--batch-number", type=int, help="Execute one five-date batch only (1-based).")
    parser.add_argument("--max-dates", type=int, help="Process at most this many dates requiring work, then checkpoint and exit.")
    parser.add_argument("--exclude-date", action="append", default=[], help="Supervisor-only date exclusion; preserve artifacts and report its current status without rebuilding it.")
    parser.add_argument("--report-only", action="store_true", help="Regenerate the offline runner-reliability report from existing outputs; no provider calls.")
    args = parser.parse_args()
    if args.execute and args.plan_only:
        parser.error("--execute and --plan-only are mutually exclusive")
    if not args.execute and not args.plan_only:
        if not args.report_only:
            parser.error("SPECIFY --plan-only, --execute, OR --report-only")
    if args.max_dates is not None and args.max_dates < 1:
        parser.error("--max-dates must be at least 1")
    if args.max_dates is not None and args.batch_number is not None:
        parser.error("--max-dates and --batch-number are mutually exclusive")
    load_dotenv(ROOT / ".env")
    dates = runner.load_dates()
    if len(dates) != 30 or dates[-1] != "2026-09-28":
        parser.error("PHASE_A_PLAN_MISSING_OR_NOT_30_DATES")
    historical.HISTORY_DIR = MILESTONE / "history"
    historical.MANIFEST_PATH = MILESTONE / "historical_cache_manifest.json"
    RESUME_DIR.mkdir(parents=True, exist_ok=True)
    logger = _logger()
    if args.report_only:
        result = write_runner_reliability_report()
        print(f"Runner reliability report written: {RELIABILITY_DIR}")
        print(f"Readiness: READY={result['readiness']['READY']} PARTIAL={result['readiness']['PARTIAL']} MISSING={result['readiness']['MISSING']}")
        return 0 if result["reports_consistent"] else 1

    # This is a fully offline audit and is persisted before the first possible
    # provider call, satisfying the resume gate.
    states = [_artifact_states(day) for day in dates]
    unknown_exclusions = set(args.exclude_date) - set(dates)
    if unknown_exclusions:
        parser.error("EXCLUDED_DATE_NOT_IN_SELECTED_PLAN:" + ",".join(sorted(unknown_exclusions)))
    for state in states:
        if state["market_date"] in set(args.exclude_date):
            state["_actions"] = []
            state["_supervisor_excluded"] = True
            state["remaining_actions"] = "none"
    public_states = [{key: value for key, value in state.items() if not key.startswith("_")} for state in states]
    _atomic_csv(RESUME_DIR / "pre_resume_status.csv", public_states)
    logger.info("PRE_RESUME_STATUS_WRITTEN path=%s provider_calls=0", RESUME_DIR / "pre_resume_status.csv")
    for row in public_states:
        logger.info("PRE_RESUME date=%s market=%s movers=%s market_news=%s stock_news=%s verification=%s snapshot=%s actions=%s", row["market_date"], row["market_data_status"], row["movers_status"], row["market_news_status"], row["stock_news_status"], row["verification_status"], row["snapshot_status"], row["remaining_actions"])
    if args.plan_only:
        print(f"Pre-resume status written: {RESUME_DIR / 'pre_resume_status.csv'}")
        return 0

    needs_work = select_work_dates(dates, states, args.max_dates)
    chunk_size = 5
    total_batches = (len(dates) + chunk_size - 1) // chunk_size
    if args.batch_number and not 1 <= args.batch_number <= total_batches:
        parser.error(f"BATCH_NUMBER_MUST_BE_1_TO_{total_batches}")
    if needs_work and not os.getenv("TWELVE_DATA_API_KEY", "").strip():
        logger.error("RESUME_PREFLIGHT_FAILED reason=TWELVE_DATA_API_KEY_MISSING")
        return 1
    if any("stock_news" in state["_actions"] or "verification" in state["_actions"] for state in states) and not os.getenv("FINNHUB_API_KEY", "").strip():
        logger.error("RESUME_PREFLIGHT_FAILED reason=FINNHUB_API_KEY_MISSING")
        return 1
    if not all(state["market_data_status"] == "HIT" for state in states):
        logger.error("RESUME_STOPPED reason=MARKET_CACHE_INVALID_REQUIRES_COMPONENT_REVIEW")
        return 1

    old_progress = {row["date"]: row for row in _read_csv(RESUME_DIR / "build_progress.csv")}
    old_usage = {row["date"]: row for row in _read_csv(RESUME_DIR / "provider_usage_incremental.csv")}
    ledger = TwelveDataRequestLedger(REQUEST_LEDGER_PATH)
    legacy_credits = sum(int(row.get("incremental_twelve_data_credits", 0) or 0) for row in old_usage.values())
    progress: dict[str, dict[str, Any]] = dict(old_progress)
    incremental: dict[str, dict[str, Any]] = dict(old_usage)
    paused = False
    interrupted = False
    max_dates_reached = False
    started = time.monotonic()
    batch_indices = [args.batch_number] if args.batch_number else list(range(1, total_batches + 1))
    dates_started = 0
    for batch_number in batch_indices:
        chunk = dates[(batch_number - 1) * chunk_size:batch_number * chunk_size]
        batch_work = [day for day in chunk if any(state["market_date"] == day and state["_actions"] for state in states)]
        logger.info("RESUME_BATCH %d/%d dates=%s work=%s", batch_number, total_batches, ",".join(chunk), ",".join(batch_work) or "none")
        initial_daily_requests = 0 if LIMITER_STATE_PATH.is_file() else legacy_credits
        with _convert_sigterm_to_interrupt(), prevent_system_sleep(logger=logger), runner.shared_batch_rate_limiter(
            app, state_path=LIMITER_STATE_PATH, request_recorder=ledger.record,
            initial_daily_requests=initial_daily_requests, max_blocking_wait_seconds=30.0,
        ) as limiter:
            logger.info("TWELVE_DATA_BUDGET configured_daily=%d persisted_used=%d available=%d", limiter.limits.daily_quota, limiter.daily_requests, max(0, limiter.limits.daily_quota - limiter.daily_requests))
            for market_date in chunk:
                initial = next(state for state in states if state["market_date"] == market_date)
                if not initial["_actions"]:
                    progress[market_date] = (
                        old_progress.get(market_date) or _status_from_artifacts(market_date, {})
                        if initial.get("_supervisor_excluded")
                        else _status_from_artifacts(market_date, old_progress.get(market_date, {}))
                    )
                    if market_date not in incremental:
                        reused = _reused_credits(initial)
                        incremental[market_date] = {"date": market_date, "incremental_twelve_data_credits": 0, "legacy_incremental_twelve_data_credits": 0, "ledger_twelve_data_credits": 0, "reused_twelve_data_artifact_credits": reused, "total_artifact_recorded_credits": reused, "incremental_finnhub_requests": 0, "unique_symbols_fetched": "", "unique_symbols_fetched_count": 0, "incremental_official_requests": 0, "incremental_tier2_requests": 0, "incremental_marketaux_requests": 0, "fmp_requests": 0, "cache_hits": 1, "cache_builds": 0, "cache_failures": 0, "runtime_seconds": 0.0}
                    continue
                logger.info("RESUME_DATE_START date=%s actions=%s", market_date, initial["remaining_actions"])
                mover_checkpoint = MILESTONE / "history" / market_date / ".movers_build" / ".resume" / "mover_checkpoint.json"
                completed_mover_symbols = 0
                if mover_checkpoint.is_file():
                    try:
                        completed_mover_symbols = len(json.loads(mover_checkpoint.read_text(encoding="utf-8")).get("valid_results", []))
                    except (OSError, ValueError, TypeError):
                        completed_mover_symbols = 0
                estimated_credits = 0 if initial["movers_status"] == "HIT" else max(0, 120 - completed_mover_symbols)
                estimated_available = max(0, limiter.limits.daily_quota - limiter.daily_requests)
                likely_finish = estimated_credits <= estimated_available and estimated_credits <= 8
                logger.info("PRE_RUN_BUDGET date=%s estimated_remaining_twelve_data_credits=%d configured_available=%d likely_finish_this_invocation=%s", market_date, estimated_credits, estimated_available, "YES" if likely_finish else "NO")
                before_manifest = _new_cache_manifest()
                date_start = time.monotonic()
                cache_state: dict[str, Any] = {}
                news_result = None
                try:
                    date_progress, artifact_usage = runner.process_date(
                        market_date, logger=logger, historical=historical, app_module=app,
                        movers_module=movers, driver_runtime=driver_runtime,
                        prevent_sleep=prevent_system_sleep, retry_known_failed_mover=True,
                    )
                except KeyboardInterrupt:
                    logger.warning("RESUME_INTERRUPTED date=%s", market_date)
                    state_now = _artifact_states(market_date)
                    date_progress = {
                        "date": market_date, "status": "INTERRUPTED", "reason": "KEYBOARD_INTERRUPT",
                        "market_data": state_now["market_data_status"], "movers": state_now["movers_status"],
                        "market_news": state_now["market_news_status"], "stock_news_and_verification": "DEFERRED",
                    }
                    artifact_usage = {}
                    interrupted = True
                except Exception as exc:
                    logger.exception("RESUME_DATE_EXCEPTION date=%s", market_date)
                    date_progress = {"date": market_date, "status": "PARTIAL", "reason": f"RESUME_{type(exc).__name__}", "market_data": "HIT", "movers": "FAILED", "market_news": "DEFERRED", "stock_news_and_verification": "DEFERRED"}
                    artifact_usage = {}
                # Capture actual post-run artifacts and only count usage deltas
                # for components that were missing/invalid at the preflight.
                stage_dir = MILESTONE / "history" / market_date
                market_after = runner.load_object(stage_dir / "market_snapshot.json")
                movers_after = runner.load_object(stage_dir / "movers_snapshot.json")
                driver_after = runner.load_object(stage_dir / "market_driver_snapshot.json")
                finnhub_after = _new_cache_manifest()
                inc = _date_incremental_usage(market_date, initial, market_after, movers_after, driver_after, news_result, cache_state, before_manifest, finnhub_after)
                old_row = old_usage.get(market_date, {})
                legacy_for_date = int(old_row.get("legacy_incremental_twelve_data_credits", old_row.get("incremental_twelve_data_credits", 0)) or 0)
                ledger_credits = ledger.totals().get(market_date, 0)
                inc["legacy_incremental_twelve_data_credits"] = legacy_for_date
                inc["ledger_twelve_data_credits"] = ledger_credits
                inc["incremental_twelve_data_credits"] = legacy_for_date + ledger_credits
                inc["total_artifact_recorded_credits"] = inc["incremental_twelve_data_credits"] + inc["reused_twelve_data_artifact_credits"]
                inc["cache_builds"] = sum(
                    initial.get(key) not in {"HIT", "READY"} and final_value in {"HIT", "READY"}
                    for key, final_value in (
                        ("market_data_status", _artifact_states(market_date)["market_data_status"]),
                        ("movers_status", _artifact_states(market_date)["movers_status"]),
                        ("market_news_status", historical._market_news_state(market_date)),
                        ("snapshot_status", "HIT" if driver_after else "MISSING"),
                    )
                )
                inc["cache_hits"] = max(0, 6 - inc["cache_builds"] - sum(value in {"MISSING", "INVALID", "FAILED"} for value in (
                    initial["market_data_status"], initial["movers_status"], initial["market_news_status"],
                    initial["stock_news_status"], initial["verification_status"], initial["snapshot_status"],
                )))
                # process_date returns cumulative artifact statistics separately;
                # actual driver/news incremental counters live in those new snapshots.
                if initial["snapshot_status"] != "HIT" and driver_after:
                    m = driver_after.get("metrics", {})
                    inc["incremental_finnhub_requests"] = int(m.get("finnhub_live_requests", 0) or 0)
                    inc["incremental_official_requests"] = int(m.get("official_requests", 0) or 0)
                    inc["incremental_tier2_requests"] = int(m.get("tier2_requests", 0) or 0)
                inc["incremental_marketaux_requests"] = int((artifact_usage or {}).get("marketaux_requests", 0) or 0) if initial["market_news_status"] != "HIT" else 0
                inc["unique_symbols_fetched"] = ",".join(sorted({path.parent.name.upper() for path in finnhub_after - before_manifest}))
                inc["unique_symbols_fetched_count"] = len({path.parent.name.upper() for path in finnhub_after - before_manifest})
                inc["runtime_seconds"] = round(time.monotonic() - date_start, 3)
                # A complete cache state outranks stale previous progress text.
                final_state = _artifact_states(market_date)
                if final_state["market_data_status"] == "HIT" and final_state["movers_status"] == "HIT" and final_state["snapshot_status"] == "HIT":
                    date_progress["status"] = "COMPLETE"
                    date_progress["reason"] = ""
                elif date_progress.get("status") != "QUOTA_PAUSED":
                    date_progress["status"] = "PARTIAL"
                date_progress["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
                progress[market_date] = {**date_progress, "runtime_seconds": inc["runtime_seconds"]}
                incremental[market_date] = inc
                _write_resume_state(progress, incremental, batch_number, chunk)
                logger.info("RESUME_DATE_DONE date=%s status=%s incremental_td=%s finnhub=%s official=%s tier2=%s", market_date, date_progress["status"], inc["incremental_twelve_data_credits"], inc["incremental_finnhub_requests"], inc["incremental_official_requests"], inc["incremental_tier2_requests"])
                if date_progress.get("status") == "QUOTA_PAUSED":
                    paused = True
                    break
                if interrupted:
                    break
                dates_started += 1
                if args.max_dates is not None and dates_started >= args.max_dates:
                    max_dates_reached = True
                    logger.info("MAX_DATES_REACHED count=%d", args.max_dates)
                    break
        _write_resume_state(progress, incremental, batch_number, chunk)
        _atomic_json(RESUME_DIR / f"checkpoint_batch_{batch_number:02d}.json", {"batch_number": batch_number, "dates": chunk, "progress": [progress[d] for d in chunk if d in progress], "provider_usage_incremental": [incremental[d] for d in chunk if d in incremental], "quota_paused": paused})
        logger.info("RESUME_BATCH_CHECKPOINT batch=%d/%d paused=%s", batch_number, total_batches, paused)
        if paused or interrupted or max_dates_reached:
            break

    # Record untouched dates as cache skips and carry their reused-artifact
    # accounting into this resume's own telemetry file.
    for market_date, state in zip(dates, states):
        if market_date not in progress:
            progress[market_date] = _status_from_artifacts(market_date, old_progress.get(market_date, {}))
            reused = _reused_credits(state)
            incremental[market_date] = {"date": market_date, "incremental_twelve_data_credits": 0, "reused_twelve_data_artifact_credits": reused, "total_artifact_recorded_credits": reused, "incremental_finnhub_requests": 0, "unique_symbols_fetched": "", "unique_symbols_fetched_count": 0, "incremental_official_requests": 0, "incremental_tier2_requests": 0, "incremental_marketaux_requests": 0, "fmp_requests": 0, "cache_hits": 1, "cache_builds": 0, "cache_failures": 0, "runtime_seconds": 0.0}
    _write_resume_state(progress, incremental)
    if max_dates_reached or interrupted:
        logger.info("RESUME_CHECKPOINT_EXIT max_dates_reached=%s interrupted=%s; offline replay deferred", max_dates_reached, interrupted)
        return 130 if interrupted else 0
    # Replay is always cache-only, including after a quota pause.
    replay_outputs = _run_offline_replay(dates, progress, incremental, logger)
    summary = json.loads((RESUME_DIR / "resume_summary.json").read_text(encoding="utf-8"))
    summary["quota_paused"] = paused
    summary["execution_runtime_seconds"] = round(time.monotonic() - started, 3)
    _atomic_json(RESUME_DIR / "resume_summary.json", summary)
    logger.info("RESUME_COMPLETE paused=%s replay=%s", paused, replay_outputs["replay_id"])
    return 2 if paused else 0


if __name__ == "__main__":
    raise SystemExit(main())
