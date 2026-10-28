"""Production orchestration for symbol-scoped Market Driver analysis."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import fields
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from news.providers.finnhub import (
    FinnhubProvider,
    analyst_action_direction_compatible,
    cluster_finnhub_candidates,
    classify_finnhub_event,
    extract_event_signature,
    finnhub_subject_role,
    parse_analyst_action,
)
from news.schema import NewsCandidate
from news.verification_acquisition import LiveEvidenceAcquirer
from news.verification_v2 import verify_candidate

from .causal import evaluate_causal_eligibility, publication_time_relation
from .detector import build_driver_analysis, classify_driver
from .model import Driver
from .sector_evidence import build_sector_evidence_pool


class _DisabledFMP:
    """Ensure FMP remains dormant in the Market Drivers production path."""

    enabled = False
    news_requests = 0
    press_release_requests = 0
    evidence_matches = 0
    no_match = 0
    errors = 0


def _candidate_from_cluster(cluster: dict[str, Any], symbol: str, market_date: str | None = None) -> dict[str, Any]:
    best = cluster.get("best_candidate")
    articles = []
    article_objects = {}
    for article in cluster.get("articles", []):
        if isinstance(article, dict):
            article = NewsCandidate(**{field.name: article[field.name] for field in fields(NewsCandidate) if field.name in article})
        signature = extract_event_signature(article, symbol)
        role, _, _ = finnhub_subject_role(article, symbol)
        article_objects[(article.title, article.published_at)] = article
        articles.append({
            key: getattr(article, key, None)
            for key in ("title", "summary", "published_at", "affected_symbols", "category", "source_name", "source_url")
        } | signature | {"target_symbol": symbol.upper(), "target_role": role})
    candidate = {
        "title": cluster.get("canonical_event_title"),
        "summary": getattr(best, "summary", None),
        "published_at": getattr(best, "published_at", None),
        "event_occurred_at": getattr(best, "raw_metadata", {}).get("event_occurred_at"),
        "affected_symbols": [symbol.upper()],
        "target_symbol": cluster.get("target_symbol", symbol.upper()),
        "event_type": cluster.get("event_type"),
        "category": cluster.get("event_type"),
        "actors": list(cluster.get("actors", [])),
        "topic_family": cluster.get("topic_family"),
        "event_actions": list(cluster.get("event_actions", [])),
        "event_signature": cluster.get("event_signature"),
        "event_cluster_id": cluster.get("event_cluster_id"),
        "canonical_event_title": cluster.get("canonical_event_title"),
        "target_role": cluster.get("target_role"),
        "source_url": getattr(best, "source_url", None),
        "source_name": getattr(best, "source_name", None),
        "discovery_source": "finnhub",
        "subject_score": cluster.get("subject_score", 0),
        "analyst_action": cluster.get("analyst_action"),
        "selection_score": cluster.get("selection_score", 0),
        "cluster_article_dates": [item.get("published_at") for item in articles if item.get("published_at")],
        "cluster_articles": articles,
    }
    if market_date:
        timing = evaluate_causal_eligibility(candidate, market_date)
        if timing["event_time_relation"] == "BEFORE_OR_DURING_SESSION" and publication_time_relation(candidate["published_at"], market_date) == "FUTURE_EVENT":
            support = next((article for article in articles if article.get("published_at") == timing["earliest_supported_publication_at"] and article.get("title") == timing["supporting_publication_title"]), None)
            if support:
                # Verification/publication must not acquire new facts from a
                # post-close recap merely because an earlier event was matched.
                candidate["representative_published_at"] = candidate["published_at"]
                candidate["representative_title"] = candidate["title"]
                for key in ("title", "summary", "published_at", "source_name", "source_url"):
                    candidate[key] = support.get(key)
                candidate["canonical_event_title"] = candidate["title"]
                for key in ("event_actions", "topic_family", "actors"):
                    candidate[key] = support.get(key)
                candidate["event_type"] = candidate["category"] = classify_finnhub_event(candidate["title"])[0]
                supporting_article = article_objects[(support["title"], support["published_at"])]
                candidate["analyst_action"] = parse_analyst_action(supporting_article, symbol)
                candidate["event_occurred_at"] = supporting_article.raw_metadata.get("event_occurred_at")
    return candidate


def _select_cluster(
    clusters: list[dict[str, Any]],
    symbol: str,
    market_date: str,
    move_pct: float | None,
) -> dict[str, Any] | None:
    """Prefer a point-in-time, direction-compatible direct analyst action."""
    eligible: list[dict[str, Any]] = []
    for cluster in clusters:
        if cluster.get("event_type") != "ANALYST_ACTION":
            continue
        action = cluster.get("analyst_action")
        if not isinstance(action, dict) or action.get("target_symbol") != symbol.upper():
            continue
        if not analyst_action_direction_compatible(action, move_pct):
            continue
        candidate = _candidate_from_cluster(cluster, symbol, market_date)
        if evaluate_causal_eligibility(candidate, market_date)["event_time_relation"] != "BEFORE_OR_DURING_SESSION":
            continue
        if cluster.get("event_type") == "ANALYST_ACTION":
            action = cluster.get("analyst_action")
            if not isinstance(action, dict) or action.get("target_symbol") != symbol.upper():
                continue
            if not analyst_action_direction_compatible(action, move_pct):
                continue
        if evaluate_causal_eligibility(candidate, market_date).get("causal_eligible"):
            eligible.append(cluster)
    if eligible:
        return max(eligible, key=lambda item: item.get("selection_score", 0))
    # Retain existing rank order among eligible events. A future cluster cannot
    # hide an already reported event for the completed session.
    timely = [cluster for cluster in clusters if evaluate_causal_eligibility(_candidate_from_cluster(cluster, symbol, market_date), market_date)["causal_eligible"]]
    return timely[0] if timely else (clusters[0] if clusters else None)


def _matches(story: dict[str, Any], driver: dict[str, Any]) -> bool:
    story_symbols = {str(value).upper() for value in story.get("affected_symbols", [])}
    return bool(story_symbols.intersection(str(value).upper() for value in driver.get("related_symbols", [])))


def _scope_analysis(analysis: dict[str, Any], candidates: list[dict[str, Any]], verified: list[dict[str, Any]]) -> dict[str, Any]:
    """Reclassify each production hypothesis with only relevant evidence."""
    driver_fields = {field.name for field in fields(Driver)}
    required_driver_fields = {"driver_id", "driver_type", "scope", "direction", "strength"}

    def normalize_driver(item: dict[str, Any]) -> Driver:
        missing = sorted(field for field in required_driver_fields if item.get(field) is None)
        if missing:
            raise ValueError(f"INVALID_DRIVER_SCHEMA:missing={','.join(missing)}")
        # Historical snapshots may contain additive classifier metadata such
        # as hypothesis_status or warnings.  Adapt only the typed Driver
        # contract, then preserve those extra fields on the result below.
        return Driver(**{key: item[key] for key in driver_fields if key in item})

    for key in ("main_market_driver", "sector_drivers", "stock_catalysts"):
        values = ([analysis[key]] if analysis.get(key) else []) if key == "main_market_driver" else analysis.get(key) or []
        scoped = []
        for item in values:
            if key == "sector_drivers":
                # Keep sector evidence assigned by build_driver_analysis;
                # ticker-scoped verification cannot confirm a sector event.
                scoped.append(dict(item))
                continue
            relevant_candidates = [story for story in candidates if _matches(story, item)]
            relevant_verified = [story for story in verified if _matches(story, item)]
            causal_candidates = [story for story in relevant_candidates if story.get("causal_eligible")]
            causal_verified = [story for story in relevant_verified if story.get("causal_eligible", True)]
            result = classify_driver(normalize_driver(item), causal_candidates, causal_verified).as_dict()
            for extra_key, value in item.items():
                if extra_key not in driver_fields:
                    result[extra_key] = value
            if item.get("driver_type") == "STOCK_CATALYST":
                chosen = relevant_candidates[0] if relevant_candidates else {}
                result.update({
                    "selected_event": chosen.get("canonical_event_title") or chosen.get("title"),
                    "event_type": chosen.get("event_type"),
                    "subject_gate": chosen.get("subject_gate", "FAIL"),
                    "causal_gate": chosen.get("causal_gate", "FAIL"),
                    "causal_reason": chosen.get("causal_reason", "NO_CONCRETE_EVENT"),
                    "event_time_relation": chosen.get("event_time_relation", "UNKNOWN"),
                })
            scoped.append(result)
        analysis[key] = scoped[0] if key == "main_market_driver" and scoped else (None if key == "main_market_driver" else scoped)
    return analysis


def run_market_driver_pipeline(
    market_date: str,
    output_dir: Path,
    *,
    refresh_verification: bool = False,
    refresh_discovery: bool = False,
    logger: logging.Logger | None = None,
) -> Path:
    """Collect only mover-required Finnhub evidence and write driver state."""
    logger = logger or logging.getLogger("market_bot.market_drivers")
    started = time.monotonic()
    market = json.loads((output_dir / "market_snapshot.json").read_text(encoding="utf-8"))
    movers = json.loads((output_dir / "movers_snapshot.json").read_text(encoding="utf-8"))
    report_data = {"market_date": market_date, "integrity_status": market.get("integrity_status", ""), "integrity_violation": bool(market.get("integrity_violation", False)), "market": market.get("market", {}), "macro": market.get("market", {}).get("macro", {}), "sectors": market.get("market", {}).get("sectors", {}), "movers": {"top_gainers": movers.get("top_gainers", []), "top_losers": movers.get("top_losers", [])}}
    hypotheses = build_driver_analysis(report_data)
    stock_hypotheses = hypotheses.get("stock_catalysts", [])
    end_date = (date.fromisoformat(market_date) + timedelta(days=1)).isoformat()
    provider = FinnhubProvider(token=os.getenv("FINNHUB_API_KEY", ""), refresh_cache=refresh_discovery)
    candidates: list[dict[str, Any]] = []
    sector_raw_candidates: list[dict[str, Any]] = []
    selected_clusters: dict[str, dict[str, Any]] = {}
    for driver in stock_hypotheses:
        symbol = str(driver.get("related_symbols", [""])[0]).upper()
        articles = provider.search_company(symbol, market_date, end_date)
        sector_raw_candidates.extend({**article.as_dict(), "cache_symbols": [symbol]} for article in articles)
        clusters = cluster_finnhub_candidates(articles, symbol, str(next((item.get("company_name") for group in ("top_gainers", "top_losers") for item in movers.get(group, []) if item.get("symbol") == symbol), ""))) if articles else []
        if clusters:
            move_values = driver.get("movement_evidence", [])
            move_pct = move_values[0].get("value") if move_values and isinstance(move_values[0], dict) else None
            selected_cluster = _select_cluster(clusters, symbol, market_date, move_pct)
            if selected_cluster is None:
                continue
            selected_clusters[symbol] = selected_cluster
            candidate = _candidate_from_cluster(selected_cluster, symbol, market_date)
            candidate.update(evaluate_causal_eligibility(candidate, market_date))
            action = candidate.get("analyst_action")
            if candidate.get("event_type") == "ANALYST_ACTION" and action and not analyst_action_direction_compatible(action, move_pct):
                candidate.update({"causal_gate": "FAIL", "causal_eligible": False, "causal_reason": "ANALYST_ACTION_DIRECTION_CONFLICT"})
            candidates.append(candidate)
    acquirer = LiveEvidenceAcquirer(logger, market_date, refresh_cache=refresh_verification, fmp_provider=_DisabledFMP())
    verified: list[dict[str, Any]] = []
    verification_records: list[dict[str, Any]] = []
    if candidates:
        acquired = acquirer.acquire(candidates)
        for candidate in candidates:
            evidence, diagnostics = acquired.get(candidate["title"], ([], None))
            result, verification = verify_candidate(candidate, evidence)
            if result.get("verification") == "VERIFIED":
                verified.append(result)
            verification_records.append({"symbol": candidate["target_symbol"], "event_type": candidate["event_type"], "route": diagnostics.verification_route if diagnostics else [], "attempted_sources": diagnostics.attempted_sources if diagnostics else [], "decision": result.get("verification"), "matched_source_type": verification.matched_source_type, "matched_domain": verification.matched_domain})
    sector_candidates = build_sector_evidence_pool(sector_raw_candidates, market_date)
    analysis = _scope_analysis(build_driver_analysis({**report_data, "candidate_stories": candidates, "sector_candidate_stories": sector_candidates, "verified_stories": verified}), candidates, verified)
    all_drivers = [*analysis.get("stock_catalysts", []), *analysis.get("sector_drivers", [])]
    if analysis.get("main_market_driver"):
        all_drivers.append(analysis["main_market_driver"])
    metrics = {
        "meaningful_moves": len(stock_hypotheses),
        "confirmed": sum(item.get("verification_status") == "CONFIRMED" for item in all_drivers),
        "plausible": sum(item.get("verification_status") == "PLAUSIBLE_NOT_VERIFIED" for item in analysis.get("stock_catalysts", [])),
        "unexplained": sum(item.get("verification_status") == "NO_CATALYST_FOUND" for item in analysis.get("stock_catalysts", [])),
        "finnhub_live_requests": provider.live_requests,
        "finnhub_cache_hits": provider.cache_hits,
        "finnhub_cache_misses": provider.cache_misses,
        "marketaux_requests": 0,
        "regulator_requests": sum(1 for record in verification_records for item in record["attempted_sources"] if str(item.get("type", "")).startswith("REGULATOR_OFFICIAL") and item.get("status") not in {"CACHE_HIT", "CACHE_MISS"}),
        "official_requests": sum(1 for record in verification_records for item in record["attempted_sources"] if str(item.get("type", "")).startswith(("OFFICIAL", "SEC_EDGAR", "REGULATOR_OFFICIAL")) and item.get("status") not in {"CACHE_HIT", "CACHE_MISS"}),
        "tier2_requests": sum(1 for record in verification_records for item in record["attempted_sources"] if str(item.get("type", "")).startswith("TIER2") and item.get("status") not in {"CACHE_HIT", "CACHE_MISS"}),
        "fmp_requests": 0,
        "verification_candidates": len(candidates),
        "verification_confirmed": len(verified),
        "verification_not_confirmed": len(candidates) - len(verified),
        "causal_candidates_passed": sum(bool(item.get("causal_eligible")) for item in candidates),
        "causal_candidates_rejected_commentary": sum(item.get("causal_reason") in {"COMMENTARY_ONLY", "REACTION_TO_MOVE"} for item in candidates),
        "causal_candidates_rejected_timing": sum(item.get("causal_reason") == "TIMING_INELIGIBLE" for item in candidates),
        "moves_without_causal_candidate": sum(1 for item in analysis.get("stock_catalysts", []) if item.get("causal_gate") != "PASS"),
        "runtime_seconds": round(time.monotonic() - started, 3),
    }
    cluster_summaries = {
        symbol: {key: cluster.get(key) for key in ("event_cluster_id", "event_signature", "canonical_event_title", "event_type", "actors", "topic_family", "event_actions", "target_symbol", "target_role", "analyst_action", "article_count", "independent_source_count", "sources")}
        for symbol, cluster in selected_clusters.items()
    }
    payload = {"market_date": market_date, "generated_at": time.time(), "market_drivers_source_of_truth": True, "legacy_catalyst_used_for_driver": False, "driver_analysis": analysis, "candidate_stories": candidates, "verified_stories": verified, "verification_records": verification_records, "metrics": metrics, "selected_clusters": cluster_summaries}
    path = output_dir / "market_driver_snapshot.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("Market Drivers snapshot created: %s", path)
    return path
