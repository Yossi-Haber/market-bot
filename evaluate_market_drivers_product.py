"""Offline multi-day product evaluation for the production Market Drivers path.

This module evaluates existing snapshots and caches only.  It delegates replay
and verification to ``evaluate_market_drivers`` and adds product metrics and QA
flags without changing production behavior.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from dataclasses import fields
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import evaluate_market_drivers as replay
from drivers.causal import evaluate_causal_eligibility
from drivers.detector import build_driver_analysis, classify_driver
from drivers.market_driver_discovery import load_market_discovery_cache_state
from drivers.model import Driver
from drivers.runtime import _scope_analysis
from drivers.sector_driver import inherit_stock_explanations
from llm.input_builder import build_llm_payload
from news.providers.finnhub import analyst_action_direction_compatible


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
EVALUATION_DIR = OUTPUT_DIR / "evaluation"
LABELS_PATH = BASE_DIR / "evaluation" / "manual_labels.csv"
REQUIRED_CACHE_COMPONENTS = ("market_data", "movers", "finnhub", "verification")


def _float(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _move(driver: dict[str, Any]) -> float | None:
    evidence = driver.get("movement_evidence", [])
    if isinstance(evidence, list) and evidence and isinstance(evidence[0], dict):
        return _float(evidence[0].get("value"))
    return None


def _symbols(story: dict[str, Any]) -> set[str]:
    values = story.get("affected_symbols", story.get("symbols", []))
    if isinstance(values, str):
        values = [values]
    result = {str(value).upper() for value in values or []}
    for key in ("symbol", "target_symbol"):
        if story.get(key):
            result.add(str(story[key]).upper())
    return result


def _matches(story: dict[str, Any], driver: dict[str, Any]) -> bool:
    targets = {str(value).upper() for value in driver.get("related_symbols", [])}
    explicit_target = story.get("target_symbol") or story.get("symbol")
    if explicit_target:
        return str(explicit_target).upper() in targets
    return bool(_symbols(story).intersection(targets))


def _matches_symbol(story: dict[str, Any], symbol: str) -> bool:
    explicit_target = story.get("target_symbol") or story.get("symbol")
    if explicit_target:
        return str(explicit_target).upper() == symbol.upper()
    direct_subjects = {str(value).upper() for value in story.get("direct_subject_symbols", [])}
    if direct_subjects:
        return symbol.upper() in direct_subjects
    affected = story.get("affected_symbols", story.get("symbols", []))
    if isinstance(affected, str):
        affected = [affected]
    normalized = {str(value).upper() for value in affected or []}
    return len(normalized) == 1 and symbol.upper() in normalized


def _scope_stock_analysis(analysis: dict[str, Any], candidates: list[dict[str, Any]], verified: list[dict[str, Any]], report_data: dict[str, Any]) -> dict[str, Any]:
    """Reclassify stock drivers with fresh, target-symbol-only candidate lists."""
    stock_drivers: list[dict[str, Any]] = []
    for original in analysis.get("stock_catalysts", []):
        related = [str(value).upper() for value in original.get("related_symbols", [])]
        if not related:
            continue
        symbol = related[0]
        scoped = [dict(item) for item in candidates if _matches_symbol(item, symbol)]
        causal = [item for item in scoped if item.get("causal_eligible") is True]
        causal_titles = {str(item.get("canonical_event_title") or item.get("title") or "") for item in causal}
        scoped_verified = [dict(item) for item in verified if _matches_symbol(item, symbol) and str(item.get("canonical_event_title") or item.get("title") or "") in causal_titles]
        driver_fields = {item.name for item in fields(Driver)}
        typed = Driver(**{key: value for key, value in original.items() if key in driver_fields})
        classified = classify_driver(typed, causal, scoped_verified).as_dict()
        classified.update({key: value for key, value in original.items() if key not in driver_fields})
        classified["candidate_stories"] = scoped
        classified["target_symbol"] = symbol
        chosen = next((item for item in causal if _matches_symbol(item, symbol)), None)
        classified.update({
            "selected_event": (chosen or {}).get("canonical_event_title") or (chosen or {}).get("title"),
            "selected_published_at": (chosen or {}).get("published_at"),
            "event_type": (chosen or {}).get("event_type"),
            "subject_gate": (chosen or {}).get("subject_gate", "FAIL"),
            "causal_gate": (chosen or {}).get("causal_gate", "FAIL"),
            "causal_reason": (chosen or {}).get("causal_reason", "NO_CONCRETE_EVENT"),
            "event_time_relation": (chosen or {}).get("event_time_relation", "UNKNOWN"),
            "candidate_count": len(scoped),
        })
        stock_drivers.append(classified)
    analysis["stock_catalysts"] = stock_drivers
    analysis["stock_explanations"] = inherit_stock_explanations(stock_drivers, analysis.get("sector_drivers", []), analysis.get("market_driver_analysis", {}), report_data)
    return analysis


def _candidate_records(day: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = day.get("news_context", {}).get("candidate_stories", [])
    result = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        item = dict(candidate)
        affected = item.get("affected_symbols", item.get("symbols", []))
        if isinstance(affected, str):
            affected = [affected]
        explicit_target = item.get("target_symbol") or item.get("symbol")
        symbol = str(explicit_target or (affected[0] if len(affected or []) == 1 else "")).upper()
        clusters = day.get("news_context", {}).get("clusters", {}).get(symbol, []) if symbol else []
        matching_cluster = next((cluster for cluster in clusters if cluster.get("canonical_event_title") == item.get("title")), None)
        if matching_cluster:
            articles = matching_cluster.get("articles", [])
            dates = [getattr(article, "published_at", None) if not isinstance(article, dict) else article.get("published_at") for article in articles]
            item["cluster_article_dates"] = [value for value in dates if value]
            item["subject_score"] = matching_cluster.get("subject_score", item.get("subject_score", 0))
            item["target_role"] = matching_cluster.get("target_role", item.get("target_role"))
            item["event_actions"] = matching_cluster.get("event_actions", item.get("event_actions", []))
            item["target_symbol"] = matching_cluster.get("target_symbol", symbol)
            item["analyst_action"] = matching_cluster.get("analyst_action", item.get("analyst_action"))
        item.update(evaluate_causal_eligibility(item, day["market_date"]))
        if item.get("event_type") == "ANALYST_ACTION" and isinstance(item.get("analyst_action"), dict):
            move = next(
                (
                    movement.get("value")
                    for driver in day.get("stock_catalysts", [])
                    if symbol in {str(value).upper() for value in driver.get("related_symbols", [])}
                    for movement in driver.get("movement_evidence", [])[:1]
                    if isinstance(movement, dict)
                ),
                None,
            )
            action = item["analyst_action"]
            if action.get("subject_confidence") in {"DIRECT_TARGET", "STRONG_TARGET"} and not analyst_action_direction_compatible(action, move):
                item.update({"subject_gate": "PASS", "causal_gate": "FAIL", "causal_eligible": False, "causal_reason": "ANALYST_ACTION_DIRECTION_CONFLICT"})
        result.append(item)
    return result


def _verification_route(day: dict[str, Any], symbol: str) -> list[str]:
    for record in day.get("news_context", {}).get("verification", {}).get("records", []):
        if symbol in {str(value).upper() for value in record.get("affected_symbols", [])}:
            return list(record.get("verification_route", []))
    return []


def _stock_records(day: dict[str, Any], candidates: list[dict[str, Any]], production_analysis: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if production_analysis is None:
        production_analysis = _scope_analysis(
            {"main_market_driver": day.get("main_market_driver"), "sector_drivers": day.get("sector_drivers", []), "stock_catalysts": day.get("stock_catalysts", [])},
            candidates,
            day.get("news_context", {}).get("verification", {}).get("verified_stories", []),
        )
    records = []
    for driver in day.get("stock_catalysts", []):
        symbols = [str(value).upper() for value in driver.get("related_symbols", [])]
        symbol = symbols[0] if symbols else ""
        relevant = [item for item in candidates if _matches_symbol(item, symbol)]
        selected = next((item for item in relevant if item.get("causal_eligible")), None)
        rejected = next((item for item in relevant if not item.get("causal_eligible")), None)
        production_driver = next((item for item in production_analysis.get("stock_catalysts", []) if item.get("driver_id") == driver.get("driver_id")), {})
        status = production_driver.get("verification_status", "NO_CATALYST_FOUND")
        chosen = selected or rejected or {}
        timing_diag = day.get("news_context", {}).get("timing_diagnostics", {}).get(symbol, {})
        future_only = not relevant and int(timing_diag.get("future_article_count", 0) or 0) > 0
        if future_only:
            # A persisted production status cannot promote a mover when the
            # only matching-time evidence is absent from the as-of replay.
            status = "NO_CATALYST_FOUND"
        explanation = next((item for item in production_analysis.get("stock_explanations", []) if str(item.get("symbol", "")).upper() == symbol), {})
        stock_specific_status = "FOUND" if selected or (status == "CONFIRMED" and chosen) else "NOT_FOUND"
        # A rejected candidate can retain the provider's raw PASS marker while
        # still being ineligible after the full causal/timing evaluation.  The
        # replay record must expose the final gate state, otherwise the QA
        # layer reports a false state-invariant breach.
        causal_gate = chosen.get("causal_gate", "FAIL")
        if not selected and chosen:
            causal_gate = "FAIL"
        records.append({
            "date": day["market_date"],
            "symbol": symbol,
            "move_pct": _move(driver),
            "event_type": (selected or rejected or {}).get("event_type"),
            "selected_event": selected.get("canonical_event_title") or selected.get("title") if selected else None,
            "selected_published_at": selected.get("published_at") if selected else None,
            "selected_affected_symbols": selected.get("affected_symbols", []) if selected else [],
            "best_rejected_candidate": rejected.get("title") if rejected else None,
            "rejection_reason": rejected.get("causal_reason") if rejected else None,
            "causal_gate": causal_gate,
            "causal_reason": "TIMING_INELIGIBLE" if future_only else (selected or rejected or {}).get("causal_reason", "NO_CONCRETE_EVENT"),
            "event_time_relation": "FUTURE_EVENT" if future_only else (selected or rejected or {}).get("event_time_relation", "UNKNOWN"),
            "verification_status": status,
            "verification_decision": "VERIFIED" if status == "CONFIRMED" else "NOT_CONFIRMED",
            "final_driver_status": status,
            "source": selected.get("source_name") if selected else None,
            "source_tier": selected.get("source_tier") if selected else None,
            "verification_route": _verification_route(day, symbol) if selected else [],
            "candidate_count": len(relevant),
            "candidate_stories": [dict(item) for item in relevant],
            "future_article_count": int(timing_diag.get("future_article_count", 0) or 0),
            "stock_specific_catalyst_status": stock_specific_status,
            "overall_explanation_status": explanation.get("overall_explanation_status", "UNEXPLAINED"),
            "inherited_sector_driver": explanation.get("inherited_sector_driver"),
            "inherited_market_driver": explanation.get("inherited_market_driver"),
        })
    return records


def _synchronize_canonical_stock_results(analysis: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """Copy each stock result from one canonical per-symbol evaluation record."""
    by_symbol = {str(item.get("symbol", "")).upper(): item for item in records}
    for driver in analysis.get("stock_catalysts", []):
        symbol = str((driver.get("related_symbols") or [""])[0]).upper()
        record = by_symbol.get(symbol)
        if not record:
            continue
        driver.update({key: record.get(key) for key in ("selected_event", "selected_published_at", "event_type", "subject_gate", "causal_gate", "causal_reason", "event_time_relation", "candidate_count", "future_article_count", "stock_specific_catalyst_status", "overall_explanation_status", "verification_status")})
        driver["candidate_stories"] = [dict(item) for item in record.get("candidate_stories", [])]
    for explanation in analysis.get("stock_explanations", []):
        symbol = str(explanation.get("symbol", "")).upper()
        record = by_symbol.get(symbol)
        if not record:
            continue
        for key in ("stock_specific_catalyst_status", "overall_explanation_status", "selected_event", "selected_published_at", "event_time_relation", "causal_gate", "causal_reason", "verification_status", "candidate_count", "future_article_count"):
            explanation[key] = record.get(key)
    return analysis


def audit_stock_consistency(day: dict[str, Any], review_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Audit stock driver, canonical record, explanation, and queue parity."""
    mismatches: list[str] = []
    leaks: list[str] = []
    records = {str(item.get("symbol", "")).upper(): item for item in day.get("stock_records", [])}
    explanations = {str(item.get("symbol", "")).upper(): item for item in day.get("stock_explanations", [])}
    drivers = {str((item.get("related_symbols") or [""])[0]).upper(): item for item in day.get("stock_catalysts", [])}
    queue = {str(item.get("symbol", "")).upper(): item for item in review_rows or []}
    parity_fields = ("selected_event", "event_time_relation", "causal_gate", "causal_reason", "verification_status", "candidate_count", "future_article_count", "stock_specific_catalyst_status", "overall_explanation_status")
    for symbol, record in records.items():
        for label, other in (("driver", drivers.get(symbol, {})), ("explanation", explanations.get(symbol, {})), ("queue", queue.get(symbol, record))):
            for field in parity_fields:
                if field in record and other.get(field) != record.get(field):
                    mismatches.append(f"{symbol}:{label}:{field}")
        driver = drivers.get(symbol, {})
        stories = driver.get("candidate_stories", [])
        if len(stories) != int(record.get("candidate_count", 0) or 0):
            mismatches.append(f"{symbol}:candidate_count")
        for story in stories:
            explicit = story.get("target_symbol") or story.get("symbol")
            if explicit and str(explicit).upper() != symbol:
                leaks.append(f"{symbol}:{explicit}")
            elif not explicit and not _matches_symbol(story, symbol):
                leaks.append(f"{symbol}:unrelated_candidate")
        if record.get("selected_event") and record.get("event_time_relation") == "FUTURE_EVENT":
            mismatches.append(f"{symbol}:selected_future_event")
    return {"stock_result_parity_mismatches": sorted(set(mismatches)), "cross_symbol_candidate_leaks": sorted(set(leaks)), "mismatch_count": len(set(mismatches)), "leak_count": len(set(leaks))}


