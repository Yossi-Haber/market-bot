"""Offline multi-day evaluation of deterministic market-driver output."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

import requests

from drivers import build_driver_analysis
from drivers.causal import publication_time_relation
from drivers.detector import classify_driver
from drivers.market_driver_discovery import load_market_discovery_cache_state
from drivers.model import Driver
from drivers.sector_evidence import build_sector_evidence_pool
from cache_config import cache_path
from news.providers.finnhub import FinnhubProvider, cluster_finnhub_candidates
from news.schema import NewsCandidate
from news.verification_acquisition import LiveEvidenceAcquirer
from news.verification_v2 import verify_candidate

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
CACHE_DIR = cache_path()
SIGNIFICANT_MOVE_PCT = 2.0


def to_json_safe(value: Any) -> Any:
    """Recursively convert evaluator data into JSON-compatible primitives.

    This is intentionally local to the evaluator; production news dataclasses
    and clustering records remain unchanged.
    """
    if isinstance(value, Enum):
        return to_json_safe(value.value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_json_safe(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): to_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_json_safe(item) for item in value]
    raise TypeError(f"Unsupported evaluator output type: {type(value).__name__}")


@contextmanager
def offline_network_guard() -> Iterator[None]:
    """Fail loudly if evaluation code attempts a network request."""
    def blocked(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("OFFLINE_NETWORK_CALL_BLOCKED")

    original = (requests.request, requests.get, requests.post, requests.Session.request)
    requests.request = blocked
    requests.get = blocked
    requests.post = blocked
    requests.Session.request = blocked
    try:
        yield
    finally:
        requests.request, requests.get, requests.post, requests.Session.request = original


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _load_json_any(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _dated_snapshot(directory: Path, prefix: str, market_date: str) -> dict[str, Any] | None:
    history = directory / "history" / market_date / f"{prefix}.json"
    historical_payload = _load_json(history)
    if historical_payload and historical_payload.get("market_date") == market_date:
        return historical_payload
    candidates: list[Path] = []
    for path in directory.glob(f"{prefix}_*.json"):
        payload = _load_json_any(path)
        if payload and payload.get("market_date") == market_date:
            candidates.append(path)
    current = directory / f"{prefix}.json"
    payload = _load_json(current)
    if payload and payload.get("market_date") == market_date:
        candidates.append(current)
    return _load_json(max(candidates, key=lambda path: path.stat().st_mtime)) if candidates else None


def _load_dated_file(directory: Path, market_date: str, filename: str) -> dict[str, Any] | None:
    path = directory / market_date / filename
    return _load_json(path) if path.exists() else None


def _news_dates(market_date: str) -> list[str]:
    """Use the production market date plus its post-close calendar day."""
    current = date.fromisoformat(market_date)
    return [current.isoformat(), (current + timedelta(days=1)).isoformat()]


def _cache_file_count(root: Path, dates: list[str]) -> int:
    return sum(len(list((root / value).rglob("*.json"))) for value in dates if (root / value).exists())


def _finnhub_cache_files(market_date: str, symbols: set[str] | None = None) -> list[Path]:
    start, end = market_date, (date.fromisoformat(market_date) + timedelta(days=1)).isoformat()
    root = CACHE_DIR / "finnhub"
    if not root.exists():
        return []
    paths = []
    for symbol_dir in root.iterdir():
        if not symbol_dir.is_dir() or (symbols and symbol_dir.name.upper() not in symbols):
            continue
        path = symbol_dir / f"{start}_{end}.json"
        if path.exists():
            paths.append(path)
    return paths


def _finnhub_cache_path(symbol: str, market_date: str) -> Path:
    end = (date.fromisoformat(market_date) + timedelta(days=1)).isoformat()
    return CACHE_DIR / "finnhub" / symbol.upper() / f"{market_date}_{end}.json"


def _validate_finnhub_cache(path: Path, symbol: str, start_date: str, end_date: str) -> tuple[str, str, dict[str, Any] | None]:
    """Validate persisted Finnhub cache identity before replay."""
    if not path.exists():
        return "MISSING", "file_not_found", None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "PARSE_ERROR", "invalid_json", None
    if not isinstance(payload, dict) or payload.get("schema_version") != "finnhub-discovery-v1" or payload.get("provider") != "FINNHUB":
        return "INVALID_SCHEMA", "schema_or_provider_mismatch", payload if isinstance(payload, dict) else None
    if payload.get("symbol") != symbol.upper() or payload.get("requested_from_date") != start_date or payload.get("requested_to_date") != end_date:
        return "INVALID_WINDOW", "symbol_or_requested_window_mismatch", payload
    if not isinstance(payload.get("normalized_candidates"), list):
        return "INVALID_SCHEMA", "normalized_candidates_missing", payload
    return "AVAILABLE", "valid_v1_cache", payload


def _cache_coverage(market_date: str, output_dir: Path) -> dict[str, str]:
    """Describe cache availability without treating a directory as consumed data."""
    news_dates = _news_dates(market_date)
    checks = {
        "market_data": bool(_dated_snapshot(output_dir, "market_snapshot", market_date)),
        "movers": bool(_dated_snapshot(output_dir, "movers_snapshot", market_date)),
        "finnhub": bool(_finnhub_cache_files(market_date)),
        "marketaux": _cache_file_count(CACHE_DIR / "marketaux", news_dates) > 0,
        "official": _cache_file_count(CACHE_DIR / "official_news", news_dates) > 0,
        "tier2": _cache_file_count(CACHE_DIR / "verification", news_dates) > 0,
        "verification": _cache_file_count(CACHE_DIR / "verification_v2", news_dates) > 0,
    }
    return {key: "AVAILABLE" if value else "MISSING" for key, value in checks.items()}


def _candidate_from_cached_cluster(symbol: str, result: dict[str, Any]) -> dict[str, Any] | None:
    cluster = result.get("event_cluster") if isinstance(result.get("event_cluster"), dict) else {}
    headline = result.get("headline") or cluster.get("canonical_event") or cluster.get("canonical_event_title")
    if not headline and not any(cluster.get(key) for key in ("candidate_sources", "independent_sources", "article_count", "canonical_event_title")):
        return None
    return {
        "title": headline or "Cached event cluster",
        "summary": (result.get("summary_facts") or [None])[0],
        "affected_symbols": [symbol.upper()],
        "target_symbol": symbol.upper(),
        "event_cluster": cluster.get("underlying_event_id") or cluster.get("event_cluster_id") or cluster.get("event_signature"),
        "event_type": result.get("catalyst_type") or cluster.get("event_type"),
        "canonical_event": cluster.get("canonical_event_title") or headline,
        "independent_sources": cluster.get("independent_sources", []),
        "independent_sources_count": result.get("independent_source_count", cluster.get("independent_source_count", 0)),
        "article_count": cluster.get("article_count", 0),
        "selection_score": cluster.get("selection_score"),
        "target_role": cluster.get("target_role"),
        "verification": "VERIFIED" if result.get("catalyst_status") == "CONFIRMED" else "NOT_CONFIRMED",
        "editorial_eligible": result.get("catalyst_status") == "CONFIRMED",
        "source_name": (result.get("primary_source") or {}).get("source_name") if isinstance(result.get("primary_source"), dict) else None,
    }


def _is_time_eligible(story: dict[str, Any], market_date: str) -> bool:
    """Unknown or post-as-of publication times cannot support historical replay."""
    published = story.get("published_at") or story.get("published") or story.get("datetime")
    return publication_time_relation(published, market_date) == "BEFORE_OR_DURING_SESSION"


def _load_cached_news_context(market_date: str, movers: dict[str, Any], report: dict[str, Any] | None) -> dict[str, Any]:
    """Replay cached catalyst records actually associated with selected movers."""
    symbols = {str(item.get("symbol", "")).upper() for group in ("top_gainers", "top_losers") for item in movers.get(group, []) if isinstance(item, dict)}
    company_names = {
        str(item.get("symbol", "")).upper(): str(item.get("company_name", ""))
        for group in ("top_gainers", "top_losers")
        for item in movers.get(group, [])
        if isinstance(item, dict) and item.get("symbol")
    }
    candidates: list[dict[str, Any]] = []
    sector_raw_candidates: list[dict[str, Any]] = []
    verified: list[dict[str, Any]] = []
    provider_metrics = {"marketaux_candidates_used": 0, "finnhub_candidates_used": 0, "official_evidence_used": 0, "tier2_evidence_used": 0}
    clusters: dict[str, list[dict[str, Any]]] = {}
    finnhub_cache: dict[str, dict[str, Any]] = {}
    timing_diagnostics: dict[str, dict[str, int]] = {}
    for symbol in sorted(symbols):
        cache_path = _finnhub_cache_path(symbol, market_date)
        expected_end = (date.fromisoformat(market_date) + timedelta(days=1)).isoformat()
        cache_status, cache_reason, cache_payload = _validate_finnhub_cache(cache_path, symbol, market_date, expected_end)
        finnhub_result = _load_finnhub_cache(symbol, market_date, company_names.get(symbol, ""))
        if cache_status == "AVAILABLE":
            raw_candidates = cache_payload.get("normalized_candidates", [])
            for raw_candidate in raw_candidates:
                if isinstance(raw_candidate, dict) and publication_time_relation(raw_candidate.get("published_at"), market_date) == "BEFORE_OR_DURING_SESSION":
                    sector_raw_candidates.append({**raw_candidate, "cache_symbols": [symbol]})
            eligible_count = future_count = invalid_time_count = 0
            target_candidates = [raw_candidate for raw_candidate in raw_candidates if isinstance(raw_candidate, dict) and symbol in {str(value).upper() for value in raw_candidate.get("affected_symbols", [])}]
            for raw_candidate in target_candidates:
                published = raw_candidate.get("published_at") if isinstance(raw_candidate, dict) else None
                relation = publication_time_relation(published, market_date)
                if relation == "UNKNOWN":
                    invalid_time_count += 1
                elif relation == "BEFORE_OR_DURING_SESSION":
                    eligible_count += 1
                else:
                    future_count += 1
            timing_diagnostics[symbol] = {"time_eligible_article_count": eligible_count, "future_article_count": future_count, "invalid_time_article_count": invalid_time_count, "target_article_count": len(target_candidates)}
            clusters[symbol] = finnhub_result
            finnhub_cache[symbol] = {"status": cache_status, "file": str(cache_path), "exists": True, "schema": cache_payload.get("schema_version"), "expected_schema": "finnhub-discovery-v1", "requested_window": f"{market_date}_{expected_end}", "stored_window": f"{cache_payload.get('requested_from_date')}_{cache_payload.get('requested_to_date')}", "validation_reason": cache_reason, "cached_articles": len(raw_candidates), **timing_diagnostics[symbol]}
            provider_metrics["finnhub_candidates_used"] += sum(int(cluster.get("article_count", 0) or 0) for cluster in finnhub_result)
            candidates.extend(_cluster_story(symbol, cluster) for cluster in finnhub_result)
        else:
            finnhub_cache[symbol] = {"status": cache_status, "file": str(cache_path), "exists": cache_path.exists(), "schema": cache_payload.get("schema_version") if cache_payload else None, "expected_schema": "finnhub-discovery-v1", "requested_window": f"{market_date}_{expected_end}", "stored_window": f"{cache_payload.get('requested_from_date')}_{cache_payload.get('requested_to_date')}" if cache_payload else None, "validation_reason": cache_reason, "cached_articles": 0}
        result = None
        for news_date in _news_dates(market_date):
            result = _load_dated_file(CACHE_DIR / "news", news_date, f"{symbol}.json")
            if result is not None:
                break
        if not result:
            continue
        if not _is_time_eligible(result, market_date):
            continue
        audit = result.get("discovery_audit", {}) if isinstance(result.get("discovery_audit"), dict) else {}
        provider_metrics["marketaux_candidates_used"] += int(audit.get("marketaux_candidates", 0) or 0)
        provider_metrics["finnhub_candidates_used"] += int(audit.get("finnhub_candidates", 0) or 0)
        provider_metrics["official_evidence_used"] += int(result.get("verification", {}).get("official_source_found", False))
        provider_metrics["tier2_evidence_used"] += int(result.get("verification", {}).get("tier2_source_found", False))
        story = _candidate_from_cached_cluster(symbol, result)
        if story:
            candidates.append(story)
            if story["verification"] == "VERIFIED":
                verified.append(story)
            clusters[symbol] = [story]
    # Existing report stories remain part of the replay; cached catalyst records
    # supplement them rather than replacing them.
    report = report or {}
    for story in report.get("verified_stories", report.get("top_stories", [])):
        if isinstance(story, dict) and _is_time_eligible(story, market_date):
            verified.append(story)
    return {"candidate_stories": candidates, "sector_candidate_stories": build_sector_evidence_pool(sector_raw_candidates, market_date), "verified_stories": verified, "provider_metrics": provider_metrics, "clusters": clusters, "finnhub_cache": finnhub_cache, "timing_diagnostics": timing_diagnostics}


def _replay_cached_verification(market_date: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Run production acquisition/verification logic against caches only."""
    class _OfflineFMP:
        enabled = False
        news_requests = 0
        press_release_requests = 0
        evidence_matches = 0
        no_match = 0
        errors = 0

        def search_press_releases(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return []

    acquirer = LiveEvidenceAcquirer(logging.getLogger("market_driver_evaluation"), market_date, request_get=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("OFFLINE_NETWORK_CALL_BLOCKED")), max_requests=10, cache_only=True, fmp_provider=_OfflineFMP())
    considered = len(candidates)
    selected = candidates[:5]
    verified_stories: list[dict[str, Any]] = []
    results: dict[str, dict[str, Any]] = {}
    evidence_attempts = 0
    official_attempts = official_hits = tier2_attempts = tier2_hits = 0
    not_confirmed = 0
    replay_records: list[dict[str, Any]] = []
    for candidate in selected:
        acquired = acquirer.acquire([candidate])
        evidence, diagnostics = acquired.get(candidate.get("title", ""), ([], None))
        diagnostics_dict = diagnostics.as_dict() if diagnostics is not None else {"attempted_sources": [], "reason": "no_diagnostics"}
        attempts = diagnostics_dict.get("attempted_sources", [])
        evidence_attempts += len(attempts)
        official_attempts += sum(1 for item in attempts if str(item.get("type", "")).startswith(("OFFICIAL", "REGULATOR_OFFICIAL")) or item.get("type") == "SEC_EDGAR")
        tier2_attempts += sum(1 for item in attempts if str(item.get("type", "")).startswith("TIER2"))
        official_hits += sum(1 for item in evidence if str(item.get("source_type", "")).upper() == "OFFICIAL")
        tier2_hits += sum(1 for item in evidence if str(item.get("source_type", "")).upper() == "TIER2")
        result, verification_diagnostics = verify_candidate(candidate, evidence)
        results[candidate.get("title", "")] = result
        if result.get("verification") == "VERIFIED":
            verified_stories.append(result)
        else:
            not_confirmed += 1
        replay_records.append({"candidate": candidate.get("title"), "affected_symbols": candidate.get("affected_symbols", []), "event_type": candidate.get("event_type") or candidate.get("category"), "actors": diagnostics_dict.get("actors", []), "verification_route": diagnostics_dict.get("verification_route", []), "attempted_sources": attempts, "evidence_matches": len(evidence), "official_evidence_matches": sum(1 for item in evidence if str(item.get("source_type", "")).upper() == "OFFICIAL"), "tier2_evidence_matches": sum(1 for item in evidence if str(item.get("source_type", "")).upper() == "TIER2"), "decision": result.get("verification"), "reason": verification_diagnostics.reason})
    return {
        "results": results,
        "verified_stories": verified_stories,
        "verification_candidates_considered": considered,
        "verification_candidates_selected": len(selected),
        "verification_evidence_attempts": evidence_attempts,
        "verification_cache_hits": acquirer.cache_hits,
        "verification_confirmed": len(verified_stories),
        "verification_not_confirmed": not_confirmed,
        "official_evidence_attempts": official_attempts,
        "official_evidence_hits": official_hits,
        "tier2_evidence_attempts": tier2_attempts,
        "tier2_evidence_hits": tier2_hits,
        "verification_precheck_rejections": max(0, considered - len(selected)),
        "offline_network_attempts_blocked": 0,
        "records": replay_records,
    }


