"""Resumable historical Market Driver evaluation batch workflow."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from drivers.causal import publication_time_relation

import build_historical_market_cache as historical
import evaluate_market_drivers as replay
from evaluate_market_drivers_product import audit_stock_consistency, evaluate_product
from news.providers.finnhub import FinnhubProvider
from utils.windows_power import prevent_system_sleep

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
BATCH_DIR = OUTPUT_DIR / "evaluation_batch"


def select_dates(*, market_dates: str | None = None, from_date: str | None = None, to_date: str | None = None) -> list[str]:
    if market_dates:
        values = [item.strip() for item in market_dates.split(",") if item.strip()]
    elif from_date and to_date:
        start, end = date.fromisoformat(from_date), date.fromisoformat(to_date)
        if end < start:
            raise ValueError("DATE_RANGE_REVERSED")
        values = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                values.append(current.isoformat())
            current += timedelta(days=1)
    else:
        raise ValueError("DATE_ARGUMENT_REQUIRED")
    result: list[str] = []
    for value in values:
        normalized = date.fromisoformat(value).isoformat()
        if normalized not in result and date.fromisoformat(normalized).weekday() < 5:
            result.append(normalized)
    return result


def _stock_news_state(coverage: dict[str, Any]) -> str:
    if coverage.get("finnhub") == "AVAILABLE":
        return "HIT"
    if any(coverage.get(key) == "AVAILABLE" for key in ("marketaux", "official", "tier2")):
        return "PARTIAL"
    return "MISS"


def _state_from_finnhub(value: str) -> str:
    """Extract the dependency state from the human-readable coverage field."""
    return value.rsplit(" ", 1)[-1] if value and " " in value else value


def inspect_components(market_date: str) -> dict[str, Any]:
    inspection = historical.inspect_date(market_date)
    coverage = replay._cache_coverage(market_date, OUTPUT_DIR)
    movers_available = inspection.get("movers") == "HIT"
    stock_news = _stock_news_state(coverage) if movers_available else "DEFERRED"
    actions = inspection.get("actions_needed", "none")
    return {
        "date": market_date,
        "trading_day_status": inspection.get("trading_day_status", "TRADING_DAY"),
        "market_data": inspection.get("market", "MISS"),
        "movers": inspection.get("movers", "MISS"),
        "market_news": inspection.get("market_news", "MISS"),
        "stock_news": stock_news,
        "verification": inspection.get("verification", "DEFERRED"),
        "coverage": coverage,
        "actions_needed": actions,
        "deferred": inspection.get("deferred", "none"),
        "plan_ready": bool(inspection.get("plan_ready")),
        "expected_network_work": actions != "none" and inspection.get("trading_day_status") == "TRADING_DAY",
    }


def evaluate_integrity(day: dict[str, Any], output_dir: Path = OUTPUT_DIR) -> dict[str, Any]:
    """Canonical point-in-time and artifact integrity decision for one date."""
    market_date = str(day["market_date"])
    reasons: set[str] = set()
    severity = "PASS"

    def fail(reason: str) -> None:
        nonlocal severity
        severity = "FAIL"
        reasons.add(reason)

    def warn(reason: str) -> None:
        nonlocal severity
        if severity != "FAIL":
            severity = "WARN"
        reasons.add(reason)

    market = replay._dated_snapshot(output_dir, "market_snapshot", market_date)
    movers = replay._dated_snapshot(output_dir, "movers_snapshot", market_date)
    for label, snapshot in (("MARKET", market), ("MOVERS", movers)):
        if not snapshot:
            fail(f"{label}_SNAPSHOT_MISSING")
        elif snapshot.get("market_date") != market_date:
            fail(f"{label}_DATE_MISMATCH")
    if market:
        proof = market.get("historical_integrity")
        if not isinstance(proof, dict):
            warn("MARKET_INTEGRITY_METADATA_MISSING")
        else:
            if proof.get("future_data_leak_detected") is True:
                fail("FUTURE_MARKET_DATA_LEAK")
            for item in proof.get("exact_price_series", []):
                if isinstance(item, dict) and (item.get("selected_source_date") or "") > market_date:
                    fail("FUTURE_MARKET_DATA_LEAK")
            if proof.get("requested_market_date") not in (None, market_date):
                fail("MARKET_INTEGRITY_DATE_MISMATCH")
        core = market.get("market", {}).get("core", {})
        for symbol in ("SPY", "QQQ", "DIA", "IWM"):
            item = core.get(symbol) if isinstance(core, dict) else None
            try:
                close = float(item.get("close")) if isinstance(item, dict) else 0.0
            except (TypeError, ValueError):
                close = 0.0
            if not math.isfinite(close) or close <= 0:
                fail("INVALID_POINT_IN_TIME_MARKET_DATA")
            source_date = str((item or {}).get("latest_bar_datetime") or "")[:10] if isinstance(item, dict) else ""
            if source_date and source_date > market_date:
                fail("FUTURE_MARKET_DATA_LEAK")
    if movers:
        proof = movers.get("historical_integrity")
        if not isinstance(proof, dict):
            warn("MOVERS_INTEGRITY_METADATA_MISSING")
        else:
            if proof.get("integrity_status") != "PASS" or proof.get("future_data_leak_detected") is True or (proof.get("max_source_date") or "") > market_date:
                fail("INVALID_REQUIRED_MOVER_ARTIFACT")
        if movers.get("collection_status") != "SUCCESS":
            fail("INVALID_REQUIRED_MOVER_ARTIFACT")
        requested = movers.get("symbols_requested")
        valid = movers.get("symbols_valid")
        if isinstance(requested, int) and isinstance(valid, int) and (valid < requested or valid < min(requested, 110)):
            fail("INVALID_REQUIRED_MOVER_ARTIFACT")
    discovery = day.get("market_discovery_cache_state")
    if discovery in {"INVALID_SCHEMA", "DATE_MISMATCH", "INTEGRITY_FAIL"}:
        fail(f"MARKET_DISCOVERY_{discovery}")
    if discovery in {None, "MISS"}:
        warn("MARKET_DISCOVERY_CACHE_MISSING")
    for record in day.get("stock_records", []):
        selected_event = record.get("selected_event")
        relation = publication_time_relation(record.get("selected_published_at"), market_date)
        if selected_event and relation == "FUTURE_EVENT":
            fail("FUTURE_EVENT_SELECTED")
        if selected_event and relation == "BEFORE_OR_DURING_SESSION" and record.get("event_time_relation") == "FUTURE_EVENT":
            fail("INVALID_SAME_DAY_TIMING_CLASSIFICATION")
        if selected_event:
            affected = {str(item).upper() for item in record.get("selected_affected_symbols", [])}
            if affected and record.get("symbol") not in affected:
                fail("CROSS_SYMBOL_EVIDENCE_SELECTED")
            if record.get("event_time_relation") == "FUTURE_EVENT":
                fail("FUTURE_EVENT_SELECTED")
    verification = day.get("news_context", {}).get("verification", {})
    if int(verification.get("offline_network_attempts_blocked", 0) or 0) > 0:
        fail("OFFLINE_NETWORK_CALL")
    timing = day.get("news_context", {}).get("timing_diagnostics", {})
    if any(item.get("invalid_time_article_count", 0) for item in timing.values() if isinstance(item, dict)):
        warn("UNKNOWN_ARTICLE_TIME_METADATA")
    if any(item.get("status") in {"MISSING", "PARSE_ERROR"} for item in day.get("news_context", {}).get("finnhub_cache", {}).values()):
        warn("FINNHUB_COVERAGE_GAP")
    if any(item.get("status") in {"INVALID_SCHEMA", "INVALID_WINDOW"} for item in day.get("news_context", {}).get("finnhub_cache", {}).values()):
        fail("REQUIRED_CACHE_SCHEMA_CORRUPTION")
    stock_audit = day.get("stock_consistency_audit", {})
    if stock_audit.get("mismatch_count", 0):
        fail("STOCK_RESULT_PARITY_MISMATCH")
    if stock_audit.get("leak_count", 0):
        fail("CROSS_SYMBOL_CANDIDATE_LEAK")
    return {"integrity_status": severity, "integrity_reasons": sorted(reasons)}


def apply_integrity_results(days: list[dict[str, Any]], components: list[dict[str, Any]], output_dir: Path = OUTPUT_DIR) -> None:
    """Attach the canonical integrity decision and enforce readiness parity."""
    by_date = {day["market_date"]: day for day in days}
    for component in components:
        day = by_date.get(component["date"])
        if day is None:
            continue
        integrity = evaluate_integrity(day, output_dir)
        day["integrity"] = integrity
        component["integrity"] = integrity
        component["integrity_status"] = integrity["integrity_status"]
        component["integrity_reasons"] = integrity["integrity_reasons"]
        if integrity["integrity_status"] == "FAIL" and component.get("status") == "READY":
            component["status"] = "PARTIAL"
            day["integrity_downgraded_readiness"] = True
        else:
            day["integrity_downgraded_readiness"] = False
        day["readiness"] = component.get("status")
        day["status"] = component.get("status")


def _integrity_ok(market_date: str) -> bool:
    """Compatibility wrapper for older callers; strict PASS only."""
    return evaluate_integrity({"market_date": market_date})["integrity_status"] == "PASS"


def _priority_market(day: dict[str, Any]) -> str:
    main = day.get("main_market_driver") or {}
    moves = [abs(float(day.get("market_values", {}).get(symbol, {}).get("change_pct") or 0)) for symbol in ("SPY", "QQQ", "DIA", "IWM")]
    if main.get("verification_status") in {"CONFIRMED", "PLAUSIBLE_NOT_VERIFIED"} or max(moves or [0]) >= 1:
        return "HIGH"
    if day.get("sector_drivers"):
        return "MEDIUM"
    return "LOW"


def _priority_stock(row: dict[str, Any]) -> str:
    move = abs(float(row.get("move_pct") or 0))
    if move >= 5 or row.get("inheritance_rejection_reason"):
        return "HIGH"
    if move >= 4:
        return "MEDIUM"
    return "LOW"


def _read_existing_csv(path: Path, keys: tuple[str, ...]) -> dict[tuple[str, ...], dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return {tuple(row.get(key, "") for key in keys): row for row in csv.DictReader(handle)}


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str], keys: tuple[str, ...], *, preserve_path: Path | None = None) -> None:
    existing = _read_existing_csv(path, keys)
    if not existing and preserve_path is not None and preserve_path != path:
        existing = _read_existing_csv(preserve_path, keys)
    label_field = "market_manual_label" if "market_manual_label" in fields else "manual_label"
    merged: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(str(row.get(item, "")) for item in keys)
        previous = existing.get(key, {})
        label = previous.get(label_field, previous.get("manual_label", row.get(label_field, "")))
        integrity_failed = row.get("integrity_status") == "FAIL"
        merged[key] = {**row, label_field: label, "review_notes": previous.get("review_notes", row.get("review_notes", "")), "manual_label_eligible": not integrity_failed, "manual_label_stale": bool(integrity_failed and label)}
    # Keep prior review rows that are outside the current generated scope; a
    # rerun must never erase a human label or note merely because a date was
    # skipped, failed, or produced no qualifying review item this time.
    for key, previous in existing.items():
        if key not in merged:
            merged[key] = previous
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields} for row in merged.values())


def _market_rows(days: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for day in days:
        main = day.get("main_market_driver") or {}
        values = day.get("market_values", {})
        analysis = day.get("market_driver_analysis", {})
        breadth = analysis.get("breadth", {})
        snapshot = historical._load(historical._snapshot_path("market_snapshot", day["market_date"])) or {}
        macro = ((snapshot.get("market") or {}).get("macro") or {}) if isinstance(snapshot, dict) else {}
        def macro_change(symbol: str, *aliases: str) -> Any:
            for key in (symbol, *aliases):
                item = macro.get(key, {}) if isinstance(macro, dict) else {}
                if isinstance(item, dict):
                    for field in ("change_pct", "change_bps", "change_bp", "change"):
                        if item.get(field) is not None:
                            return item[field]
            return None
        integrity = day.get("integrity", {"integrity_status": "WARN", "integrity_reasons": ["INTEGRITY_NOT_EVALUATED"]})
        counts = day.get("market_candidate_metrics", {})
        rows.append({
            "date": day["market_date"], "readiness": day.get("status"), "market_regimes": ",".join(analysis.get("market_regime", [])), "primary_market_driver": main.get("driver_id"), "market_driver_status": main.get("verification_status"), "selected_market_story": (main.get("candidate_stories") or [{}])[0].get("title"), "selected_market_story_source": (main.get("candidate_stories") or [{}])[0].get("source_name"), "eligible_market_candidates": counts.get("eligible_candidate_count"), "market_candidates_raw": counts.get("raw_candidate_count"), "market_candidates_filtered": counts.get("filtered_candidate_count"), "market_candidates_selected": counts.get("selected_candidate_count"), "market_candidate_metric_state": json.dumps(counts.get("metric_state", {}), sort_keys=True), "breadth_up": breadth.get("up"), "breadth_down": breadth.get("down"), "SPY_change_pct": values.get("SPY", {}).get("change_pct"), "QQQ_change_pct": values.get("QQQ", {}).get("change_pct"), "DIA_change_pct": values.get("DIA", {}).get("change_pct"), "IWM_change_pct": values.get("IWM", {}).get("change_pct"), "US10Y_change_bp": macro_change("US10Y", "DGS10"), "VIX_change_pct": macro_change("VIX", "VIXCLS"), "DXY_change_pct": macro_change("DXY", "DX-Y.NYB"), "WTI_change_pct": macro_change("WTI", "CL=F"), "XAU_change_pct": macro_change("XAU", "GOLD", "XAU/USD"), "market_manual_label": "", "manual_label_eligible": integrity.get("integrity_status") != "FAIL", "manual_label_stale": False, "review_notes": "", "priority": _priority_market(day), "integrity_status": integrity.get("integrity_status"), "integrity_reasons": ";".join(integrity.get("integrity_reasons", [])), "market_discovery_cache_state": day.get("market_discovery_cache_state", "MISS"),
        })
    return rows


def _stock_rows(days: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    explanations = {(day["market_date"], item.get("symbol")): item for day in days for item in day.get("stock_explanations", [])}
    for day in days:
        for record in day.get("stock_records", []):
            explanation = explanations.get((day["market_date"], record.get("symbol")), {})
            integrity = day.get("integrity", {})
            rows.append({"date": day["market_date"], "symbol": record.get("symbol"), "readiness": day.get("status"), "integrity_status": integrity.get("integrity_status"), "integrity_reasons": ";".join(integrity.get("integrity_reasons", [])), "move_pct": record.get("move_pct"), "stock_specific_status": record.get("stock_specific_catalyst_status"), "stock_specific_catalyst_status": record.get("stock_specific_catalyst_status"), "overall_explanation_status": record.get("overall_explanation_status"), "selected_event": record.get("selected_event"), "selected_published_at": record.get("selected_published_at"), "event_time_relation": record.get("event_time_relation"), "causal_gate": record.get("causal_gate"), "causal_reason": record.get("causal_reason"), "verification_status": record.get("verification_status"), "candidate_count": record.get("candidate_count"), "future_article_count": record.get("future_article_count"), "inherited_sector_driver": (record.get("inherited_sector_driver") or {}).get("driver_id") if isinstance(record.get("inherited_sector_driver"), dict) else record.get("inherited_sector_driver"), "inherited_market_driver": (record.get("inherited_market_driver") or {}).get("driver_id") if isinstance(record.get("inherited_market_driver"), dict) else record.get("inherited_market_driver"), "manual_label": "", "manual_label_eligible": integrity.get("integrity_status") != "FAIL", "manual_label_stale": False, "review_notes": "", "priority": _priority_stock({**record, **explanation})})
    return rows


def _aggregate(days: list[dict[str, Any]], components: list[dict[str, Any]]) -> dict[str, Any]:
    main = [day.get("main_market_driver") or {} for day in days]
    explanations = [item for day in days for item in day.get("stock_explanations", [])]
    count = len(explanations)
    statuses = [str(item.get("overall_explanation_status") or "") for item in explanations]
    metric_names = {"raw": "raw_candidate_count", "filtered": "filtered_candidate_count", "eligible": "eligible_candidate_count", "selected": "selected_candidate_count"}
    metric_values: dict[str, list[int]] = {key: [] for key in metric_names}
    for day in days:
        values = day.get("market_candidate_metrics", {})
        for key, field in metric_names.items():
            value = values.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                metric_values[key].append(value)
    integrity_counts = {status: sum(day.get("integrity", {}).get("integrity_status") == status for day in days) for status in ("PASS", "WARN", "FAIL")}
    metrics = {"days_total": len(components), "days_ready": sum(item.get("status") == "READY" for item in components), "days_evaluated": len(days), "days_partial": sum(item.get("status") == "PARTIAL" for item in components), "days_failed": sum(item.get("status") == "FAILED" for item in components), "integrity_pass_days": integrity_counts["PASS"], "integrity_warn_days": integrity_counts["WARN"], "integrity_fail_days": integrity_counts["FAIL"], "market_driver_confirmed_days": sum(item.get("verification_status") == "CONFIRMED" for item in main), "market_driver_plausible_days": sum(item.get("verification_status") == "PLAUSIBLE_NOT_VERIFIED" for item in main), "market_driver_missing_days": sum(item.get("verification_status") in {None, "NO_MARKET_DRIVER_FOUND", "NO_CATALYST_FOUND"} for item in main), "market_driver_coverage_rate": sum(item.get("verification_status") in {"CONFIRMED", "PLAUSIBLE_NOT_VERIFIED"} for item in main) / len(days) if days else 0.0, "market_discovery_hit_rate": sum(item.get("market_news") == "HIT" for item in components) / len(components) if components else 0.0, **{f"avg_market_candidates_{key}": sum(values) / len(values) if values else None for key, values in metric_values.items()}, **{f"market_candidates_{key}_known_days": len(values) for key, values in metric_values.items()}, "market_explanation_coverage_rate": sum(bool(day.get("main_market_driver")) for day in days) / len(days) if days else 0.0, "sector_explanation_coverage_rate": sum(bool(day.get("sector_driver_analysis")) for day in days) / len(days) if days else 0.0, "stock_specific_explanation_rate": statuses.count("STOCK_SPECIFIC") / count if count else 0.0, "broad_market_explanation_rate": statuses.count("MARKET_EXPLAINED") / count if count else 0.0, "multi_factor_explanation_rate": statuses.count("MULTI_FACTOR") / count if count else 0.0, "fully_unexplained_rate": statuses.count("UNEXPLAINED") / count if count else 0.0}
    metrics["diversity"] = {"market_driver_statuses": {status: sum(item.get("verification_status") == status for item in main) for status in ("CONFIRMED", "PLAUSIBLE_NOT_VERIFIED", "NO_MARKET_DRIVER_FOUND")}, "stock_explanation_statuses": {status: statuses.count(status) for status in sorted(set(statuses)) if status}}
    return metrics


def _offline_evaluate_dates(dates: list[str], *, logger: logging.Logger) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Evaluate each locally available date independently, without building."""
    days: list[dict[str, Any]] = []
    failures: dict[str, str] = {}
    for market_date in dates:
        if replay._dated_snapshot(OUTPUT_DIR, "market_snapshot", market_date) is None or replay._dated_snapshot(OUTPUT_DIR, "movers_snapshot", market_date) is None:
            failures[market_date] = "MISSING_MARKET_OR_MOVERS_SNAPSHOT"
            continue
        try:
            result = evaluate_product(market_date, market_date, market_dates=[market_date], output_dir=OUTPUT_DIR)
            days.extend(result.get("days", []))
            if not result.get("days"):
                failures[market_date] = "NO_EVALUATION_DAY"
        except Exception as exc:
            logger.error("Offline evaluation failed for %s: %s", market_date, type(exc).__name__)
            failures[market_date] = type(exc).__name__
    return days, failures