def _quality_flags(day: dict[str, Any], candidates: list[dict[str, Any]], records: list[dict[str, Any]]) -> list[str]:
    flags: set[str] = set()
    for record in records:
        if record["causal_gate"] == "FAIL" and record["final_driver_status"] in {"PLAUSIBLE_NOT_VERIFIED", "CONFIRMED"}:
            flags.add("EVALUATOR_STATE_INVARIANT_BREACH")
        if record["event_time_relation"] == "FUTURE_EVENT" and record["final_driver_status"] in {"PLAUSIBLE_NOT_VERIFIED", "CONFIRMED"}:
            flags.add("EVALUATOR_STATE_INVARIANT_BREACH")
        if record["causal_reason"] == "TIMING_INELIGIBLE" and record["causal_gate"] == "PASS":
            flags.add("EVALUATOR_STATE_INVARIANT_BREACH")
        if record["final_driver_status"] in {"PLAUSIBLE_NOT_VERIFIED", "CONFIRMED"} and record["causal_gate"] != "PASS":
            flags.add("POSSIBLE_FALSE_POSITIVE")
        if record["event_time_relation"] == "FUTURE_EVENT" and record["final_driver_status"] in {"PLAUSIBLE_NOT_VERIFIED", "CONFIRMED"}:
            flags.add("FUTURE_EVENT_LEAK")
        if record["causal_gate"] == "PASS" and not record.get("verification_route", []):
            flags.add("VERIFICATION_ROUTE_EMPTY")
    for candidate in candidates:
        if candidate.get("causal_reason") in {"COMMENTARY_ONLY", "REACTION_TO_MOVE"} and any(record["final_driver_status"] in {"PLAUSIBLE_NOT_VERIFIED", "CONFIRMED"} and record.get("selected_event") == (candidate.get("title") or candidate.get("canonical_event_title")) for record in records):
            flags.add("COMMENTARY_PROMOTED")
    for driver in day.get("stock_catalysts", []):
        related = {str(value).upper() for value in driver.get("related_symbols", [])}
        for story in driver.get("candidate_stories", []):
            if isinstance(story, dict) and related and not any(_matches_symbol(story, symbol) for symbol in related):
                flags.add("CROSS_SYMBOL_EVIDENCE_LEAK")
    if int(day.get("metrics", {}).get("fmp_requests", 0) or 0) > 0:
        flags.add("FMP_USED_IN_MARKET_DRIVERS")
    # Validate the exact safe-driver boundary, not internal candidate records.
    safe_input = build_llm_payload({"publication_readiness": {"status": "READY"}, "stock_catalysts": day.get("stock_catalysts", []), "sector_drivers": [], "main_market_driver": None})
    safe_text = json.dumps(safe_input, ensure_ascii=False)
    for driver in day.get("stock_catalysts", []):
        if driver.get("verification_status") == "NO_CATALYST_FOUND":
            if any(str(candidate.get(key) or "") in safe_text for candidate in driver.get("candidate_stories", []) if isinstance(candidate, dict) for key in ("title", "headline", "source_name", "source")):
                flags.add("NO_CATALYST_METADATA_LEAK")
    return sorted(flags)