def _load_finnhub_cache(symbol: str, market_date: str, company_name: str = "") -> list[dict[str, Any]]:
    """Cluster only cache articles published on or before the evaluation date."""
    start, end = market_date, (date.fromisoformat(market_date) + timedelta(days=1)).isoformat()
    path = CACHE_DIR / "finnhub" / symbol.upper() / f"{start}_{end}.json"
    status, _, payload = _validate_finnhub_cache(path, symbol, start, end)
    if status != "AVAILABLE" or not payload:
        return []
    try:
        normalized = [NewsCandidate(**item) for item in payload["normalized_candidates"] if isinstance(item, dict) and symbol.upper() in {str(value).upper() for value in item.get("affected_symbols", [])}]
    except (TypeError, ValueError):
        return []
    eligible = [item for item in normalized if publication_time_relation(item.published_at, market_date) == "BEFORE_OR_DURING_SESSION"]
    return cluster_finnhub_candidates(eligible, symbol, company_name)


def _parse_candidate_date(value: Any) -> date | None:
    """Parse provider timestamps to a date; unknown timestamps are ineligible."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None


def _cluster_story(symbol: str, cluster: dict[str, Any]) -> dict[str, Any]:
    """Expose a Finnhub cluster as an unverified discovery candidate."""
    best = cluster.get("best_candidate")
    published_at = getattr(best, "published_at", None)
    source_url = getattr(best, "source_url", None)
    source_name = getattr(best, "source_name", None)
    return {
        "title": cluster.get("canonical_event_title"),
        "summary": None,
        "published_at": published_at,
        "affected_symbols": [symbol],
        "target_symbol": symbol.upper(),
        "category": cluster.get("event_type"),
        "event_cluster": cluster.get("event_cluster_id"),
        "canonical_event": cluster.get("canonical_event_title"),
        "event_type": cluster.get("event_type"),
        "actors": cluster.get("actors", []),
        "event_actions": cluster.get("event_actions", []),
        "analyst_action": cluster.get("analyst_action"),
        "target_role": cluster.get("target_role"),
        "independent_sources": cluster.get("sources", []),
        "independent_sources_count": cluster.get("independent_source_count", 0),
        "article_count": cluster.get("article_count", 0),
        "selection_score": cluster.get("selection_score"),
        "verification": "NOT_CONFIRMED",
        "editorial_eligible": False,
        "discovery_source": "finnhub",
        "source_url": source_url,
        "source_name": source_name,
    }


def discover_cached_dates(output_dir: Path, start: str, end: str) -> tuple[list[str], list[str]]:
    """Return weekday dates with market+movers cache and explicit missing dates."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    available, missing = [], []
    current = first
    while current <= last:
        if current.weekday() < 5:
            value = current.isoformat()
            if _dated_snapshot(output_dir, "market_snapshot", value) and _dated_snapshot(output_dir, "movers_snapshot", value):
                available.append(value)
            else:
                missing.append(value)
        current += timedelta(days=1)
    return available, missing