def build_missing_components(dates: list[str], *, logger: logging.Logger | None = None) -> dict[str, Any]:
    """Build only absent market-discovery and mover-required stock caches.

    This deliberately bypasses the market/mover collector.  It uses the
    existing dated snapshots as inputs and the existing discovery,
    verification, and Finnhub pipeline, so valid caches remain untouched.
    """
    logger = logger or logging.getLogger("market_bot.evaluation_batch")
    results: list[dict[str, Any]] = []
    for market_date in dates:
        inspection = inspect_components(market_date)
        result: dict[str, Any] = {"market_date": market_date, "market_news": "HIT" if inspection.get("market_news") == "HIT" else "SKIPPED", "stock_news": "HIT" if inspection.get("stock_news") == "HIT" else "SKIPPED"}
        if inspection.get("market_news") != "HIT":
            market_news = historical._build_market_news(market_date, refresh=True, logger=logger)
            result["market_news"] = market_news.get("status", "FAILED")
            result["market_news_cache"] = market_news.get("cache_path")
        market_path = historical._snapshot_path("market_snapshot", market_date)
        movers_path = historical._snapshot_path("movers_snapshot", market_date)
        market = historical._load(market_path) if market_path else None
        movers = historical._load(movers_path) if movers_path else None
        if result["stock_news"] != "HIT" and market and movers:
            history_dir = historical._history_path(market_date)
            history_dir.mkdir(parents=True, exist_ok=True)
            for name, payload in (("market_snapshot.json", market), ("movers_snapshot.json", movers)):
                target = history_dir / name
                if not target.exists():
                    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            try:
                symbols = historical._mover_symbols(movers)
                provider = FinnhubProvider(token=os.getenv("FINNHUB_API_KEY", ""), refresh_cache=False)
                end_date = (date.fromisoformat(market_date) + timedelta(days=1)).isoformat()
                for symbol in symbols:
                    provider.search_company(symbol, market_date, end_date)
                stock_hits, stock_total = historical._finnhub_cache_status(market_date, symbols)
                result["stock_news"] = "BUILT" if stock_hits == stock_total else ("PARTIAL" if stock_hits else "FAILED")
                result["stock_symbols_requested"] = len(symbols)
                result["stock_symbols_cached"] = stock_hits
                if stock_hits:
                    driver_path = historical.run_market_driver_pipeline(market_date, history_dir, refresh_discovery=False, refresh_verification=False, logger=logger)
                    result["market_driver_snapshot"] = str(driver_path)
            except Exception as exc:
                logger.exception("Missing stock-news build failed for %s", market_date)
                result["stock_news"] = "FAILED"
                result["stock_news_error"] = type(exc).__name__
        results.append(result)
    return {"dates": results, "network_work": True}


