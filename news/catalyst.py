"""Evidence-first catalyst analysis for selected movers."""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from news.rank import choose_status
from cache_config import cache_path
from news.alpha_vantage import AlphaVantageClient
from news.discovery import BroadDiscovery, DiscoveryCandidate, deduplicate_candidates
from news.sources import CompanySource, Evidence, RegulatorSource, SecSource, YahooNewsSource, classify_title, has_explicit_market_link
from news.targeted_search import TargetedSearchClient
from news.verify import DirectVerifier

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = cache_path("news")
OUTPUT_DIR = BASE_DIR / "output"
MAX_SLOW_MOVER_FALLBACKS = 3


class CatalystEngine:
    """Collect and rank source metadata without generating prose with an LLM."""

    def __init__(self, logger: logging.Logger, market_date: str) -> None:
        self.logger = logger
        self.market_date = date.fromisoformat(market_date)
        self.sec = SecSource(logger, market_date)
        self.company = CompanySource(logger, market_date)
        self.regulator = RegulatorSource(logger, market_date)
        self.major_news = YahooNewsSource(logger, market_date)
        self.discovery = BroadDiscovery(logger, market_date)
        self.verifier = DirectVerifier(logger, market_date, self.company.mapping)
        self.alpha_vantage = AlphaVantageClient(logger, market_date)
        self.targeted_search = TargetedSearchClient(logger, market_date)
        self.cache_hits = 0
        self.cache_writes = 0
        self.slow_fallbacks_used = 0

    def analyze(self, mover: dict[str, Any]) -> dict[str, Any]:
        symbol, company_name = mover["symbol"], mover["company_name"]
        cached = self._load_cache(symbol)
        if cached is not None:
            self.cache_hits += 1
            return cached
        self.logger.info("Catalyst source search started: %s", symbol)
        sec = self.sec.search(symbol)
        ir, press = self.company.search(symbol)
        regulator = self.regulator.search(symbol)
        major_news = self.major_news.search(symbol, company_name)
        direct_candidates = sec.evidence + ir.evidence + press.evidence + regulator.evidence + major_news.evidence
        slow_needed = not direct_candidates
        self.logger.info("[NEWS] Slow fallback required: %s reason=%s", "YES" if slow_needed and self.slow_fallbacks_used < MAX_SLOW_MOVER_FALLBACKS else "NO", "no_official_or_major_news_candidate" if slow_needed else "candidate_already_available" if direct_candidates else "slow_fallback_cap_reached")
        if slow_needed and self.slow_fallbacks_used < MAX_SLOW_MOVER_FALLBACKS:
            self.slow_fallbacks_used += 1
            discovery_candidates, discovery_coverage, queries = self.discovery.search(symbol, company_name)
            alpha_candidates, alpha_rejected = self.alpha_vantage.search(symbol)
            targeted_candidates, targeted_rejected, targeted_queries, targeted_status = self.targeted_search.search(symbol, company_name, "up" if mover["change_pct"] >= 0 else "down")
        else:
            discovery_candidates, discovery_coverage, queries = [], {"gdelt": "not_attempted", "google_news": "not_attempted"}, []
            alpha_candidates, alpha_rejected = [], []
            targeted_candidates, targeted_rejected, targeted_queries, targeted_status = [], [], [], "not_attempted"
        discovery_candidates = deduplicate_candidates(discovery_candidates + alpha_candidates + targeted_candidates)
        discovery_coverage["alpha_vantage"] = self.alpha_vantage.status
        discovery_coverage["targeted_search"] = targeted_status
        coverage = {"sec": sec.status, "company_ir": ir.status, "company_press_release": press.status, "regulators": regulator.status, "major_news": major_news.status, "broad_discovery": discovery_coverage}
        completed = {"searched", "not_found", "not_applicable"}
        broad_complete = any(state in completed for state in discovery_coverage.values())
        search_complete = sec.status in completed and ir.status in completed and press.status in completed and major_news.status in completed and regulator.status != "failed" and broad_complete
        verification_results = [self.verifier.verify(item) for item in discovery_candidates]
        discovery_evidence = [self._to_evidence(item, verified) for item, verified in zip(discovery_candidates, verification_results)]
        candidates = sec.evidence + ir.evidence + press.evidence + regulator.evidence + major_news.evidence + discovery_evidence
        status, primary, supporting, independent_count = choose_status(candidates, self.market_date, search_complete)
        discovery_audit = {"gdelt_candidates": sum(1 for item in discovery_candidates if item.discovery_source == "GDELT"), "google_news_candidates": sum(1 for item in discovery_candidates if item.discovery_source.startswith("Google News")), "alpha_vantage_candidates": len(alpha_candidates), "deduplicated_candidates": len(discovery_candidates), "total_candidates": len(candidates), "verified_direct_candidates": sum(1 for item in discovery_evidence if item.is_direct), "tier1_candidates": sum(1 for item in discovery_evidence if item.source_tier == 1), "tier2_candidates": sum(1 for item in discovery_evidence if item.source_tier == 2), "rejected_weak_candidates": len(alpha_rejected) + len(targeted_rejected), "high_authority_candidates": sum(1 for item in discovery_evidence if item.source_tier <= 2), "queries_used": queries, "targeted_search_audit": {"queries_attempted": targeted_queries, "results_found": len(targeted_candidates) + len(targeted_rejected), "direct_urls_found": sum(1 for item in targeted_candidates if not item.url.startswith("https://news.google.com/")), "tier1_candidates": sum(1 for item in targeted_candidates if item.source_domain.endswith(".com") and "investor" in item.source_domain), "tier2_candidates": sum(1 for item in targeted_candidates if item.source_domain.endswith("reuters.com") or item.source_domain.endswith("apnews.com")), "tier3_candidates": sum(1 for item in targeted_candidates if item.source_domain.endswith(("cnbc.com", "marketwatch.com", "barrons.com", "yahoo.com"))), "rejected_results": len(targeted_rejected)}}
        result = self._build_result(mover, status, primary, supporting, coverage, search_complete, len(candidates), independent_count, discovery_audit, verification_results, candidates)
        self._write_cache(symbol, result)
        return result

    def _to_evidence(self, item: DiscoveryCandidate, verified: Any) -> Evidence:
        domain = verified.publisher_domain.lower().removeprefix("www.") or item.source_domain.lower()
        return Evidence(source_name=verified.source_name or domain or item.discovery_source, source_tier=verified.source_tier, title=verified.title or item.title, published_at=verified.published_at or item.published_at, url=verified.canonical_url or item.url, retrieved_at=datetime.now(timezone.utc).isoformat(), event_relevance=f"Discovery candidate from {item.discovery_source}; direct verification={verified.retrieval_status}", supports_catalyst=any(term in (verified.title or item.title).lower() for term in ("earnings", "guidance", "analyst", "acquisition", "contract", "regulator", "lawsuit", "shares", "stock")), catalyst_type=classify_title(verified.title or item.title), event_datetime=verified.published_at or item.published_at, event_window_match=True, explicit_market_link=verified.explicit_market_link, source_group=f"direct:{domain}" if verified.direct_retrieval else f"discovery:{domain or item.discovery_source}", retrieval_status=verified.retrieval_status, url_resolution=verified.url_resolution, is_discovery=True, is_direct=verified.direct_retrieval, explicit_market_link_text=verified.explicit_market_link_text)

    def _build_result(self, mover: dict[str, Any], status: str, primary: Evidence | None, supporting: list[Evidence], coverage: dict[str, Any], search_complete: bool, candidate_count: int, independent_count: int, discovery_audit: dict[str, Any], verification_results: list[Any], candidates: list[Evidence]) -> dict[str, Any]:
        evidence_score = primary.evidence_score if primary else 0
        rejected = []
        if primary and status not in {"CONFIRMED", "POSSIBLE"}:
            rejected.append({"title": primary.title, "reason": "REJECTED: no direct credible verification" if not primary.is_direct else "REJECTED: insufficient causal evidence"})
        rejected.extend({"title": item.title, "reason": "REJECTED: unresolved discovery URL" if item.retrieval_status == "REDIRECT_ONLY" else "REJECTED: metadata-only or lower evidence score"} for item in supporting[:10])
        event_supporting = [item for item in supporting if primary and item.catalyst_type == primary.catalyst_type and (item.event_datetime or "")[:10] == (primary.event_datetime or "")[:10]]
        event_items = ([primary] if primary else []) + event_supporting
        completed_states = {"searched", "not_found", "not_applicable", "no_results"}
        searched = [name for name, state in coverage.items() if (isinstance(state, str) and state in completed_states) or (isinstance(state, dict) and any(value in completed_states for value in state.values()))]
        return {
            "engine_version": 13,
            "symbol": mover["symbol"],
            "company_name": mover["company_name"],
            "move_pct": mover["change_pct"],
            "direction": "up" if mover["change_pct"] >= 0 else "down",
            "catalyst_status": status,
            "catalyst_type": primary.catalyst_type if primary else None,
            "headline": primary.title if primary else None,
            "summary_facts": [item.event_relevance for item in ([primary] if primary else [])],
            "primary_source": primary.as_dict() if primary else None,
            "supporting_sources": [item.as_dict() for item in supporting],
            "source_count": len([primary] if primary else []) + len(supporting),
            "independent_source_count": independent_count,
            "explicit_market_link": bool(primary and primary.explicit_market_link),
            "confidence": "HIGH" if status == "CONFIRMED" else "MEDIUM" if status == "POSSIBLE" else "LOW",
            "evidence_quality": evidence_quality(primary, event_supporting),
            "explicit_market_link_text": primary.explicit_market_link_text if primary else None,
            "event_datetime": primary.event_datetime if primary else None,
            "event_window_match": bool(primary and primary.event_window_match),
            "evidence_score": evidence_score,
            "search_coverage": coverage,
            "search_complete": search_complete,
            "search_status": "COMPLETE" if search_complete else "INCOMPLETE",
            "coverage_limitations": (["GDELT_RATE_LIMITED"] if coverage.get("broad_discovery", {}).get("gdelt") == "failed" else []) + (["TARGETED_SEARCH_FAILED"] if coverage.get("broad_discovery", {}).get("targeted_search") == "failed" else []) + (["DIRECT_VERIFICATION_LIMITED"] if any(item.retrieval_status in {"REDIRECT_ONLY", "INACCESSIBLE"} for item in ([primary] if primary else []) + supporting) else []),
            "discovery_audit": discovery_audit,
            "verification": {
                "direct_sources_attempted": len(verification_results),
                "direct_sources_verified": sum(1 for item in verification_results if item.direct_retrieval),
                "official_source_found": any(item.source_tier == 1 and item.is_direct for item in event_items),
                "tier2_source_found": any(item.source_tier == 2 and item.is_direct for item in event_items),
                "evidence_quality": evidence_quality(primary, event_supporting),
            },
            "event_cluster": build_event_cluster(mover["symbol"], primary, event_supporting),
            "audit": {
                "sources_searched": searched,
                "candidate_events_found": candidate_count,
                "selected_event_reason": ("PROMOTED: direct evidence met the status rules" if status in {"CONFIRMED", "POSSIBLE"} else "REJECTED: no direct credible verification") if primary else "No in-window material event metadata selected",
                "rejected_candidates": rejected,
            },
        }

    def _cache_path(self, symbol: str) -> Path:
        return CACHE_DIR / self.market_date.isoformat() / f"{symbol}.json"

    def _load_cache(self, symbol: str) -> dict[str, Any] | None:
        path = self._cache_path(symbol)
        if not path.exists():
            return None
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            return result if result.get("engine_version") == 13 and "search_coverage" in result and "audit" in result else None
        except (OSError, ValueError):
            return None

    def _write_cache(self, symbol: str, result: dict[str, Any]) -> None:
        path = self._cache_path(symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.cache_writes += 1


def run_catalyst_snapshot(logger: logging.Logger) -> Path:
    """Read movers, analyze only selected movers, and write catalyst snapshots."""
    started = time.monotonic()
    movers = json.loads((OUTPUT_DIR / "movers_snapshot.json").read_text(encoding="utf-8"))
    selected: dict[str, dict[str, Any]] = {}
    for mover in movers.get("top_gainers", []) + movers.get("top_losers", []):
        selected.setdefault(mover["symbol"], mover)
    logger.info("Catalyst processing started: %d symbols", len(selected))
    engine = CatalystEngine(logger, movers["market_date"])
    catalysts = {symbol: engine.analyze(mover) for symbol, mover in selected.items()}
    statuses = ("CONFIRMED", "POSSIBLE", "NOT_CONFIRMED", "NO_RELEVANT_EVENT_FOUND", "SOURCE_CONFLICT")
    counts = {status: sum(1 for result in catalysts.values() if result["catalyst_status"] == status) for status in statuses}
    complete_count = sum(1 for result in catalysts.values() if result["search_complete"])
    generated_at = datetime.now(timezone.utc)
    snapshot = {
        "generated_at_utc": generated_at.isoformat(),
        "market_date": movers["market_date"],
        "symbols_processed": len(catalysts),
        "catalysts": catalysts,
        "summary": {**counts, "search_complete": complete_count, "search_incomplete": len(catalysts) - complete_count},
            "request_statistics": {
            "sec_requests": engine.sec.request_count,
            "company_requests": engine.company.request_count,
            "regulator_requests": engine.regulator.request_count,
            "major_news_requests": engine.major_news.request_count,
            "gdelt_requests": engine.discovery.gdelt.request_count,
            "gdelt_status": engine.discovery.gdelt.status,
            "google_news_requests": engine.discovery.google_news.request_count,
            "alpha_vantage_requests": engine.alpha_vantage.request_count,
            "alpha_vantage_candidates_returned": engine.alpha_vantage.candidate_count,
            "alpha_vantage_cache_hits": engine.alpha_vantage.cache_hits,
            "alpha_vantage_cache_writes": engine.alpha_vantage.cache_writes,
            "targeted_search_requests": engine.targeted_search.request_count,
            "targeted_search_candidates": engine.targeted_search.candidate_count,
            "targeted_search_cache_hits": engine.targeted_search.cache_hits,
            "targeted_search_cache_writes": engine.targeted_search.cache_writes,
            "underlying_source_requests": engine.sec.request_count + engine.company.request_count + engine.regulator.request_count + engine.major_news.request_count,
            "total_http_requests": engine.sec.request_count + engine.company.request_count + engine.regulator.request_count + engine.major_news.request_count + engine.discovery.gdelt.request_count + engine.discovery.google_news.request_count + engine.alpha_vantage.request_count + engine.targeted_search.request_count + engine.verifier.request_count,
            "cache_hits": engine.cache_hits + engine.discovery.cache_hits + engine.alpha_vantage.cache_hits + engine.targeted_search.cache_hits,
            "cache_writes": engine.cache_writes + engine.discovery.cache_writes + engine.alpha_vantage.cache_writes + engine.targeted_search.cache_writes,
            "catalyst_cache_hits": engine.cache_hits,
            "catalyst_cache_writes": engine.cache_writes,
            "discovery_cache_hits": engine.discovery.cache_hits,
            "discovery_cache_writes": engine.discovery.cache_writes,
            "direct_verification_requests": engine.verifier.request_count,
            "direct_verification_cache_hits": engine.verifier.cache_hits,
            "direct_verification_cache_writes": engine.verifier.cache_writes,
            "slow_fallbacks_used": engine.slow_fallbacks_used,
            "slow_fallback_cap": MAX_SLOW_MOVER_FALLBACKS,
            "runtime_seconds": round(time.monotonic() - started, 3),
            "blocked_or_inaccessible": sorted(set(engine.sec.access_failures + engine.company.access_failures + engine.regulator.access_failures + engine.major_news.access_failures + engine.discovery.gdelt.access_failures + engine.discovery.google_news.access_failures + engine.alpha_vantage.access_failures + engine.targeted_search.access_failures + engine.verifier.access_failures)),
        },
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    current = OUTPUT_DIR / "catalysts_snapshot.json"
    history = OUTPUT_DIR / f"catalysts_snapshot_{generated_at.strftime('%Y%m%d_%H%M%S')}.json"
    current.write_text(text, encoding="utf-8")
    history.write_text(text, encoding="utf-8")
    logger.info("Catalyst snapshot created: %s", current)
    return current


def build_catalyst_snapshot_from_live_news(
    movers: dict[str, Any],
    live_news: dict[str, Any],
    *,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Map verified Live News V3 stories into the established catalyst contract.

    This adapter intentionally performs no network work. Live News V3 already
    owns discovery and verification, so a mover without a verified story is a
    valid ``NO_CONFIRMED_CATALYST`` result rather than a reason to rediscover
    the same event through legacy providers.
    """
    started = time.monotonic()
    logger = logger or logging.getLogger("market_bot.catalyst_mapping")
    market_date = str(movers.get("market_date") or live_news.get("market_date") or "")
    selected: dict[str, dict[str, Any]] = {}
    for mover in movers.get("top_gainers", []) + movers.get("top_losers", []):
        if isinstance(mover, dict) and mover.get("symbol"):
            selected.setdefault(str(mover["symbol"]).upper(), mover)

    stories = [item for item in live_news.get("stories", []) if isinstance(item, dict)]
    catalysts: dict[str, dict[str, Any]] = {}
    for symbol, mover in selected.items():
        story = next(
            (
                item
                for item in stories
                if symbol in {str(value).upper() for value in item.get("affected_symbols", [])}
                and item.get("verification") == "VERIFIED"
                and item.get("editorial_eligible") is True
            ),
            None,
        )
        if story is None:
            catalysts[symbol] = _empty_live_catalyst(mover)
            continue
        source_name = story.get("primary_source_name") or story.get("source_name") or "Verified source"
        source_domain = story.get("primary_source_domain") or story.get("source_domain") or ""
        source_url = story.get("primary_source_url") or story.get("source_url") or ""
        source_type = str(story.get("primary_source_type") or "OTHER").upper()
        source_tier = 1 if source_type == "OFFICIAL" else 2 if source_type == "TIER2" else 3
        primary = {
            "source_name": source_name,
            "source_tier": source_tier,
            "publisher_domain": source_domain,
            "source_domain": source_domain,
            "url": source_url,
            "source_url": source_url,
            "published_at": story.get("published_at"),
            "retrieval_status": "FULL_METADATA",
            "is_direct": True,
        }
        catalysts[symbol] = {
            "engine_version": 14,
            "symbol": symbol,
            "company_name": mover.get("company_name"),
            "move_pct": mover.get("change_pct"),
            "direction": "up" if float(mover.get("change_pct") or 0) >= 0 else "down",
            "catalyst_status": "CONFIRMED",
            "catalyst_type": story.get("category") or "OTHER",
            "category": story.get("category") or "OTHER",
            "headline": story.get("title_he") or story.get("title"),
            "summary_he": story.get("summary_he") or story.get("summary") or story.get("title_he") or story.get("title"),
            "summary_facts": [story.get("summary_he") or story.get("summary") or story.get("title_he") or story.get("title")],
            "primary_source": primary,
            "supporting_sources": [primary],
            "source_count": int(story.get("supporting_sources_count", 1) or 1),
            "independent_source_count": int(story.get("supporting_sources_count", 1) or 1),
            "explicit_market_link": False,
            "confidence": "HIGH" if source_tier == 1 else "MEDIUM",
            "evidence_quality": "STRONG" if source_tier <= 2 else "MODERATE",
            "event_datetime": story.get("published_at"),
            "event_window_match": True,
            "search_coverage": {"live_news": "searched", "legacy_catalyst": "not_attempted"},
            "search_complete": True,
            "search_status": "COMPLETE",
            "coverage_limitations": [],
            "editorial_eligible": True,
            "verification": {"direct_sources_attempted": 1, "direct_sources_verified": 1, "official_source_found": source_tier == 1, "tier2_source_found": source_tier == 2, "evidence_quality": "STRONG" if source_tier <= 2 else "MODERATE"},
            "event_cluster": {"underlying_event_id": f"live-news:{symbol}:{story.get('published_at')}", "candidate_sources": [source_name], "independent_sources": [source_domain] if source_domain else [], "independent_source_count": int(story.get("supporting_sources_count", 1) or 1), "official_source_present": source_tier == 1, "tier2_source_present": source_tier == 2, "explicit_market_link_present": False},
            "audit": {"sources_searched": ["LIVE_NEWS_V3"], "candidate_events_found": 1, "selected_event_reason": "PROMOTED: Live News V3 verification passed", "rejected_candidates": []},
        }

    live_budget = live_news.get("request_budget", {}) if isinstance(live_news.get("request_budget"), dict) else {}
    mapping_seconds = round(time.monotonic() - started, 3)
    verified_count = sum(item.get("catalyst_status") == "CONFIRMED" for item in catalysts.values())
    request_statistics = {
        "live_news_seconds": float(live_budget.get("total_news_seconds", 0) or 0),
        "catalyst_mapping_seconds": mapping_seconds,
        "catalyst_gap_fill_seconds": 0.0,
        "duplicate_requests_avoided": len(selected),
        "catalyst_network_requests": 0,
        "total_http_requests": 0,
        "slow_fallbacks_used": 0,
        "slow_fallback_cap": MAX_SLOW_MOVER_FALLBACKS,
        "runtime_seconds": mapping_seconds,
        "integration_mode": "LIVE_NEWS_REUSE_V1",
    }
    snapshot = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "market_date": market_date,
        "symbols_processed": len(catalysts),
        "catalysts": catalysts,
        "summary": {"CONFIRMED": verified_count, "NO_CONFIRMED_CATALYST": len(catalysts) - verified_count, "search_complete": len(catalysts), "search_incomplete": 0},
        "request_statistics": request_statistics,
    }
    logger.info("Catalyst mapping completed: %d/%d verified, catalyst_network_requests=0", verified_count, len(catalysts))
    return snapshot


def _empty_live_catalyst(mover: dict[str, Any]) -> dict[str, Any]:
    """Return a report-compatible no-catalyst entry without a network fallback."""
    symbol = str(mover.get("symbol", "")).upper()
    return {
        "engine_version": 14,
        "symbol": symbol,
        "company_name": mover.get("company_name"),
        "move_pct": mover.get("change_pct"),
        "direction": "up" if float(mover.get("change_pct") or 0) >= 0 else "down",
        "catalyst_status": "NO_CONFIRMED_CATALYST",
        "catalyst_type": None,
        "headline": None,
        "summary_facts": [],
        "primary_source": None,
        "supporting_sources": [],
        "source_count": 0,
        "independent_source_count": 0,
        "confidence": "LOW",
        "evidence_quality": "WEAK",
        "search_coverage": {"live_news": "searched", "legacy_catalyst": "not_attempted"},
        "search_complete": True,
        "search_status": "COMPLETE",
        "coverage_limitations": [],
        "editorial_eligible": False,
        "verification": {"direct_sources_attempted": 0, "direct_sources_verified": 0, "official_source_found": False, "tier2_source_found": False, "evidence_quality": "WEAK"},
        "audit": {"sources_searched": ["LIVE_NEWS_V3"], "candidate_events_found": 0, "selected_event_reason": "No verified Live News V3 story for mover", "rejected_candidates": []},
    }


def write_live_news_catalyst_snapshot(movers_path: Path, live_news_path: Path, output_dir: Path = OUTPUT_DIR, logger: logging.Logger | None = None) -> Path:
    """Write the established catalyst snapshot contract from local Live News output."""
    logger = logger or logging.getLogger("market_bot.catalyst_mapping")
    movers = json.loads(movers_path.read_text(encoding="utf-8"))
    live_news = json.loads(live_news_path.read_text(encoding="utf-8"))
    snapshot = build_catalyst_snapshot_from_live_news(movers, live_news, logger=logger)
    output_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    current = output_dir / "catalysts_snapshot.json"
    history = output_dir / f"catalysts_snapshot_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    current.write_text(text, encoding="utf-8")
    history.write_text(text, encoding="utf-8")
    logger.info("Catalyst snapshot created from Live News V3: %s", current)
    return current


def evidence_quality(primary: Evidence | None, supporting: list[Evidence]) -> str:
    """Classify evidence quality without turning timing into causality."""
    items = ([primary] if primary else []) + supporting
    direct = [item for item in items if item.is_direct]
    if not direct:
        return "WEAK"
    has_official = any(item.source_tier == 1 for item in direct)
    has_tier2 = any(item.source_tier == 2 for item in direct)
    if (has_official and has_tier2) or (has_tier2 and any(item.explicit_market_link for item in direct)) or len({item.source_group for item in direct if item.source_tier <= 2}) >= 2:
        return "STRONG"
    if has_official or any(item.source_tier in {2, 3} for item in direct):
        return "MODERATE"
    return "WEAK"


def build_event_cluster(symbol: str, primary: Evidence | None, supporting: list[Evidence]) -> dict[str, Any]:
    """Create a stable, compact cluster record for the selected event."""
    items = ([primary] if primary else []) + supporting
    event_type = primary.catalyst_type if primary else None
    event_date = (primary.event_datetime or "")[:10] if primary else None
    seed = f"{symbol}|{event_type}|{event_date}"
    import hashlib
    underlying_event_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return {
        "underlying_event_id": underlying_event_id,
        "candidate_sources": sorted({item.source_name for item in items}),
        "independent_sources": sorted({item.source_group for item in items if item.source_group}),
        "independent_source_count": len({item.source_group for item in items if item.source_group}),
        "official_source_present": any(item.source_tier == 1 and item.is_direct for item in items),
        "tier2_source_present": any(item.source_tier == 2 and item.is_direct for item in items),
        "explicit_market_link_present": any(item.explicit_market_link for item in items),
    }


def publisher_tier(domain: str, company_mapping: dict[str, Any]) -> int:
    """Map publisher domains conservatively; discovery never promotes unknown sites."""
    if domain in {"reuters.com", "apnews.com"} or domain.endswith(".reuters.com"):
        return 2
    if domain in {"cnbc.com", "marketwatch.com", "barrons.com", "finance.yahoo.com"} or domain.endswith(".cnbc.com"):
        return 3
    for config in company_mapping.values():
        for key in ("ir_url", "press_release_url"):
            value = str(config.get(key, ""))
            if domain and domain in value.lower():
                return 1
    return 4