def _driver_input(market: dict[str, Any], movers: dict[str, Any], report: dict[str, Any] | None, news_context: dict[str, Any] | None = None) -> dict[str, Any]:
    report = report or {}
    news_context = news_context or {}
    market_date = str(market.get("market_date") or report.get("market_date") or "")
    report_verified = [item for item in report.get("verified_stories", report.get("top_stories", [])) if isinstance(item, dict) and (not market_date or _is_time_eligible(item, market_date))]
    report_candidates = [item for item in report.get("candidate_stories", []) if isinstance(item, dict) and (not market_date or _is_time_eligible(item, market_date))]
    return {"market_date": market_date, "integrity_status": report.get("integrity_status", market.get("integrity_status", "")), "integrity_violation": report.get("integrity_violation", market.get("integrity_violation", False)), "market": market.get("market", {}), "macro": report.get("macro", market.get("market", {}).get("macro", {})), "sectors": report.get("sectors", market.get("market", {}).get("sectors", {})), "movers": {"top_gainers": movers.get("top_gainers", []), "top_losers": movers.get("top_losers", [])}, "verified_stories": [*report_verified, *news_context.get("verified_stories", [])], "candidate_stories": [*report_candidates, *news_context.get("candidate_stories", [])], "sector_candidate_stories": news_context.get("sector_candidate_stories", []), "market_candidate_stories": [*report.get("market_candidate_stories", []), *news_context.get("market_candidate_stories", [])]}