def _read_labels(path: Path = LABELS_PATH) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _label_metrics(records: list[dict[str, Any]], labels: list[dict[str, str]]) -> dict[str, Any]:
    """Score explicit truth against predictions, separating event and attribution.

    Legacy FP/FN labels describe an earlier model, not independent ground truth;
    they and UNCLEAR rows are excluded. Event identity is assessed only when a
    human supplied expected_event. Identity comparison is deliberately exact
    after case/punctuation normalization; this evaluator never guesses a match.
    A wrong-event prediction contributes both a false positive and false negative.
    """
    expected_status = {"CORRECT_CAUSAL": "CONFIRMED", "PLAUSIBLE_CAUSAL": "PLAUSIBLE_NOT_VERIFIED", "NO_PUBLIC_CATALYST": "NO_CATALYST_FOUND"}
    positive_statuses = {"CONFIRMED", "PLAUSIBLE_NOT_VERIFIED"}
    def normalize(value: Any) -> str:
        return " ".join(re.findall(r"\w+", str(value or "").casefold()))

    def summarize(counts: dict[str, int]) -> dict[str, Any]:
        tp, fp, fn, tn = (counts[key] for key in ("tp", "fp", "fn", "tn"))
        return {**counts, "precision": tp / (tp + fp) if tp + fp else None,
                "recall": tp / (tp + fn) if tp + fn else None,
                "false_positive_rate": fp / (fp + tn) if fp + tn else None,
                "false_negative_rate": fn / (fn + tp) if fn + tp else None}

    def score(counts: dict[str, int], truth_positive: bool, predicted: bool, matches: bool) -> None:
        counts["support"] += 1
        counts["positive_support"] += int(truth_positive)
        counts["negative_support"] += int(not truth_positive)
        if truth_positive and predicted and matches:
            counts["tp"] += 1
        else:
            counts["fp"] += int(predicted)
            counts["fn"] += int(truth_positive)
            counts["tn"] += int(not truth_positive and not predicted)

    def empty() -> dict[str, int]:
        return dict.fromkeys(("tp", "fp", "fn", "tn", "support", "positive_support", "negative_support"), 0)

    by_key = {(row.get("date"), row.get("symbol", "").upper()): row for row in records}
    labeled = [(row, by_key.get((row.get("date"), row.get("symbol", "").upper()))) for row in labels]
    labeled = [(row, record) for row, record in labeled if record]
    event, causal = empty(), empty()
    correct = unassessed = excluded = wrong_event = 0
    for row, record in labeled:
        expected = expected_status.get(str(row.get("label", "")).upper())
        if expected is None:
            excluded += 1
            continue
        truth_positive = expected != "NO_CATALYST_FOUND"
        status = record.get("final_driver_status")
        selected = normalize(record.get("selected_event"))
        wanted = normalize(row.get("expected_event"))
        event_matches = bool(selected and selected == wanted) if wanted else True
        predicted_causal = status in positive_statuses
        attribution_matches = status == expected and event_matches and (truth_positive or not selected)
        score(causal, truth_positive, predicted_causal, attribution_matches)
        correct += int(attribution_matches)
        if truth_positive and not wanted:
            unassessed += 1
        else:
            score(event, truth_positive, bool(selected), event_matches)
            wrong_event += int(truth_positive and bool(selected) and not event_matches)
    event_result, causal_result = summarize(event), summarize(causal)
    return {"labeled_records": causal["support"], "matched_label_records": len(labeled),
            "unmatched_label_records": len(labels) - len(labeled), "excluded_label_records": excluded,
            "correct_records": correct, "accuracy_on_labeled": correct / causal["support"] if causal["support"] else None,
            "precision_on_labeled": causal_result["precision"], "recall_on_labeled": causal_result["recall"],
            "false_positive_rate_on_labeled": causal_result["false_positive_rate"],
            "false_negative_rate_on_labeled": causal_result["false_negative_rate"],
            "event_identification": event_result, "causal_attribution": causal_result,
            "event_identity_unassessed": unassessed, "wrong_event_predictions": wrong_event}


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * 0.95) - 1)]


