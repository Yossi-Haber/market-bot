"""Write review, integrity, and aggregate artifacts for Extended Dataset V2."""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _json(path: Path, payload: Any) -> None:
    _atomic(path, json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")


def _csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _mean(days: list[dict[str, Any]], key: str) -> float | None:
    values = [float(day.get("market_candidate_metrics", {}).get(key)) for day in days if isinstance(day.get("market_candidate_metrics", {}).get(key), (int, float))]
    return sum(values) / len(values) if values else None


def _regression(days: list[dict[str, Any]], integrity_rows: list[dict[str, Any]]) -> dict[str, Any]:
    indexed = {day["market_date"]: day for day in days}
    expected = [
        ("2026-09-25", "HUM", "STOCK_SPECIFIC"), ("2026-09-28", "PANW", "STOCK_SPECIFIC"),
        ("2026-09-21", "QCOM", "SECTOR_EXPLAINED"), ("2026-09-21", "LRCX", "SECTOR_EXPLAINED"),
        ("2026-09-16", "COP", "SECTOR_EXPLAINED"), ("2026-09-16", "EOG", "SECTOR_EXPLAINED"),
        ("2026-09-17", "MU", "SECTOR_EXPLAINED"), ("2026-09-21", "META", "STOCK_SPECIFIC"),
        ("2026-09-28", "BA", "STOCK_SPECIFIC"),
    ]
    checks = []
    for market_date, symbol, expected_status in expected:
        day = indexed.get(market_date, {})
        explanation = next((row for row in day.get("stock_explanations", []) if row.get("symbol") == symbol), {})
        actual = explanation.get("overall_explanation_status")
        checks.append({"date": market_date, "symbol": symbol, "expected": expected_status, "actual": actual, "pass": actual == expected_status})
    sep22 = indexed.get("2026-09-22", {})
    integrity = next((row for row in integrity_rows if row.get("date") == "2026-09-22"), {})
    actual = f"{sep22.get('status')}+{integrity.get('integrity')}"
    checks.append({"date": "2026-09-22", "symbol": "DAY", "expected": "PARTIAL+FAIL", "actual": actual, "pass": actual == "PARTIAL+FAIL"})
    meta = next((row for row in indexed.get("2026-09-24", {}).get("stock_records", []) if row.get("symbol") == "META"), {})
    published = str(meta.get("selected_published_at") or "")[:10]
    no_future = not published or published <= "2026-09-24"
    checks.append({"date": "2026-09-24", "symbol": "META", "expected": "NO_FUTURE_ARTICLE", "actual": published or None, "pass": no_future})
    return {"checks": checks, "passed": sum(row["pass"] for row in checks), "failed": sum(not row["pass"] for row in checks), "all_pass": all(row["pass"] for row in checks)}


def generate(milestone: Path, replay_id: str, dates: list[str], days: list[dict[str, Any]], skipped: list[dict[str, Any]], progress: dict[str, dict[str, Any]], usage: dict[str, dict[str, Any]], replay_result: dict[str, Any]) -> None:
    import build_historical_evaluation_batch as batch
    import build_historical_market_cache as historical

    days = sorted(days, key=lambda item: item["market_date"])
    market_rows = batch._market_rows(days)
    all_stock_rows = batch._stock_rows(days)
    stock_rows = [row for row in all_stock_rows if abs(float(row.get("move_pct") or 0)) >= 4.0]
    labels_dir = ROOT / "output/evaluation/manual_qa_v1"
    batch._write_csv(milestone / "market_day_review_queue.csv", market_rows, list(market_rows[0]) if market_rows else ["date"], ("date",), preserve_path=labels_dir / "market_day_review_queue_manual_qa_v1.csv")
    batch._write_csv(milestone / "stock_review_queue.csv", stock_rows, list(stock_rows[0]) if stock_rows else ["date", "symbol"], ("date", "symbol"), preserve_path=labels_dir / "stock_review_queue_manual_qa_v1.csv")

    integrity_rows: list[dict[str, Any]] = []
    parity = leaks = future_violations = 0
    for day in days:
        integrity = day.get("integrity", {})
        audit = day.get("stock_consistency_audit", {})
        mismatch = int(audit.get("mismatch_count", 0) or 0)
        leak = int(audit.get("leak_count", 0) or 0)
        future_events = []
        for record in day.get("stock_records", []):
            if record.get("selected_event") and record.get("event_time_relation") == "FUTURE_EVENT":
                future_events.append(record)
            published_date = str(record.get("selected_published_at") or "")[:10]
            if published_date and published_date > day["market_date"]:
                future_events.append(record)
        parity += mismatch
        leaks += leak
        future_violations += len(future_events)
        integrity_rows.append({"date": day["market_date"], "readiness": day.get("status"), "integrity": integrity.get("integrity_status", "WARN"), "reasons": ";".join(integrity.get("integrity_reasons", [])), "future_event_violations": len(future_events), "stock_parity_mismatches": mismatch, "cross_symbol_candidate_leaks": leak, "offline_network_calls": int(day.get("metrics", {}).get("offline_network_attempts_blocked", 0) or 0), "mover_records": len(day.get("stock_records", []))})
    for item in skipped:
        integrity_rows.append({"date": item.get("market_date"), "readiness": "FAILED", "integrity": "FAIL", "reasons": ";".join(item.get("missing_components", [])) or item.get("status"), "future_event_violations": 0, "stock_parity_mismatches": 0, "cross_symbol_candidate_leaks": 0, "offline_network_calls": 0, "mover_records": 0})
    integrity_fields = ["date", "readiness", "integrity", "reasons", "future_event_violations", "stock_parity_mismatches", "cross_symbol_candidate_leaks", "offline_network_calls", "mover_records"]
    _csv(milestone / "integrity_report.csv", integrity_rows, integrity_fields)

    diagnostic_types = {"ANALYST_ACTION", "EARNINGS", "GUIDANCE", "M_AND_A", "CONTRACT", "PARTNERSHIP", "PRODUCT", "REGULATORY"}
    diagnostic_rows: list[dict[str, Any]] = []
    for day in days:
        explanations = {row.get("symbol"): row for row in day.get("stock_explanations", [])}
        for driver in day.get("stock_catalysts", []):
            symbol = str((driver.get("related_symbols") or [""])[0]).upper()
            for candidate in driver.get("candidate_stories", []):
                event_type = str(candidate.get("event_type") or candidate.get("category") or "").upper()
                if event_type not in diagnostic_types:
                    continue
                explanation = explanations.get(symbol, {})
                diagnostic_rows.append({"date": day["market_date"], "symbol": symbol, "move_pct": explanation.get("move_pct"), "event_type": event_type, "event": candidate.get("canonical_event_title") or candidate.get("title"), "published_at": candidate.get("published_at"), "target_role": candidate.get("target_role"), "subject_gate": candidate.get("subject_gate"), "causal_gate": candidate.get("causal_gate"), "verification_status": candidate.get("verification"), "explanation_status": explanation.get("overall_explanation_status"), "diagnostic_only": True})
    diagnostic_rows.sort(key=lambda row: (str(row["date"]), str(row["symbol"]), str(row["event_type"]), str(row["event"])))
    _csv(milestone / "diagnostic_event_sample.csv", diagnostic_rows, ["date", "symbol", "move_pct", "event_type", "event", "published_at", "target_role", "subject_gate", "causal_gate", "verification_status", "explanation_status", "diagnostic_only"])

    coverage_rows = []
    day_by_date = {day["market_date"]: day for day in days}
    for date_value in dates:
        day = day_by_date.get(date_value)
        if day:
            coverage = day.get("cache_coverage", {})
            history_snapshot = historical._load(historical._history_path(date_value) / "market_driver_snapshot.json")
            states = {key: coverage.get(key, "MISSING") for key in ("market_data", "movers", "finnhub", "marketaux", "official", "tier2", "verification")}
            driver_state = "AVAILABLE" if history_snapshot else "MISSING"
        else:
            # Keep coverage visible for dates the offline evaluator could not
            # replay; do not omit them from the 30-date milestone artifact.
            build_state = progress.get(date_value, {})
            movers_state = build_state.get("movers", "MISSING")
            states = {
                "market_data": build_state.get("market_data", "MISSING"),
                "movers": movers_state,
                "finnhub": "DEFERRED" if movers_state != "HIT" else "MISSING",
                "marketaux": "DEFERRED",
                "official": "DEFERRED",
                "tier2": "DEFERRED",
                "verification": "DEFERRED",
            }
            history_snapshot = historical._load(historical._history_path(date_value) / "market_driver_snapshot.json")
            driver_state = "AVAILABLE" if history_snapshot else "MISSING"
        coverage_rows.append({"date": date_value, **states, "market_driver_snapshot": driver_state})
    _csv(milestone / "cache_coverage.csv", coverage_rows, ["date", "market_data", "movers", "finnhub", "marketaux", "official", "tier2", "verification", "market_driver_snapshot"])

    readiness = {key: sum(row.get("readiness") == key for row in integrity_rows) for key in ("READY", "PARTIAL", "FAILED")}
    integrity_counts = {key: sum(row.get("integrity") == key for row in integrity_rows) for key in ("PASS", "WARN", "FAIL")}
    statuses = [row.get("overall_explanation_status") for row in all_stock_rows]
    total_stocks = max(1, len(all_stock_rows))
    offline_calls = sum(int(row.get("offline_network_calls", 0) or 0) for row in integrity_rows)
    totals = {key: sum(int(row.get(key, 0) or 0) for row in usage.values()) for key in ("fmp_requests", "twelve_data_total_credits", "finnhub_requests", "finnhub_symbols_newly_cached", "marketaux_requests", "google_news_searches", "gdelt_searches", "official_source_requests", "tier2_verification_requests", "cache_hits", "cache_builds", "cache_failures")}
    metrics = {
        "days_total": len(dates), "days_replayed": len(days), "days_skipped": len(skipped),
        "days_ready": readiness["READY"], "days_partial": readiness["PARTIAL"], "days_failed": readiness["FAILED"],
        "integrity_pass_days": integrity_counts["PASS"], "integrity_warn_days": integrity_counts["WARN"], "integrity_fail_days": integrity_counts["FAIL"],
        "market_driver_coverage": sum(bool(day.get("main_market_driver")) for day in days) / max(1, len(days)),
        "sector_driver_coverage": sum(bool(day.get("sector_driver_analysis")) for day in days) / max(1, len(days)),
        "stock_specific_rate": statuses.count("STOCK_SPECIFIC") / total_stocks,
        "market_explained_rate": statuses.count("MARKET_EXPLAINED") / total_stocks,
        "sector_explained_rate": statuses.count("SECTOR_EXPLAINED") / total_stocks,
        "fully_unexplained_rate": statuses.count("UNEXPLAINED") / total_stocks,
        "avg_market_candidates_raw": _mean(days, "raw_candidate_count"),
        "avg_market_candidates_filtered": _mean(days, "filtered_candidate_count"),
        "avg_market_candidates_eligible": _mean(days, "eligible_candidate_count"),
        "avg_market_candidates_selected": _mean(days, "selected_candidate_count"),
        "market_review_rows": len(market_rows), "stock_rows_all": len(all_stock_rows),
        "stock_review_rows_abs_move_ge_4pct": len(stock_rows), "diagnostic_event_sample_rows": len(diagnostic_rows),
        "new_unlabeled_market_rows": sum(not row.get("market_manual_label") for row in market_rows),
        "new_unlabeled_stock_rows": sum(not row.get("manual_label") for row in stock_rows),
        "stock_parity_mismatches": parity, "cross_symbol_candidate_leaks": leaks,
        "future_event_violations": future_violations, "offline_network_calls": offline_calls,
        "replay_id": replay_id, "provider_usage_totals": totals,
    }
    regression = _regression(days, integrity_rows)
    metrics["original_10_day_regression"] = regression
    summary = {"dataset": "Extended Historical Evaluation Dataset V2", "date_range": {"from": dates[0], "to": dates[-1]}, "selected_dates": dates, "metrics": metrics, "readiness_counts": readiness, "integrity_counts": integrity_counts, "skipped_dates": skipped, "regression_checks": regression, "replay_directory": f"replays/{replay_id}/evaluation", "provider_build_status": {date_value: progress.get(date_value, {}).get("status", "NOT_ATTEMPTED") for date_value in dates}}
    _json(milestone / "dataset_summary.json", summary)
    lines = ["# Extended Historical Evaluation Dataset V2 — 30-Day Milestone", "", f"Window: **{dates[0]} to {dates[-1]}** ({len(dates)} selected trading sessions).", "", "## Readiness and integrity", "", f"- Readiness: READY={readiness['READY']}, PARTIAL={readiness['PARTIAL']}, FAILED={readiness['FAILED']}.", f"- Integrity: PASS={integrity_counts['PASS']}, WARN={integrity_counts['WARN']}, FAIL={integrity_counts['FAIL']}.", f"- Offline network calls: {offline_calls}; parity mismatches: {parity}; cross-symbol leaks: {leaks}; future-event violations: {future_violations}.", "", "## QA coverage", "", f"- Market-day review rows: {len(market_rows)}.", f"- Stock review rows (absolute move at least 4%): {len(stock_rows)}.", f"- New unlabeled stock rows: {metrics['new_unlabeled_stock_rows']}.", f"- Diagnostic event sample rows: {len(diagnostic_rows)}.", f"- FMP requests: {totals['fmp_requests']}.", "", "## Original 10-day regression", "", f"- Checks passed: {regression['passed']}/{len(regression['checks'])}.", "", "| Date | Symbol | Expected | Actual | Result |", "|---|---|---|---|---|"]
    lines.extend(f"| {row['date']} | {row['symbol']} | {row['expected']} | {row['actual']} | {'PASS' if row['pass'] else 'FAIL'} |" for row in regression["checks"])
    lines.extend(["", "Per-date build/checkpoint and provider-usage telemetry are in build_progress.csv and provider_usage.csv. Cache coverage and date-level integrity are recorded separately.", ""])
    _atomic(milestone / "dataset_summary.md", "\n".join(lines))