def _story_matches_driver(story: dict[str, Any], driver: dict[str, Any]) -> bool:
    explicit_target = story.get("target_symbol") or story.get("symbol")
    target_symbols = {str(value).upper() for value in driver.get("related_symbols", [])}
    if explicit_target:
        return str(explicit_target).upper() in target_symbols
    candidate_symbols: set[str] = set()
    for key in ("affected_symbols", "candidate_symbols", "symbols"):
        values = story.get(key, [])
        if isinstance(values, str):
            values = [values]
        candidate_symbols.update(str(value).upper() for value in values or [])
    for key in ("target_symbol", "symbol"):
        if story.get(key):
            candidate_symbols.add(str(story[key]).upper())
    if story.get("target_symbol") or story.get("symbol"):
        return bool(candidate_symbols & target_symbols)
    direct_subjects = {str(value).upper() for value in story.get("direct_subject_symbols", [])}
    if direct_subjects:
        return bool(direct_subjects & target_symbols)
    affected = story.get("affected_symbols", story.get("candidate_symbols", story.get("symbols", [])))
    if isinstance(affected, str):
        affected = [affected]
    normalized = {str(value).upper() for value in affected or []}
    if len(normalized) > 1:
        return False
    return bool(candidate_symbols & target_symbols)


def _reconcile_driver_scope(analysis: dict[str, Any], candidate_stories: list[dict[str, Any]], verified_stories: list[dict[str, Any]], market_candidate_stories: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], list[str]]:
    """Reapply the production classifier to symbol-scoped stories only."""
    warnings: list[str] = []
    for key in ("main_market_driver", "sector_drivers", "stock_catalysts"):
        if key == "main_market_driver":
            values = [analysis[key]] if analysis.get(key) else []
        else:
            values = analysis.get(key) or []
        updated: list[dict[str, Any]] = []
        for item in values:
            if key == "sector_drivers":
                # Sector promotion already used its isolated cached-evidence
                # pool in build_driver_analysis. Reapplying ticker-scoped
                # classification here would erase broad sector evidence.
                updated.append(dict(item))
                continue
            candidate_pool = market_candidate_stories if key == "main_market_driver" and market_candidate_stories is not None else candidate_stories
            relevant_candidates = list(candidate_pool) if key == "main_market_driver" and market_candidate_stories is not None else [story for story in candidate_pool if _story_matches_driver(story, item)]
            relevant_verified = list(market_candidate_stories or []) if key == "main_market_driver" and market_candidate_stories is not None else [story for story in verified_stories if _story_matches_driver(story, item)]
            if len(relevant_candidates) != len(candidate_stories) and item.get("driver_id", "").startswith("STOCK_") and candidate_stories:
                warnings.append("CROSS_SYMBOL_CANDIDATE_BLOCKED")
            if key == "main_market_driver":
                # Market-driver status was decided by the dedicated market
                # classifier; do not run the stock Driver classifier here.
                classified = dict(item)
                classified["candidate_stories"] = relevant_candidates
                if relevant_verified:
                    classified["verified_story"] = relevant_verified[0]
                updated.append(classified)
                continue
            required_fields = {"driver_id", "driver_type", "scope", "direction", "strength"}
            if required_fields.issubset(item):
                try:
                    driver = Driver(**item)
                except TypeError:
                    driver = None
                if driver is not None:
                    classified = classify_driver(driver, relevant_candidates, relevant_verified).as_dict()
                else:
                    classified = dict(item)
                    classified["candidate_stories"] = relevant_candidates
                    classified["verification_status"] = "CONFIRMED" if relevant_verified else ("PLAUSIBLE_NOT_VERIFIED" if relevant_candidates else "NO_CATALYST_FOUND")
                    classified["confidence_reason"] = "Movement evidence is supported by an independently verified relevant story." if relevant_verified else ("A relevant discovery candidate exists, but it is not independently verified." if relevant_candidates else "Movement evidence exists, but no relevant candidate story was found.")
            else:
                # Keep evaluator compatibility with deliberately minimal mocked
                # analysis records without changing the production Driver model.
                classified = dict(item)
                classified["candidate_stories"] = relevant_candidates
                if relevant_verified:
                    classified["verification_status"] = "CONFIRMED"
                    classified["confidence_reason"] = "Movement evidence is supported by an independently verified relevant story."
                elif relevant_candidates:
                    classified["verification_status"] = "PLAUSIBLE_NOT_VERIFIED"
                    classified["confidence_reason"] = "A relevant discovery candidate exists, but it is not independently verified."
                else:
                    classified["verification_status"] = "NO_CATALYST_FOUND"
                    classified["confidence_reason"] = "Movement evidence exists, but no relevant candidate story was found."
            updated.append(classified)
        if key == "main_market_driver":
            analysis[key] = updated[0] if updated else None
        else:
            analysis[key] = updated
    return analysis, warnings


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _driver_stories(driver: dict[str, Any]) -> list[dict[str, Any]]:
    stories = []
    if isinstance(driver.get("verified_story"), dict):
        stories.append(driver["verified_story"])
    stories.extend(item for item in driver.get("candidate_stories", []) if isinstance(item, dict))
    return stories