def evaluate_product(start: str, end: str, *, market_dates: list[str] | None = None, output_dir: Path = OUTPUT_DIR) -> dict[str, Any]:
    """Evaluate requested cached dates with the existing offline replay engine."""
    if market_dates:
        selected = sorted(set(market_dates))
        start, end = selected[0], selected[-1]
    base = replay.evaluate_market_drivers(start, end, output_dir=output_dir)
    wanted = set(market_dates or [day["market_date"] for day in base.get("days", [])] + [item["market_date"] for item in base.get("skipped_dates", [])])
    days = []
    skipped = []
    for market_date in sorted(wanted):
        base_day = next((day for day in base.get("days", []) if day["market_date"] == market_date), None)
        coverage = (base_day or {}).get("cache_coverage", {})
        if base_day is None:
            missing_components = []
            if replay._dated_snapshot(output_dir, "market_snapshot", market_date) is None:
                missing_components.append("market_data")
            if replay._dated_snapshot(output_dir, "movers_snapshot", market_date) is None:
                missing_components.append("movers")
            skipped.append({"market_date": market_date, "status": "SKIPPED_MISSING_CACHE", "missing_components": missing_components or ["required_snapshot"]})
            continue
        status = "READY" if all(coverage.get(key) == "AVAILABLE" for key in REQUIRED_CACHE_COMPONENTS) else "PARTIAL"
        market_cache = load_market_discovery_cache_state(output_dir.parent / "cache", market_date)
        market_candidates = list(market_cache.get("eligible_candidates", [])) if market_cache["state"] == "HIT" else []
        # Use one candidate source for stock records, driver classification,
        # explanations, and QA rows. Persisted driver snapshots are retained as
        # context only; they cannot substitute a separately selected candidate set.
        candidates = _candidate_records(base_day)
        verified = base_day.get("news_context", {}).get("verification", {}).get("verified_stories", [])
        production_input = {"main_market_driver": base_day.get("main_market_driver"), "sector_drivers": base_day.get("sector_drivers", []), "stock_catalysts": base_day.get("stock_catalysts", [])}
        production_analysis = _scope_analysis(production_input, candidates, verified)
        # Historical snapshots created before the cross-asset engine remain
        # replayable.  Rebuild only the additive market/sector explanation
        # layer from the same local dated market and movers artifacts.
        # Always rebuild the additive market/sector layer from the shared
        # date-scoped market-news cache. Legacy production snapshots may exist,
        # but they must not hide a HIT or turn a MISS into stale evidence.
        if market_cache["state"] in {"HIT", "MISS", "INVALID_SCHEMA", "DATE_MISMATCH", "INTEGRITY_FAIL"}:
            market_snapshot = replay._dated_snapshot(output_dir, "market_snapshot", market_date) or {}
            movers_snapshot = replay._dated_snapshot(output_dir, "movers_snapshot", market_date) or {}
            market_values = market_snapshot.get("market", {}) if isinstance(market_snapshot, dict) else {}
            enriched_input = {
                "market_date": market_date,
                "integrity_status": base_day.get("integrity", {}).get("integrity_status", ""),
                "integrity_violation": bool(base_day.get("integrity", {}).get("integrity_status") == "FAIL"),
                "market": {"core": market_values.get("core", {})},
                "macro": market_values.get("macro", {}),
                "sectors": market_values.get("sectors", {}),
                "movers": {"top_gainers": movers_snapshot.get("top_gainers", []), "top_losers": movers_snapshot.get("top_losers", [])},
                "candidate_stories": candidates,
                "sector_candidate_stories": base_day.get("news_context", {}).get("sector_candidate_stories", []),
                "market_candidate_stories": market_candidates,
                "verified_stories": verified,
            }
            additive_analysis = build_driver_analysis(enriched_input)
            production_analysis = {**production_analysis, "market_driver_analysis": additive_analysis.get("market_driver_analysis", {}), "sector_driver_analysis": additive_analysis.get("sector_driver_analysis", []), "sector_drivers": additive_analysis.get("sector_drivers", production_analysis.get("sector_drivers", [])), "stock_explanations": additive_analysis.get("stock_explanations", [])}
            if additive_analysis.get("main_market_driver"):
                production_analysis["main_market_driver"] = additive_analysis["main_market_driver"]
            production_analysis["stock_catalysts"] = additive_analysis.get("stock_catalysts", [])
            production_analysis = _scope_stock_analysis(production_analysis, candidates, verified, enriched_input)
        stock_records = _stock_records(base_day, candidates, production_analysis)
        production_analysis = _synchronize_canonical_stock_results(production_analysis, stock_records)
        base_day["stock_records"] = stock_records
        base_day["stock_catalysts"] = production_analysis.get("stock_catalysts", [])
        base_day["stock_explanations"] = production_analysis.get("stock_explanations", [])
        consistency_audit = audit_stock_consistency(base_day)
        warnings = _quality_flags(base_day, candidates, stock_records)
        causal_passed = sum(item.get("causal_eligible") is True for item in candidates)
        commentary_rejected = sum(item.get("causal_reason") in {"COMMENTARY_ONLY", "REACTION_TO_MOVE"} for item in candidates)
        timing_rejected = sum(item.get("causal_reason") == "TIMING_INELIGIBLE" for item in candidates)
        no_event_rejected = sum(item.get("causal_reason") == "NO_CONCRETE_EVENT" for item in candidates)
        day = dict(base_day)
        market_discovery = day.get("news_context", {}).get("market_discovery", {})
        market_diag = market_cache.get("diagnostics", {})
        main_driver = production_analysis.get("main_market_driver") or {}
        selected_market_count = len(main_driver.get("candidate_stories", []))
        metric_fields = {name: market_diag.get(name) for name in ("raw_candidate_count", "filtered_candidate_count", "eligible_candidate_count")}
        metric_fields["selected_candidate_count"] = selected_market_count
        metric_fields["metric_state"] = {name: "AVAILABLE" if value is not None else "MISSING" for name, value in metric_fields.items() if name != "metric_state"}
        day.update({"status": status, "missing_components": [key for key, value in coverage.items() if value != "AVAILABLE"], "production_analysis": production_analysis, "main_market_driver": production_analysis.get("main_market_driver"), "sector_drivers": production_analysis.get("sector_drivers", []), "stock_catalysts": production_analysis.get("stock_catalysts", []), "market_driver_analysis": production_analysis.get("market_driver_analysis", {}), "sector_driver_analysis": production_analysis.get("sector_driver_analysis", []), "stock_explanations": production_analysis.get("stock_explanations", []), "stock_records": stock_records, "stock_consistency_audit": consistency_audit, "market_discovery_cache_state": market_discovery.get("state", "MISS"), "market_discovery_cache_path": market_discovery.get("path"), "eligible_market_candidates_loaded": market_discovery.get("eligible_market_candidates_loaded", 0), "selected_market_candidate": market_discovery.get("selected_market_candidate"), "market_candidate_metrics": metric_fields, "warnings": sorted(set(day.get("warnings", []) + warnings)), "causal": {"causal_candidates_passed": causal_passed, "rejected_commentary": commentary_rejected, "rejected_timing": timing_rejected, "rejected_no_concrete_event": no_event_rejected}})
        days.append(day)
    records = [record for day in days for record in day["stock_records"]]
    ready = [day for day in days if day["status"] == "READY"]
    partial = [day for day in days if day["status"] == "PARTIAL"]

    def aggregate(scope: list[dict[str, Any]]) -> dict[str, Any]:
        scoped_records = [record for day in scope for record in day["stock_records"]]
        total_moves = len(scoped_records)
        confirmed = sum(record["final_driver_status"] == "CONFIRMED" for record in scoped_records)
        plausible = sum(record["final_driver_status"] == "PLAUSIBLE_NOT_VERIFIED" for record in scoped_records)
        unexplained = sum(record["final_driver_status"] == "NO_CATALYST_FOUND" for record in scoped_records)
        causal_total = sum(sum(day["causal"].values()) for day in scope)
        causal_passed = sum(day["causal"]["causal_candidates_passed"] for day in scope)
        verification = [day.get("news_context", {}).get("verification", {}) for day in scope]
        finnhub_hits = sum(day.get("news_context", {}).get("finnhub_cache", {}).get(symbol, {}).get("status") == "AVAILABLE" for day in scope for symbol in day.get("news_context", {}).get("finnhub_cache", {}))
        finnhub_total = sum(len(day.get("news_context", {}).get("finnhub_cache", {})) for day in scope)
        runtimes = [_float(day.get("metrics", {}).get("runtime_seconds")) or 0.0 for day in scope]
        main_statuses = [day.get("main_market_driver", {}).get("verification_status") if day.get("main_market_driver") else None for day in scope]
        verification_candidates = sum(item.get("verification_candidates_considered", 0) for item in verification)
        verification_confirmed = sum(item.get("verification_confirmed", 0) for item in verification)
        return {
            "days_evaluated": len(scope), "total_meaningful_moves": total_moves, "confirmed_drivers": confirmed, "plausible_drivers": plausible, "unexplained_moves": unexplained,
            "confirmed_rate": confirmed / total_moves if total_moves else 0.0, "plausible_rate": plausible / total_moves if total_moves else 0.0, "unexplained_rate": unexplained / total_moves if total_moves else 0.0,
            "causal_candidate_pass_rate": causal_passed / causal_total if causal_total else 0.0, "commentary_rejection_rate": sum(day["causal"]["rejected_commentary"] for day in scope) / causal_total if causal_total else 0.0, "timing_rejection_rate": sum(day["causal"]["rejected_timing"] for day in scope) / causal_total if causal_total else 0.0,
            "verification_candidates": verification_candidates, "verification_confirmed": verification_confirmed, "verification_yield": verification_confirmed / verification_candidates if verification_candidates else 0.0,
            "days_with_confirmed_main_driver": sum(status == "CONFIRMED" for status in main_statuses), "days_with_plausible_main_driver": sum(status == "PLAUSIBLE_NOT_VERIFIED" for status in main_statuses), "days_with_no_main_driver": sum(status in {None, "NO_CATALYST_FOUND", "NO_MARKET_DRIVER_FOUND"} for status in main_statuses),
            "finnhub_cache_hit_rate": finnhub_hits / finnhub_total if finnhub_total else 0.0, "avg_finnhub_candidates_per_mover": sum(day.get("news_context", {}).get("provider_metrics", {}).get("finnhub_candidates_used", 0) for day in scope) / total_moves if total_moves else 0.0, "avg_verification_attempts_per_mover": sum(item.get("verification_evidence_attempts", 0) for item in verification) / total_moves if total_moves else 0.0,
            "avg_driver_runtime_seconds": sum(runtimes) / len(runtimes) if runtimes else 0.0, "p95_driver_runtime_seconds": _p95(runtimes), "fmp_requests_total": sum(day.get("metrics", {}).get("fmp_requests", 0) for day in scope),
            "market_driver_confirmed_days": sum((day.get("market_driver_analysis", {}).get("primary_driver") or {}).get("verification_status") == "CONFIRMED" for day in scope),
            "market_driver_plausible_days": sum((day.get("market_driver_analysis", {}).get("primary_driver") or {}).get("verification_status") == "PLAUSIBLE_NOT_VERIFIED" for day in scope),
            "market_driver_missing_days": sum((day.get("market_driver_analysis", {}).get("primary_driver") or {}).get("verification_status") in {None, "NO_MARKET_DRIVER_FOUND", "NO_CATALYST_FOUND"} for day in scope),
            "market_explanation_coverage_rate": sum((day.get("market_driver_analysis", {}).get("primary_driver") or {}).get("verification_status") in {"CONFIRMED", "PLAUSIBLE_NOT_VERIFIED"} for day in scope) / len(scope) if scope else 0.0,
            "sector_explanation_coverage_rate": sum(bool(day.get("sector_driver_analysis")) for day in scope) / len(scope) if scope else 0.0,
            "stock_specific_explanation_rate": sum(item.get("overall_explanation_status") == "STOCK_SPECIFIC" for day in scope for item in day.get("stock_explanations", [])) / sum(len(day.get("stock_explanations", [])) for day in scope) if sum(len(day.get("stock_explanations", [])) for day in scope) else 0.0,
            "broad_market_explanation_rate": sum(item.get("overall_explanation_status") == "MARKET_EXPLAINED" for day in scope for item in day.get("stock_explanations", [])) / sum(len(day.get("stock_explanations", [])) for day in scope) if sum(len(day.get("stock_explanations", [])) for day in scope) else 0.0,
            "multi_factor_explanation_rate": sum(item.get("overall_explanation_status") == "MULTI_FACTOR" for day in scope for item in day.get("stock_explanations", [])) / sum(len(day.get("stock_explanations", [])) for day in scope) if sum(len(day.get("stock_explanations", [])) for day in scope) else 0.0,
            "fully_unexplained_rate": sum(item.get("overall_explanation_status") == "UNEXPLAINED" for day in scope for item in day.get("stock_explanations", [])) / sum(len(day.get("stock_explanations", [])) for day in scope) if sum(len(day.get("stock_explanations", [])) for day in scope) else 0.0,
        }

    ready_metrics = aggregate(ready)
    all_metrics = aggregate([*ready, *partial])
    metrics = {**all_metrics, "days_evaluated": len(ready), "ready_days_evaluated": len(ready), "partial_days_evaluated": len(partial), "skipped_days": len(skipped), "days_skipped": len(skipped), "ready_only_metrics": ready_metrics, "all_available_metrics": all_metrics}
    review_queue = [record for record in records if abs(record.get("move_pct") or 0) >= 4]
    warnings = sorted({warning for day in days for warning in day.get("warnings", [])})
    parity_mismatches = sum(day.get("stock_consistency_audit", {}).get("mismatch_count", 0) for day in days)
    candidate_leaks = sum(day.get("stock_consistency_audit", {}).get("leak_count", 0) for day in days)
    labels = _label_metrics(records, _read_labels())
    return {"from_date": start, "to_date": end, "days": days, "skipped_dates": skipped, "stock_records": records, "review_queue": review_queue, "warnings": warnings, "stock_result_parity_mismatches": parity_mismatches, "cross_symbol_candidate_leaks": candidate_leaks, "metrics": {**metrics, **labels}, "offline": True}