def _run_batch_impl(dates: list[str], *, plan: bool = False, resume: bool = False, refresh_market_news: bool = False, offline_only: bool = False, overwrite_main_evaluation: bool = False, logger: logging.Logger | None = None) -> dict[str, Any]:
    logger = logger or logging.getLogger("market_bot.evaluation_batch")
    components: list[dict[str, Any]] = []
    for market_date in dates:
        state = inspect_components(market_date)
        if plan:
            trading_status = state.get("trading_day_status", "TRADING_DAY")
            state["status"] = trading_status if trading_status != "TRADING_DAY" else "PLAN"
            components.append(state)
            continue
        if state.get("trading_day_status", "TRADING_DAY") != "TRADING_DAY":
            state["status"] = state.get("trading_day_status", "INCOMPLETE_TRADING_DAY")
            components.append(state)
            continue
        if offline_only:
            state["status"] = "PENDING_OFFLINE_EVALUATION"
            components.append(state)
            continue
        if resume and not state["expected_network_work"] and not refresh_market_news:
            state["status"] = "SKIPPED"
            components.append(state)
            continue
        try:
            result = historical.build([market_date], refresh_market_news=refresh_market_news, logger=logger)
            entry = result["dates"][0]
            refreshed = inspect_components(market_date)
            state.update(refreshed)
            state.update({"status": entry.get("status") if entry.get("status") in {"INCOMPLETE_TRADING_DAY", "NON_TRADING_DAY", "FAILED"} else ("READY" if refreshed.get("plan_ready") else "PARTIAL"), "build_result": entry})
        except Exception as exc:
            state.update({"status": "FAILED", "failure_component": "historical_builder", "failure_reason": type(exc).__name__})
        components.append(state)
    ready_dates = [item["date"] for item in components if item.get("status") == "READY" and _integrity_ok(item["date"])]
    days: list[dict[str, Any]] = []
    offline_failures: dict[str, str] = {}
    if not plan and offline_only:
        days, offline_failures = _offline_evaluate_dates(dates, logger=logger)
        for item in components:
            if item["date"] in offline_failures:
                item["status"] = "FAILED"
                item["failure_component"] = "offline_evaluator"
                item["failure_reason"] = offline_failures[item["date"]]
            elif any(day.get("market_date") == item["date"] for day in days):
                item["status"] = "READY" if any(day.get("market_date") == item["date"] and day.get("status") == "READY" for day in days) else "PARTIAL"
            elif item.get("status") == "PENDING_OFFLINE_EVALUATION":
                item["status"] = "FAILED"
                item["failure_component"] = "offline_evaluator"
                item["failure_reason"] = "NO_EVALUATION_DAY"
    elif not plan and ready_dates:
        evaluated = evaluate_product(ready_dates[0], ready_dates[-1], market_dates=ready_dates, output_dir=OUTPUT_DIR)
        days = evaluated.get("days", [])
    apply_integrity_results(days, components)
    provisional_stock_rows = _stock_rows(days)
    component_by_date = {item["date"]: item for item in components}
    for day in days:
        row_subset = [row for row in provisional_stock_rows if row.get("date") == day["market_date"]]
        queue_audit = audit_stock_consistency(day, row_subset)
        previous = day.get("stock_consistency_audit", {})
        combined = {
            "stock_result_parity_mismatches": sorted(set(previous.get("stock_result_parity_mismatches", []) + queue_audit["stock_result_parity_mismatches"])),
            "cross_symbol_candidate_leaks": sorted(set(previous.get("cross_symbol_candidate_leaks", []) + queue_audit["cross_symbol_candidate_leaks"])),
        }
        combined.update({"mismatch_count": len(combined["stock_result_parity_mismatches"]), "leak_count": len(combined["cross_symbol_candidate_leaks"])})
        day["stock_consistency_audit"] = combined
        if combined["mismatch_count"] or combined["leak_count"]:
            reasons = set(day.get("integrity", {}).get("integrity_reasons", []))
            reasons.update(("STOCK_RESULT_PARITY_MISMATCH",) if combined["mismatch_count"] else ())
            reasons.update(("CROSS_SYMBOL_CANDIDATE_LEAK",) if combined["leak_count"] else ())
            day["integrity"] = {"integrity_status": "FAIL", "integrity_reasons": sorted(reasons)}
            component = component_by_date[day["market_date"]]
            component["integrity"] = day["integrity"]
            component["integrity_status"] = "FAIL"
            component["integrity_reasons"] = day["integrity"]["integrity_reasons"]
            if component.get("status") == "READY":
                component["status"] = "PARTIAL"
                day["status"] = "PARTIAL"
            day["readiness"] = day["status"]
    market_rows = _market_rows(days)
    stock_rows = _stock_rows(days)
    metrics = _aggregate(days, components)
    integrity_failures = sorted({item["date"] for item in components if item.get("status") == "FAILED"} | {day["market_date"] for day in days if day.get("integrity", {}).get("integrity_status") == "FAIL"})
    summary = {"dates": dates, "components": components, "days": days, "metrics": metrics, "evaluation_metrics": evaluated.get("metrics", {}) if days and not offline_only else {}, "fmp_requests": 0, "offline_network_calls": sum(int(day.get("metrics", {}).get("offline_network_attempts_blocked", 0) or 0) for day in days), "integrity_failures": integrity_failures, "stock_result_parity_mismatches": sum(day.get("stock_consistency_audit", {}).get("mismatch_count", 0) for day in days), "cross_symbol_candidate_leaks": sum(day.get("stock_consistency_audit", {}).get("leak_count", 0) for day in days), "market_review_rows": len(market_rows), "stock_review_rows": len(stock_rows)}
    if not plan:
        range_name = dates[0] if len(dates) == 1 else f"{dates[0]}_to_{dates[-1]}"
        runs_dir = BATCH_DIR / "runs"
        base_name = range_name if overwrite_main_evaluation else f"{range_name}_integrity_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
        package_dir = BATCH_DIR if overwrite_main_evaluation else runs_dir / base_name
        package_dir.mkdir(parents=True, exist_ok=True)
        summary["package_dir"] = str(package_dir)
        (package_dir / "historical_batch_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        previous_package = runs_dir / range_name
        _write_csv(package_dir / "market_day_review_queue.csv", market_rows, list(market_rows[0].keys()) if market_rows else ["date"], ("date",), preserve_path=previous_package / "market_day_review_queue.csv")
        _write_csv(package_dir / "stock_review_queue.csv", stock_rows, list(stock_rows[0].keys()) if stock_rows else ["date", "symbol"], ("date", "symbol"), preserve_path=previous_package / "stock_review_queue.csv")
        report_lines = ["# Historical Evaluation Batch", "", "| Date | Readiness | Integrity | Reasons |", "|---|---|---|---|"]
        report_lines.extend(f"| {day['market_date']} | {day.get('status')} | {day.get('integrity', {}).get('integrity_status')} | {', '.join(day.get('integrity', {}).get('integrity_reasons', [])) or '—'} |" for day in days)
        report_lines += ["", "## Aggregate metrics", "", *[f"- {key}: {value}" for key, value in metrics.items()]]
        (package_dir / "historical_batch_summary.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    return summary


def run_batch(dates: list[str], *, plan: bool = False, resume: bool = False, refresh_market_news: bool = False, offline_only: bool = False, overwrite_main_evaluation: bool = False, build_missing: bool = False, logger: logging.Logger | None = None) -> dict[str, Any]:
    """Run a batch, holding the system awake only during real execution."""
    logger = logger or logging.getLogger("market_bot.evaluation_batch")
    if plan:
        return _run_batch_impl(dates, plan=True, resume=resume, refresh_market_news=refresh_market_news, offline_only=offline_only, overwrite_main_evaluation=overwrite_main_evaluation, logger=logger)
    with prevent_system_sleep(logger=logger):
        repair = build_missing_components(dates, logger=logger) if build_missing else None
        result = _run_batch_impl(dates, plan=False, resume=resume, refresh_market_news=refresh_market_news, offline_only=offline_only or build_missing, overwrite_main_evaluation=overwrite_main_evaluation, logger=logger)
        if repair is not None:
            result["missing_component_build"] = repair
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and evaluate historical Market Driver samples.")
    parser.add_argument("--market-dates")
    parser.add_argument("--from", dest="from_date")
    parser.add_argument("--to", dest="to_date")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--refresh-market-news", action="store_true")
    parser.add_argument("--offline-only", action="store_true", help="Rebuild evaluation artifacts from existing local caches only.")
    parser.add_argument("--overwrite-main-evaluation", action="store_true", help="Write the consolidated package to output/evaluation_batch.")
    parser.add_argument("--build-missing", action="store_true", help="Build only missing dated news/verification caches before offline evaluation.")
    args = parser.parse_args(argv)
    load_dotenv(BASE_DIR / ".env")
    try:
        dates = select_dates(market_dates=args.market_dates, from_date=args.from_date, to_date=args.to_date)
        result = run_batch(dates, plan=args.plan, resume=args.resume, refresh_market_news=args.refresh_market_news, offline_only=args.offline_only, overwrite_main_evaluation=args.overwrite_main_evaluation, build_missing=args.build_missing)
    except ValueError as exc:
        parser.error(str(exc))
    for item in result["components"]:
        print(f"DATE {item['date']} readiness={item.get('status')} market_data={item.get('market_data')} movers={item.get('movers')} market_news={item.get('market_news')} stock_news={item.get('stock_news')} verification={item.get('verification')} expected_network_work={'YES' if item.get('expected_network_work') else 'NO'}")
    print(json.dumps(result["metrics"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