def _story_symbols(story: dict[str, Any]) -> set[str]:
    values = story.get("affected_symbols", story.get("symbols", []))
    if isinstance(values, str):
        values = [values]
    return {str(value).upper() for value in values or []}


def _driver_move(driver: dict[str, Any]) -> float | None:
    evidence = driver.get("movement_evidence")
    if isinstance(evidence, list) and evidence:
        return _as_float(evidence[0].get("value")) if isinstance(evidence[0], dict) else None
    return None


def _cached_provider_metrics(report: dict[str, Any] | None) -> dict[str, int]:
    """Extract already-recorded provider counters without making provider calls."""
    totals = {"marketaux_candidates_used": 0, "finnhub_candidates_used": 0, "official_evidence_used": 0, "tier2_evidence_used": 0}
    if not report:
        return totals

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                name = str(key).lower()
                if isinstance(item, (int, float)) and not isinstance(item, bool):
                    if "marketaux" in name and ("candidate" in name or "article" in name):
                        totals["marketaux_candidates_used"] += int(item)
                    if "finnhub" in name and ("candidate" in name or "article" in name):
                        totals["finnhub_candidates_used"] += int(item)
                    if "official" in name and "evidence" in name:
                        totals["official_evidence_used"] += int(item)
                    if "tier2" in name and "evidence" in name:
                        totals["tier2_evidence_used"] += int(item)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(report)
    return totals