def _markdown(result: dict[str, Any]) -> str:
    result = replay.to_json_safe(result)
    lines = ["# Market Drivers Multi-Day Product Evaluation", "", "## Executive summary", "", f"Evaluated {result['metrics'].get('days_evaluated', 0)} READY day(s); skipped {result['metrics'].get('days_skipped', 0)} day(s).", "", "## Per-day", "", "| Date | Status | Confirmed | Plausible | Unexplained |", "|---|---|---:|---:|---:|"]
    for day in result["days"]:
        lines.append(f"| {day['market_date']} | {day['status']} | {sum(r['final_driver_status'] == 'CONFIRMED' for r in day['stock_records'])} | {sum(r['final_driver_status'] == 'PLAUSIBLE_NOT_VERIFIED' for r in day['stock_records'])} | {sum(r['final_driver_status'] == 'NO_CATALYST_FOUND' for r in day['stock_records'])} |")
    lines += ["", "## Aggregate metrics", "", "```json", json.dumps(result["metrics"], ensure_ascii=False, indent=2), "```", "", "## Large-move review queue", "", "| Date | Symbol | Move | Status | Event |", "|---|---|---:|---|---|"]
    for item in result["review_queue"]:
        lines.append(f"| {item['date']} | {item['symbol']} | {item['move_pct']}% | {item['final_driver_status']} | {item.get('selected_event') or ''} |")
    lines += ["", "## Warnings", ""] + [f"- {warning}" for warning in result["warnings"] or ["None"]]
    return "\n".join(lines) + "\n"


