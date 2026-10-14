"""Automatically supervise the existing quota-safe Phase B resume runner."""

from __future__ import annotations

import csv
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

from collectors.rate_limit import TwelveDataPlanLimits
from evaluation import resume_extended_dataset_after_dxy as resume
from evaluation.quota_runner import TwelveDataRequestLedger
from utils.windows_power import prevent_system_sleep

ROOT = Path(__file__).resolve().parents[1]
AUTO_DIR = resume.RESUME_DIR / "auto_resume"
AUTO_PROGRESS = AUTO_DIR / "auto_resume_progress.csv"
AUTO_USAGE = AUTO_DIR / "auto_resume_usage.csv"
AUTO_SUMMARY_JSON = AUTO_DIR / "auto_resume_summary.json"
AUTO_SUMMARY_MD = AUTO_DIR / "auto_resume_summary.md"
AUTO_INTEGRITY = AUTO_DIR / "final_integrity_report.csv"
MAX_IDENTICAL_FAILURES = 2


def _atomic_json_retry(
    path: Path,
    payload: Any,
    *,
    replace: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.replace,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = 4,
) -> None:
    """Atomically persist JSON, retrying brief Windows sharing violations."""
    if attempts < 1:
        raise ValueError("attempts must be at least one")
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(attempts):
            try:
                replace(temporary, path)
                return
            except PermissionError as exc:
                if attempt + 1 >= attempts:
                    raise PermissionError(f"AUTO_RESUME_SUMMARY_WRITE_FAILED: {path}") from exc
                sleep(0.05 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    resume.runner.atomic_csv(path, rows, fields)


def estimate_remaining_mover_credits(market_date: str, state: dict[str, Any]) -> int:
    """Conservative credit estimate; assumes full universe unless cache says otherwise."""
    if state.get("movers_status") == "HIT":
        return 0
    checkpoint = resume.MILESTONE / "history" / market_date / ".movers_build" / ".resume" / "mover_checkpoint.json"
    completed = 0
    if checkpoint.is_file():
        try:
            value = json.loads(checkpoint.read_text(encoding="utf-8"))
            if value.get("market_date") == market_date:
                completed = len(value.get("valid_results", []))
        except (OSError, ValueError, TypeError):
            completed = 0
    return max(0, 120 - completed)


def _daily_budget_state(required_credits: int, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    path = resume.LIMITER_STATE_PATH
    persisted: dict[str, Any] = {}
    if path.is_file():
        try:
            persisted = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            persisted = {}
    quota_day = now.date().isoformat()
    used = int(persisted.get("daily_requests", 0) or 0) if persisted.get("quota_day_utc") == quota_day else 0
    daily_quota = TwelveDataPlanLimits().daily_quota
    remaining = max(0, daily_quota - used)
    reset = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    return {
        "quota_day_utc": quota_day, "daily_quota": daily_quota, "daily_used": used,
        "daily_remaining": remaining, "required_credits": required_credits,
        "sufficient": required_credits <= remaining, "next_daily_reset": reset.isoformat(),
    }


def _next_eligible(progress: dict[str, Any]) -> tuple[str | None, float | None]:
    pause = progress.get("quota_pause")
    if isinstance(pause, str):
        try:
            pause = json.loads(pause)
        except ValueError:
            try:
                import ast
                pause = ast.literal_eval(pause)
            except (ValueError, SyntaxError):
                pause = None
    path = resume.LIMITER_STATE_PATH
    limiter_state: dict[str, Any] = {}
    if path.is_file():
        try:
            limiter_state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            pass
    candidate = (pause or {}).get("next_eligible_at") or limiter_state.get("next_eligible_at")
    if not candidate:
        return None, None
    try:
        eligible = datetime.fromisoformat(str(candidate).replace("Z", "+00:00"))
        if eligible.tzinfo is None:
            eligible = eligible.replace(tzinfo=timezone.utc)
    except ValueError:
        return str(candidate), None
    return eligible.isoformat(), max(0.0, (eligible - datetime.now(timezone.utc)).total_seconds())


def _run_one(excluded_dates: list[str], *, final_replay: bool = False) -> int:
    command = [sys.executable, "-m", "evaluation.resume_extended_dataset_after_dxy", "--execute"]
    if not final_replay:
        command.extend(["--max-dates", "1"])
    for value in excluded_dates:
        command.extend(["--exclude-date", value])
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def _find_next_work(excluded_dates: set[str]) -> tuple[str, dict[str, Any]] | None:
    for market_date in resume.runner.load_dates():
        if market_date in excluded_dates:
            continue
        state = resume._artifact_states(market_date)
        if state.get("_actions"):
            return market_date, state
    return None


def _component(state: dict[str, Any]) -> str:
    return ",".join(state.get("_actions", [])) or "UNKNOWN"


def _counts(progress_rows: list[dict[str, Any]]) -> dict[str, int]:
    return {label: sum(row.get("status") == label for row in progress_rows) for label in ("COMPLETE", "PARTIAL", "FAILED", "QUOTA_PAUSED", "DAILY_QUOTA_PAUSED")}


def _persist(
    rows: list[dict[str, Any]], *, usage_rows: list[dict[str, Any]], baseline: dict[str, Any],
    excluded: set[str], wait_state: dict[str, Any] | None = None, blocker: str = "",
) -> dict[str, Any]:
    dates = resume.runner.load_dates()
    progress_by_date = {str(row.get("date")): row for row in _read_csv(resume.RESUME_DIR / "build_progress.csv")}
    history_by_date = {str(row.get("date")): row for row in rows}
    progress_rows = []
    for day in dates:
        original = progress_by_date.get(day, {})
        row = history_by_date.get(day, {})
        status = original.get("status", row.get("status", "PENDING"))
        if row.get("status") == "DAILY_QUOTA_PAUSED":
            status = "DAILY_QUOTA_PAUSED"
        if day in excluded:
            status = "PARTIAL" if status not in {"COMPLETE", "FAILED"} else status
        progress_rows.append({
            "date": day, "status": status, "reason": original.get("reason", row.get("reason", "")),
            "component": row.get("component", ""), "attempts_same_component": row.get("attempts_same_component", 0),
            "excluded_after_repeated_failure": day in excluded,
            "next_eligible_at": (wait_state or {}).get("next_eligible_at", ""),
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        })
    _write_csv(AUTO_PROGRESS, progress_rows, ["date", "status", "reason", "component", "attempts_same_component", "excluded_after_repeated_failure", "next_eligible_at", "updated_at_utc"])
    _write_csv(AUTO_USAGE, usage_rows, ["date", "incremental_twelve_data_credits", "incremental_finnhub_requests", "incremental_official_requests", "incremental_tier2_requests", "fmp_requests", "runtime_seconds"])
    counts = _counts(progress_rows)
    ledger = TwelveDataRequestLedger(resume.REQUEST_LEDGER_PATH)
    current_credits = sum(ledger.totals().values())
    incremental_credits = max(0, current_credits - int(baseline.get("ledger_credits", 0)))
    summary = {
        "selected_dates": dates, "final_processed_date": max((row["date"] for row in progress_rows if row["status"] in {"COMPLETE", "PARTIAL", "FAILED"}), default=""),
        "started_at_utc": baseline.get("started_at_utc", ""),
        "baseline_complete_dates": baseline.get("baseline_complete_dates", []),
        "baseline_ledger_credits": int(baseline.get("ledger_credits", 0)),
        "_baseline_usage": baseline.get("baseline_usage", {}),
        "started_at_utc": baseline.get("started_at_utc", ""),
        "date_status_counts": counts, "excluded_repeated_failure_dates": sorted(excluded),
        "incremental_twelve_data_credits": incremental_credits,
        "legacy_credits_excluded": int(baseline.get("legacy_credits", 0)),
        "incremental_finnhub_requests": sum(int(row.get("incremental_finnhub_requests", 0) or 0) for row in usage_rows),
        "incremental_official_verification_requests": sum(int(row.get("incremental_official_requests", 0) or 0) for row in usage_rows),
        "incremental_tier2_requests": sum(int(row.get("incremental_tier2_requests", 0) or 0) for row in usage_rows),
        "fmp_requests": sum(int(row.get("fmp_requests", 0) or 0) for row in usage_rows),
        "quota_wait": wait_state, "remaining_blocker": blocker,
        "final_replay_id": None, "final_readiness": None,
    }
    _atomic_json_retry(AUTO_SUMMARY_JSON, summary)
    resume.runner.atomic_text(
        AUTO_SUMMARY_MD,
        "# Automatic Phase B Resume — In Progress\n\n"
        f"- Build counts: {counts}.\n"
        f"- Incremental Twelve Data credits: {incremental_credits}.\n"
        f"- Remaining blocker: {blocker or 'none'}.\n",
    )
    return summary


def _wait_until(eligible_at: str, seconds: float, *, reason: str) -> None:
    logger = logging.getLogger("auto_resume_phase_b")
    logger.info("AUTO_WAIT provider=Twelve Data reason=%s wait_seconds=%.1f next_eligible_at=%s", reason, seconds, eligible_at)
    remaining = seconds
    while remaining > 0:
        interval = min(60.0, remaining)
        time.sleep(interval)
        remaining -= interval


def _baseline() -> dict[str, Any]:
    ledger = TwelveDataRequestLedger(resume.REQUEST_LEDGER_PATH)
    legacy = sum(int(row.get("legacy_incremental_twelve_data_credits", row.get("incremental_twelve_data_credits", 0)) or 0) for row in resume._read_csv(resume.RESUME_DIR / "provider_usage_incremental.csv"))
    complete_dates = [day for day in resume.runner.load_dates() if not resume._artifact_states(day).get("_actions")]
    return {"ledger_credits": sum(ledger.totals().values()), "legacy_credits": legacy, "baseline_complete_dates": complete_dates}


def _usage_deltas(baseline_usage: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    current = {row["date"]: row for row in resume._read_csv(resume.RESUME_DIR / "provider_usage_incremental.csv")}
    result = []
    for day, row in current.items():
        old = baseline_usage.get(day, {})
        result.append({
            "date": day,
            "incremental_twelve_data_credits": max(0, int(row.get("ledger_twelve_data_credits", 0) or 0) - int(old.get("ledger_twelve_data_credits", 0) or 0)),
            "incremental_finnhub_requests": max(0, int(row.get("incremental_finnhub_requests", 0) or 0) - int(old.get("incremental_finnhub_requests", 0) or 0)),
            "incremental_official_requests": max(0, int(row.get("incremental_official_requests", 0) or 0) - int(old.get("incremental_official_requests", 0) or 0)),
            "incremental_tier2_requests": max(0, int(row.get("incremental_tier2_requests", 0) or 0) - int(old.get("incremental_tier2_requests", 0) or 0)),
            "fmp_requests": max(0, int(row.get("fmp_requests", 0) or 0) - int(old.get("fmp_requests", 0) or 0)),
            "runtime_seconds": row.get("runtime_seconds", 0),
        })
    return result


def run_supervisor(
    *, run_one: Callable[..., int] = _run_one,
    find_next: Callable[[set[str]], tuple[str, dict[str, Any]] | None] = _find_next_work,
    wait_until: Callable[..., None] = _wait_until,
    max_iterations: int | None = None,
) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    AUTO_DIR.mkdir(parents=True, exist_ok=True)
    dates = resume.runner.load_dates()
    if len(dates) != 30 or dates[-1] != "2026-09-28":
        raise RuntimeError("PHASE_B_PLAN_MISMATCH_EXPECTED_30_SESSIONS_THROUGH_2026-09-28")
    baseline_usage = {row["date"]: row for row in resume._read_csv(resume.RESUME_DIR / "provider_usage_incremental.csv")}
    baseline = _baseline()
    if AUTO_SUMMARY_JSON.is_file():
        try:
            prior_summary = json.loads(AUTO_SUMMARY_JSON.read_text(encoding="utf-8"))
            if prior_summary.get("selected_dates") == dates and not prior_summary.get("final_replay_id"):
                baseline = {
                    "ledger_credits": int(prior_summary.get("baseline_ledger_credits", baseline["ledger_credits"])),
                    "legacy_credits": int(prior_summary.get("legacy_credits_excluded", baseline["legacy_credits"])),
                    "baseline_complete_dates": prior_summary.get("baseline_complete_dates", baseline["baseline_complete_dates"]),
                    "started_at_utc": prior_summary.get("started_at_utc", ""),
                    "baseline_usage": prior_summary.get("_baseline_usage", baseline_usage),
                }
                baseline_usage = baseline["baseline_usage"]
        except (OSError, ValueError, TypeError):
            pass
    baseline.setdefault("started_at_utc", datetime.now(timezone.utc).isoformat())
    baseline["baseline_usage"] = baseline_usage
    attempts: dict[tuple[str, str], int] = {}
    history = [dict(row) for row in _read_csv(AUTO_PROGRESS)]
    for row in history:
        key = (str(row.get("date", "")), str(row.get("component", "")))
        try:
            attempts[key] = int(row.get("attempts_same_component", 0) or 0)
        except (ValueError, TypeError):
            attempts[key] = 0
    excluded = {str(row["date"]) for row in history if row.get("excluded_after_repeated_failure", "").lower() == "true"}
    blocker = ""
    wait_state: dict[str, Any] | None = None
    iterations = 0
    logger = logging.getLogger("auto_resume_phase_b")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler(sys.stdout))

    with prevent_system_sleep(logger=logger):
        while True:
            next_item = find_next(excluded)
            if next_item is None:
                # No actions remain; runner's no-work execution performs the final cache-only replay.
                code = run_one(sorted(excluded), final_replay=True)
                if code != 0:
                    blocker = f"FINAL_OFFLINE_REPLAY_EXIT_{code}"
                break
            market_date, state = next_item
            required = estimate_remaining_mover_credits(market_date, state)
            budget = _daily_budget_state(required)
            if required > budget["daily_quota"]:
                blocker = f"UNRECOVERABLE_DAILY_QUOTA_REQUIREMENT:{market_date}:{required}"
                history.append({"date": market_date, "status": "DAILY_QUOTA_PAUSED", "reason": blocker, "component": _component(state), "attempts_same_component": 0})
                _persist(history, usage_rows=_usage_deltas(baseline_usage), baseline=baseline, excluded=excluded, blocker=blocker)
                break
            if not budget["sufficient"]:
                wait_state = {"reason": "DAILY_QUOTA_PAUSED", "next_eligible_at": budget["next_daily_reset"], "wait_seconds": max(0.0, (datetime.fromisoformat(budget["next_daily_reset"]) - datetime.now(timezone.utc)).total_seconds()), "market_date": market_date, "required_credits": required, "daily_remaining": budget["daily_remaining"]}
                history.append({"date": market_date, "status": "DAILY_QUOTA_PAUSED", "reason": "DAILY_QUOTA_PAUSED", "component": _component(state), "attempts_same_component": 0})
                _persist(history, usage_rows=_usage_deltas(baseline_usage), baseline=baseline, excluded=excluded, wait_state=wait_state)
                wait_until(wait_state["next_eligible_at"], wait_state["wait_seconds"], reason="DAILY_QUOTA_PAUSED")
                wait_state = None
                continue

            component = _component(state)
            code = run_one(sorted(excluded), final_replay=False)
            if code not in {0, 2}:
                blocker = f"RESUME_RUNNER_EXIT_{code}"
                history.append({"date": market_date, "status": "FAILED", "reason": blocker, "component": component, "attempts_same_component": 1})
                _persist(history, usage_rows=_usage_deltas(baseline_usage), baseline=baseline, excluded=excluded, blocker=blocker)
                break
            after = resume._artifact_states(market_date)
            progress_row = next((row for row in resume._read_csv(resume.RESUME_DIR / "build_progress.csv") if row.get("date") == market_date), {})
            status = progress_row.get("status", "PARTIAL")
            if code == 2 or status == "QUOTA_PAUSED":
                next_at, seconds = _next_eligible(progress_row)
                if next_at is None or seconds is None:
                    blocker = f"QUOTA_PAUSE_WITHOUT_VALID_NEXT_ELIGIBLE:{market_date}"
                    history.append({"date": market_date, "status": "FAILED", "reason": blocker, "component": component, "attempts_same_component": 0})
                    _persist(history, usage_rows=_usage_deltas(baseline_usage), baseline=baseline, excluded=excluded, blocker=blocker)
                    break
                wait_state = {"reason": "QUOTA_PAUSED", "next_eligible_at": next_at, "wait_seconds": seconds, "market_date": market_date, "component": component}
                history.append({"date": market_date, "status": "QUOTA_PAUSED", "reason": "QUOTA_PAUSED", "component": component, "attempts_same_component": 0})
                _persist(history, usage_rows=_usage_deltas(baseline_usage), baseline=baseline, excluded=excluded, wait_state=wait_state)
                wait_until(next_at, seconds, reason="QUOTA_PAUSED")
                wait_state = None
                continue

            remaining_component = _component(after)
            if status == "PARTIAL" and after.get("_actions"):
                same = (market_date, component)
                attempts[same] = attempts.get(same, 0) + 1 if remaining_component == component else 0
                if attempts[same] >= MAX_IDENTICAL_FAILURES:
                    excluded.add(market_date)
                    logger.error("AUTO_RESUME_QUARANTINE date=%s component=%s attempts=%d", market_date, component, attempts[same])
            else:
                attempts[(market_date, component)] = 0
            history.append({
                "date": market_date, "status": status, "reason": progress_row.get("reason", ""),
                "component": component, "attempts_same_component": attempts.get((market_date, component), 0),
                "excluded_after_repeated_failure": market_date in excluded,
            })
            _persist(history, usage_rows=_usage_deltas(baseline_usage), baseline=baseline, excluded=excluded)
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                blocker = "TEST_ITERATION_LIMIT"
                break

    usage_rows = _usage_deltas(baseline_usage)
    summary = _persist(history, usage_rows=usage_rows, baseline=baseline, excluded=excluded, wait_state=wait_state, blocker=blocker)
    if not blocker:
        _finalize_outputs(summary)
    return summary


def _finalize_outputs(summary: dict[str, Any]) -> None:
    dates = resume.runner.load_dates()
    integrity_source = resume.RESUME_DIR / "integrity_report.csv"
    if integrity_source.is_file():
        resume.runner.atomic_copy_replace(integrity_source, AUTO_INTEGRITY)
    dataset_path = resume.RESUME_DIR / "dataset_summary.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8")) if dataset_path.is_file() else {}
    resume_summary_path = resume.RESUME_DIR / "resume_summary.json"
    try:
        resume_summary = json.loads(resume_summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        resume_summary = {}
    metrics = dataset.get("metrics", {})
    progress_rows = _read_csv(AUTO_PROGRESS)
    usage_rows = _read_csv(AUTO_USAGE)
    completed = sum(row.get("status") == "COMPLETE" for row in progress_rows)
    baseline_dates = set(summary.get("baseline_complete_dates", []))
    newly_completed = sum(row.get("status") == "COMPLETE" and row.get("date") not in baseline_dates for row in progress_rows)
    incremental_credits = int(summary.get("incremental_twelve_data_credits", 0))
    final = {
        **summary,
        "final_replay_id": resume_summary.get("replay_id"),
        "final_processed_date": dates[-1],
        "date_status_counts": _counts(progress_rows),
        "complete_dates": [row["date"] for row in progress_rows if row.get("status") == "COMPLETE"],
        "partial_dates": [row["date"] for row in progress_rows if row.get("status") == "PARTIAL"],
        "failed_dates": [row["date"] for row in progress_rows if row.get("status") == "FAILED"],
        "final_readiness": {
            "READY": sum(row.get("readiness") == "READY" for row in _read_csv(AUTO_INTEGRITY)),
            "PARTIAL": sum(row.get("readiness") == "PARTIAL" for row in _read_csv(AUTO_INTEGRITY)),
            "FAIL": sum(row.get("integrity") == "FAIL" for row in _read_csv(AUTO_INTEGRITY)),
        },
        "incremental_credits_per_completed_date_average": round(incremental_credits / max(1, newly_completed), 2),
        "offline_network_calls": int(metrics.get("offline_network_calls", 0) or 0),
        "stock_parity_mismatches": int(metrics.get("stock_parity_mismatches", 0) or 0),
        "cross_symbol_candidate_leaks": int(metrics.get("cross_symbol_candidate_leaks", 0) or 0),
        "future_event_violations": int(metrics.get("future_event_violations", 0) or 0),
        "market_review_rows": len(_read_csv(resume.RESUME_DIR / "market_day_review_queue.csv")),
        "stock_review_rows_abs_move_ge_4pct": int(metrics.get("stock_review_rows_abs_move_ge_4pct", 0) or 0),
        "new_unlabeled_rows": {
            "market": int(metrics.get("new_unlabeled_market_rows", 0) or 0),
            "stock": int(metrics.get("new_unlabeled_stock_rows", 0) or 0),
        },
        "original_10_day_regression": metrics.get("original_10_day_regression"),
        "output_paths": {
            "progress": str(AUTO_PROGRESS), "usage": str(AUTO_USAGE), "summary_json": str(AUTO_SUMMARY_JSON),
            "summary_markdown": str(AUTO_SUMMARY_MD), "integrity": str(AUTO_INTEGRITY),
            "final_replay": str(resume.RESUME_DIR / "replays" / str(resume_summary.get("replay_id", ""))),
            "market_review_queue": str(resume.RESUME_DIR / "market_day_review_queue.csv"),
            "stock_review_queue": str(resume.RESUME_DIR / "stock_review_queue.csv"),
            "diagnostic_event_sample": str(resume.RESUME_DIR / "diagnostic_event_sample.csv"),
        },
    }
    summary.update(final)
    _atomic_json_retry(AUTO_SUMMARY_JSON, final)
    lines = [
        "# Automatic Phase B Resume — Summary", "",
        f"- Selected dates: {len(dates)} ({dates[0]} through {dates[-1]}).",
        f"- Build counts: {final['date_status_counts']}.",
        f"- Offline readiness/integrity: {final['final_readiness']}.",
        f"- Incremental Twelve Data credits: {incremental_credits}; average per newly completed date: {final['incremental_credits_per_completed_date_average']}.",
        f"- Finnhub={final['incremental_finnhub_requests']}; official verification={final['incremental_official_verification_requests']}; Tier2={final['incremental_tier2_requests']}; FMP={final['fmp_requests']}.",
        f"- Offline calls={final['offline_network_calls']}; stock parity mismatches={final['stock_parity_mismatches']}; cross-symbol leaks={final['cross_symbol_candidate_leaks']}; future-event violations={final['future_event_violations']}.",
        f"- Market review rows={final['market_review_rows']}; stock abs(move) >=4% rows={final['stock_review_rows_abs_move_ge_4pct']}; new unlabeled={final['new_unlabeled_rows']}.",
        f"- Original 10-day regression: {final['original_10_day_regression']}.",
        f"- Remaining blocker: {final.get('remaining_blocker') or 'none'}.", "",
    ]
    resume.runner.atomic_text(AUTO_SUMMARY_MD, "\n".join(lines))


def main() -> int:
    try:
        summary = run_supervisor()
    except KeyboardInterrupt:
        print("AUTO_RESUME_INTERRUPTED checkpoint_preserved=true")
        return 130
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not summary.get("remaining_blocker") else 1


if __name__ == "__main__":
    raise SystemExit(main())