def _evaluate_day(output_dir: Path, market_date: str) -> dict[str, Any] | None:
    market = _dated_snapshot(output_dir, "market_snapshot", market_date)
    movers = _dated_snapshot(output_dir, "movers_snapshot", market_date)
    report = _dated_snapshot(output_dir, "daily_report_data", market_date)
    if not market or not movers:
        return None
    news_context = _load_cached_news_context(market_date, movers, report)
    verification_replay = _replay_cached_verification(market_date, news_context["candidate_stories"])
    news_context["verification"] = verification_replay
    news_context["verified_stories"].extend(verification_replay["verified_stories"])
    news_context["provider_metrics"]["official_evidence_used"] = verification_replay["official_evidence_hits"]
    news_context["provider_metrics"]["tier2_evidence_used"] = verification_replay["tier2_evidence_hits"]
    replay_input = _driver_input(market, movers, report, news_context)
    # Market discovery is a separate date-scoped cache. Offline replay may
    # consume it, but never substitutes stock/mover candidates when absent.
    market_cache = load_market_discovery_cache_state(output_dir.parent / "cache", market_date)
    replay_input["market_candidate_stories"] = list(market_cache.get("eligible_candidates", [])) if market_cache["state"] == "HIT" else []
    news_context["market_discovery"] = {
        "state": market_cache["state"],
        "path": market_cache["path"],
        "eligible_market_candidates_loaded": len(replay_input["market_candidate_stories"]),
        "selected_market_candidate": (market_cache.get("diagnostics", {}).get("top_candidates") or [{}])[0].get("title") if market_cache["state"] == "HIT" else None,
        "diagnostics": market_cache.get("diagnostics", {}),
    }
    analysis = build_driver_analysis(replay_input)
    analysis, scope_warnings = _reconcile_driver_scope(analysis, replay_input["candidate_stories"], replay_input["verified_stories"], replay_input.get("market_candidate_stories", []))
    all_drivers = ([analysis["main_market_driver"]] if analysis.get("main_market_driver") else []) + analysis.get("sector_drivers", []) + analysis.get("stock_catalysts", [])
    unique_drivers: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in all_drivers:
        driver_id = str(item.get("driver_id", ""))
        if driver_id in seen_ids:
            continue
        seen_ids.add(driver_id)
        unique_drivers.append(item)
    confirmed = [item for item in unique_drivers if item.get("verification_status") == "CONFIRMED"]
    plausible = [item for item in unique_drivers if item.get("verification_status") == "PLAUSIBLE_NOT_VERIFIED"]
    unexplained = [item for item in unique_drivers if item.get("verification_status") == "NO_CATALYST_FOUND"]
    warnings: list[str] = []
    warnings.extend(scope_warnings)
    for item in unique_drivers:
        if not item.get("movement_evidence"):
            warnings.append("DRIVER_WITHOUT_MOVEMENT_EVIDENCE")
        related = {str(symbol).upper() for symbol in item.get("related_symbols", [])}
        for story in _driver_stories(item):
            if related and not related.intersection(_story_symbols(story)):
                warnings.append("DRIVER_STORY_NOT_RELEVANT")
                if item.get("driver_id", "").startswith("SECTOR_"):
                    warnings.append("SECTOR_STORY_SYMBOL_MISMATCH")
                if item.get("driver_id", "").startswith("STOCK_"):
                    warnings.append("STOCK_CATALYST_SUBJECT_MISMATCH")
        move = _driver_move(item)
        if item.get("driver_id", "").startswith("STOCK_") and move is not None and abs(move) >= SIGNIFICANT_MOVE_PCT and item.get("verification_status") == "NO_CATALYST_FOUND":
            warnings.append("UNEXPLAINED_LARGE_MOVE")
        if item.get("driver_id", "").startswith("STOCK_") and item.get("verification_status") == "NO_CATALYST_FOUND":
            warnings.append("STOCK_CATALYST_SUBJECT_MISMATCH")
    if len(seen_ids) != len(all_drivers):
        warnings.append("DUPLICATE_DRIVER_EVENT")
    core = market.get("market", {}).get("core", {})
    market_values = {symbol: {"close": core.get(symbol, {}).get("close"), "change_pct": core.get(symbol, {}).get("change_pct")} for symbol in ("SPY", "QQQ", "DIA", "IWM") if isinstance(core.get(symbol), dict)}
    return {"market_date": market_date, "market_values": market_values, "cache_coverage": _cache_coverage(market_date, output_dir), "news_context": news_context, "main_market_driver": analysis.get("main_market_driver"), "sector_drivers": analysis.get("sector_drivers", []), "stock_catalysts": analysis.get("stock_catalysts", []), "confirmed_drivers": len(confirmed), "plausible_drivers": len(plausible), "unexplained_moves": len(unexplained), "warnings": sorted(set(warnings)), "metrics": {**analysis.get("metrics", {}), **{key: value for key, value in verification_replay.items() if key not in {"results", "verified_stories", "records"}}}}