def _write_outputs(result: dict[str, Any], output_dir: Path) -> None:
    safe_result = replay.to_json_safe(result)
    destination = output_dir / "evaluation"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "market_driver_multiday.json").write_text(json.dumps(safe_result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (destination / "market_driver_multiday.md").write_text(_markdown(safe_result), encoding="utf-8")
    with (destination / "market_driver_review_queue.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["date", "symbol", "move_pct", "final_driver_status", "selected_event", "causal_gate", "verification_status", "causal_reason"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: safe_result_item.get(field) for field in fields} for safe_result_item in safe_result["review_queue"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--market-dates")
    parser.add_argument("--artifact-dir", help="Write evaluation artifacts to this run-specific directory.")
    parser.add_argument("--offline", action="store_true", required=True)
    args = parser.parse_args(argv)
    dates = [value.strip() for value in args.market_dates.split(",") if value.strip()] if args.market_dates else None
    if dates:
        start, end = dates[0], dates[-1]
    elif args.from_date and args.to_date:
        start, end = args.from_date, args.to_date
    else:
        parser.error("provide --from-date/--to-date or --market-dates")
    result = evaluate_product(start, end, market_dates=dates)
    artifact_dir = Path(args.artifact_dir).resolve() if args.artifact_dir else OUTPUT_DIR
    _write_outputs(result, artifact_dir)
    for day in result["days"]:
        print(f"DATE: {day['market_date']} status={day['status']} missing={','.join(day['missing_components']) or 'none'}")
        print(f"MARKET DISCOVERY: state={day.get('market_discovery_cache_state')} path={day.get('market_discovery_cache_path')} eligible_loaded={day.get('eligible_market_candidates_loaded', 0)} selected={day.get('selected_market_candidate')}")
        print(f"STOCK MOVES: meaningful_moves={len(day['stock_records'])} confirmed={sum(r['final_driver_status'] == 'CONFIRMED' for r in day['stock_records'])} plausible={sum(r['final_driver_status'] == 'PLAUSIBLE_NOT_VERIFIED' for r in day['stock_records'])} unexplained={sum(r['final_driver_status'] == 'NO_CATALYST_FOUND' for r in day['stock_records'])}")
    for day in result["skipped_dates"]:
        print(f"DATE: {day['market_date']} status=SKIPPED_MISSING_CACHE missing={','.join(day.get('missing_components', []))}")
    print(json.dumps(result["metrics"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