def evaluate_market_drivers(start: str, end: str, output_dir: Path = OUTPUT_DIR, trace_symbol: str | None = None) -> dict[str, Any]:
    """Evaluate only local snapshots, with a hard network-call guard."""
    with offline_network_guard():
        dates, missing = discover_cached_dates(output_dir, start, end)
        days = [result for value in dates if (result := _evaluate_day(output_dir, value))]
    total_meaningful = sum(len(day["sector_drivers"]) + len(day["stock_catalysts"]) + bool(day.get("main_market_driver")) for day in days)
    confirmed = sum(day["confirmed_drivers"] for day in days)
    plausible = sum(day["plausible_drivers"] for day in days)
    unexplained = sum(day["unexplained_moves"] for day in days)
    main_days = sum(bool(day.get("main_market_driver")) for day in days)
    main_confirmed = sum(bool((day.get("main_market_driver") or {}).get("verification_status") == "CONFIRMED") for day in days)
    sector_total = sum(len(day["sector_drivers"]) for day in days)
    sector_confirmed = sum(sum(item.get("verification_status") == "CONFIRMED" for item in day["sector_drivers"]) for day in days)
    stock_total = sum(len(day["stock_catalysts"]) for day in days)
    stock_confirmed = sum(sum(item.get("verification_status") == "CONFIRMED" for item in day["stock_catalysts"]) for day in days)
    provider_keys = ("marketaux_candidates_used", "finnhub_candidates_used", "official_evidence_used", "tier2_evidence_used")
    provider_metrics = {key: sum(day.get("news_context", {}).get("provider_metrics", {}).get(key, 0) for day in days) for key in provider_keys}
    verified_story_count = sum(len((_dated_snapshot(output_dir, "daily_report_data", value) or {}).get("verified_stories", [])) for value in dates)
    verification_candidate_count = sum(len(day.get("news_context", {}).get("candidate_stories", [])) for day in days) + sum(
        len((_dated_snapshot(output_dir, "daily_report_data", value) or {}).get("candidate_stories", []))
        + len((_dated_snapshot(output_dir, "daily_report_data", value) or {}).get("verified_stories", []))
        for value in dates
    )
    metric_warnings: list[str] = []
    if verified_story_count > verification_candidate_count:
        metric_warnings.append("METRIC_INVARIANT_VIOLATION")
    replay_metrics = {key: sum(day.get("news_context", {}).get("verification", {}).get(key, 0) for day in days) for key in ("verification_candidates_considered", "verification_evidence_attempts", "verification_cache_hits", "verification_confirmed", "verification_not_confirmed", "official_evidence_attempts", "official_evidence_hits", "tier2_evidence_attempts", "tier2_evidence_hits", "verification_precheck_rejections", "offline_network_attempts_blocked")}
    if replay_metrics["verification_candidates_considered"] > 0 and replay_metrics["verification_evidence_attempts"] == 0 and replay_metrics["verification_precheck_rejections"] == 0:
        metric_warnings.append("METRIC_INVARIANT_VIOLATION")
    metrics = {"days_with_confirmed_main_driver": main_confirmed, "days_with_any_confirmed_driver": sum(day["confirmed_drivers"] > 0 for day in days), "total_meaningful_moves": total_meaningful, "moves_with_confirmed_driver": confirmed, "moves_with_plausible_driver": plausible, "moves_unexplained": unexplained, "driver_coverage_rate": confirmed / total_meaningful if total_meaningful else 0.0, "main_driver_coverage_rate": main_confirmed / main_days if main_days else 0.0, "sector_driver_coverage_rate": sector_confirmed / sector_total if sector_total else 0.0, "stock_catalyst_coverage_rate": stock_confirmed / stock_total if stock_total else 0.0, **provider_metrics, **replay_metrics, "verified_story_count": verified_story_count, "verification_candidate_count": verification_candidate_count, "verified_story_yield": verified_story_count / verification_candidate_count if verification_candidate_count else 0.0, "metric_warnings": sorted(set(metric_warnings))}
    result = {"from_date": start, "to_date": end, "cached_dates_detected": dates, "days_evaluated": len(days), "days_skipped_missing_cache": len(missing), "skipped_dates": [{"market_date": value, "status": "SKIPPED_MISSING_CACHE"} for value in missing], "days": days, "cache_coverage": {day["market_date"]: day["cache_coverage"] for day in days}, "metrics": metrics}
    if trace_symbol:
        result["trace"] = _build_trace(result, trace_symbol.upper())
    return result


def _build_trace(result: dict[str, Any], symbol: str) -> dict[str, Any]:
    for day in result.get("days", []):
        for item in day.get("stock_catalysts", []):
            if symbol in {str(value).upper() for value in item.get("related_symbols", [])}:
                stories = day.get("news_context", {}).get("clusters", {}).get(symbol, [])
                cache = day.get("news_context", {}).get("finnhub_cache", {}).get(symbol, {"status": "MISSING", "file": "", "cached_articles": 0})
                replay = day.get("news_context", {}).get("verification", {})
                records = [record for record in replay.get("records", []) if symbol in {str(value).upper() for value in record.get("affected_symbols", [])}]
                first = records[0] if records else {}
                return {"symbol": symbol, "market_date": day["market_date"], "movement": _driver_move(item), "finnhub_cache_status": cache["status"], "finnhub_cache_exists": cache.get("exists", False), "finnhub_cache_schema": cache.get("schema"), "expected_schema": cache.get("expected_schema"), "requested_window": cache.get("requested_window"), "stored_window": cache.get("stored_window"), "cache_validation_reason": cache.get("validation_reason"), "finnhub_cache_file": cache["file"], "cached_articles": cache["cached_articles"], "cached_candidates_found": len(stories), "clusters_created": len(stories), "top_cluster": stories[0] if stories else None, "verification_candidates": len(records) if records else len(stories), "evidence_attempts": sum(len(record.get("attempted_sources", [])) for record in records), "official_cache_hits": sum(record.get("official_evidence_matches", 0) for record in records), "tier2_cache_hits": sum(record.get("tier2_evidence_matches", 0) for record in records), "verification_decision": first.get("decision"), "verification_records": records, "final_status": item.get("verification_status"), "reason": item.get("confidence_reason")}
    return {"symbol": symbol, "movement": None, "hypothesis_created": False, "cached_candidates_found": 0, "clusters_created": 0, "top_cluster": None, "verification_candidates": 0, "evidence_attempts": 0, "final_status": "NOT_FOUND", "reason": "No cached mover hypothesis for symbol"}


def _markdown(result: dict[str, Any]) -> str:
    result = to_json_safe(result)
    lines = ["# Market Drivers Evaluation", "", "| Date | Main Driver | Confirmed? | Sector Drivers | Stock Catalysts | Unexplained Moves |", "|---|---|---:|---:|---:|---:|"]
    for day in result["days"]:
        main = day.get("main_market_driver") or {}
        lines.append(f"| {day['market_date']} | {main.get('driver_id', 'None')} | {'YES' if main.get('verification_status') == 'CONFIRMED' else 'NO'} | {len(day['sector_drivers'])} | {len(day['stock_catalysts'])} | {day['unexplained_moves']} |")
    lines.extend(["", "## TOP REVIEW ITEMS", ""])
    for day in result["days"]:
        for warning in day.get("warnings", [])[:3]:
            lines.append(f"- {day['market_date']}: {warning}")
    lines.extend(["", "## Metrics", "", "```json", json.dumps(result["metrics"], ensure_ascii=False, indent=2), "```", ""])
    return "\n".join(lines)


def _print_day(day: dict[str, Any]) -> None:
    """Print the compact, deterministic review record for one cached day."""
    print(f"market_date={day['market_date']}")
    discovery = day.get("news_context", {}).get("market_discovery", {})
    if discovery:
        print("\nMARKET DISCOVERY CACHE:")
        print(f"market_discovery_cache_state={discovery.get('state')}")
        print(f"market_discovery_cache_path={discovery.get('path')}")
        print(f"eligible_market_candidates_loaded={discovery.get('eligible_market_candidates_loaded', 0)}")
        print(f"selected_market_candidate={discovery.get('selected_market_candidate')}")
    print("\nMARKET:")
    main = day.get("main_market_driver") or {}
    for symbol, values in day.get("market_values", {}).items():
        print(f"{symbol}=close:{values.get('close')} change_pct:{values.get('change_pct')}")
    print("\nMAIN DRIVER:")
    print(f"hypothesis={main.get('driver_id', 'None')}")
    print(f"status={main.get('verification_status', 'NONE')}")
    print(f"supporting_evidence={json.dumps(main.get('movement_evidence', []), ensure_ascii=False)}")
    print(f"verified_story={json.dumps(main.get('verified_story'), ensure_ascii=False)}")
    print(f"confidence_reason={main.get('confidence_reason', '')}")
    print("\nSECTOR DRIVERS:")
    for item in day.get("sector_drivers", []):
        print(f"sector={item.get('related_sector', item.get('scope'))} move={_driver_move(item)} hypothesis={item.get('driver_id')} status={item.get('verification_status')} related_symbols={','.join(item.get('related_symbols', []))} supporting_story={json.dumps(item.get('verified_story'), ensure_ascii=False)}")
    print("\nSTOCK CATALYSTS:")
    for item in day.get("stock_catalysts", []):
        story = item.get("verified_story") or (item.get("candidate_stories") or [{}])[0]
        print(f"symbol={','.join(item.get('related_symbols', []))} move={_driver_move(item)} event_cluster={story.get('event_cluster', '') if isinstance(story, dict) else ''} event_type={story.get('event_type', '') if isinstance(story, dict) else ''} status={item.get('verification_status')} supporting_sources={story.get('supporting_sources_count', story.get('source_name', '')) if isinstance(story, dict) else ''}")
    print(f"\nconfirmed_drivers={day['confirmed_drivers']}\nplausible_drivers={day['plausible_drivers']}\nunexplained_moves={day['unexplained_moves']}")
    if day.get("warnings"):
        print(f"warnings={','.join(day['warnings'])}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-date", required=True)
    parser.add_argument("--to-date", required=True)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--trace-symbol")
    args = parser.parse_args(argv)
    if not args.offline:
        parser.error("--offline is required")
    result = evaluate_market_drivers(args.from_date, args.to_date, trace_symbol=args.trace_symbol)
    json_safe_result = to_json_safe(result)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "market_driver_evaluation.json").write_text(json.dumps(json_safe_result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUTPUT_DIR / "market_driver_evaluation.md").write_text(_markdown(json_safe_result), encoding="utf-8")
    for day in json_safe_result["skipped_dates"]:
        print(f"market_date={day['market_date']}\nSKIPPED_MISSING_CACHE")
    for day in json_safe_result["days"]:
        print("\nCACHE COVERAGE:")
        for key, status in day.get("cache_coverage", {}).items():
            print(f"{key}={status}")
        _print_day(day)
    if json_safe_result.get("trace"):
        trace = json_safe_result["trace"]
        print(f"\nTRACE {trace['symbol']}")
        print("VERIFICATION TRACE:")
        for key in ("verification_candidates", "evidence_attempts", "official_cache_hits", "tier2_cache_hits", "verification_decision", "reason"):
            print(f"{key}={trace.get(key)}")
        for key, value in trace.items():
            if key not in {"symbol", "verification_candidates", "evidence_attempts", "official_cache_hits", "tier2_cache_hits", "verification_decision", "reason"}:
                print(f"{key}={json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value}")
    print(json.dumps(json_safe_result["metrics"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
