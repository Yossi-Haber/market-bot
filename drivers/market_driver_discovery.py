"""Bounded, market-level discovery helpers.

This module does not perform HTTP requests. Existing provider clients remain
responsible for acquisition; these helpers define the separate market-story
pool, normalization/filtering, and deterministic diagnostics.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

MARKET_PROVIDER_ORDER = ("marketaux", "google_news", "gdelt", "finnhub", "official")
MAX_QUERY_PHASES = 2

QUERY_TEMPLATES: dict[str, list[str]] = {
    "RISK_OFF_VOLATILITY_SHOCK": ["stocks fall volatility", "Wall Street selloff VIX", "stocks risk off", "S&P 500 Nasdaq volatility"],
    "VOLATILITY_SPIKE": ["stocks fall volatility", "Wall Street selloff VIX", "S&P 500 Nasdaq volatility"],
    "BROAD_RISK_OFF": ["stocks fall yields", "stocks selloff macro", "Wall Street risk off"],
    "RISING_YIELDS": ["Treasury yields stocks", "10-year yield stocks", "bond yields Nasdaq", "Treasury selloff equities"],
    "BOND_SELLOFF": ["Treasury yields stocks", "bond selloff stocks"],
    "RATE_PRESSURE_ON_GROWTH": ["yields tech stocks", "rising yields growth stocks", "Treasury yields Nasdaq"],
    "OIL_RALLY_SUPPORTING_ENERGY": ["oil prices energy stocks", "crude rally energy sector", "oil jump XLE"],
    "OIL_SHOCK_UP": ["oil prices energy stocks", "crude rally energy sector", "oil jump XLE"],
    "USD_STRENGTH_HEADWIND": ["dollar rally stocks", "strong dollar equities", "DXY stocks"],
    "USD_RALLY": ["dollar rally stocks", "DXY stocks market"],
    "MARKET_BREADTH_WEAKNESS": ["stocks broad selloff", "most sectors lower", "Wall Street breadth weak"],
}

FALLBACK_TEMPLATES = {
    "RISK_OFF_VOLATILITY_SHOCK": ["Wall Street {date} market selloff"],
    "VOLATILITY_SPIKE": ["Wall Street {date} market selloff"],
    "BROAD_RISK_OFF": ["Wall Street {date} market selloff"],
    "RISING_YIELDS": ["Treasury stocks {date} market reaction"],
    "RATE_PRESSURE_ON_GROWTH": ["Wall Street {date} growth stocks yields"],
    "OIL_SHOCK_UP": ["oil energy stocks {date} market reaction"],
    "USD_RALLY": ["dollar stocks {date} market reaction"],
    "MARKET_BREADTH_WEAKNESS": ["Wall Street {date} breadth market"],
}

TOPIC_TERMS: dict[str, tuple[str, ...]] = {
    "TREASURY_YIELDS": ("treasury", "yield", "bond", "10-year", "10 year"),
    "FED": ("fed", "federal reserve", "rate expectations"),
    "INFLATION": ("inflation", "cpi", "prices"),
    "LABOR": ("jobs", "employment", "payroll", "unemployment", "labor"),
    "GDP": ("gdp", "growth"),
    "VIX": ("vix", "volatility"),
    "RISK_OFF": ("risk off", "selloff", "sell-off", "stocks fall", "stocks slid", "stocks dropped"),
    "RISK_ON": ("risk on", "rally", "stocks rise", "stocks climbed", "wall street rallies"),
    "OIL": ("oil", "crude", "brent", "wti"),
    "DOLLAR": ("dollar", "dxy", "fx"),
    "SEMICONDUCTORS": ("semiconductor", "chips", "chip stocks"),
    "AI": ("artificial intelligence", " ai ", "ai stocks"),
    "GEOPOLITICS": ("war", "conflict", "tariff", "sanctions", "geopolit"),
    "FISCAL": ("shutdown", "debt ceiling", "deficit", "fiscal"),
    "EARNINGS_BREADTH": ("earnings", "sectors", "breadth", "most stocks", "most sectors"),
}

COMMENTARY_TERMS = ("could move", "what investors should know", "valuation", "price target", "outlook", "forecast", "opinion", "long-term")
CAUSAL_PATTERNS = (
    (r"\b(?:stocks|shares|nasdaq|s&p 500|wall street|markets?)\b.{0,80}\b(?:as|after|following|on)\b", "EXPLICIT_AS"),
    (r"\b(?:stocks|shares|nasdaq|s&p 500|wall street|markets?)\b.{0,80}\bafter\b", "EXPLICIT_AFTER"),
    (r"\b(?:stocks|shares|markets?)\b.{0,80}\bon\b", "EXPLICIT_ON"),
    (r"\b(?:alongside|amid|with)\b", "ASSOCIATED_WITH"),
)


def _text(story: dict[str, Any]) -> str:
    return " ".join(str(story.get(key) or "") for key in ("title", "headline", "summary", "description")).lower()


def _date_only(value: Any) -> str:
    return str(value or "")[:10]


def build_market_driver_queries(hypothesis: dict[str, Any], market_date: str, *, provider: str = "existing", phase: int = 1) -> list[dict[str, str]]:
    """Return a bounded primary or fallback query phase; never performs I/O."""
    key = str(hypothesis.get("signal_type") or hypothesis.get("hypothesis_id") or "").upper()
    if phase == 2:
        templates = FALLBACK_TEMPLATES.get(key, FALLBACK_TEMPLATES["BROAD_RISK_OFF"])
        queries = [template.format(date=market_date) for template in templates]
    else:
        templates = QUERY_TEMPLATES.get(key, QUERY_TEMPLATES.get("BROAD_RISK_OFF", []))
        queries = [f"{query} {market_date}" for query in templates]
    return [{"provider": provider, "query": query, "market_date": market_date, "hypothesis_id": str(hypothesis.get("hypothesis_id") or key), "phase": str(phase)} for query in queries]


def build_market_query_plan(hypotheses: Iterable[dict[str, Any]], market_date: str, *, provider: str = "existing") -> dict[str, Any]:
    primary = {str(item.get("hypothesis_id")): build_market_driver_queries(item, market_date, provider=provider) for item in hypotheses}
    fallback = {str(item.get("hypothesis_id")): build_market_driver_queries(item, market_date, provider=provider, phase=2) for item in hypotheses}
    return {"market_date": market_date, "provider_order": list(MARKET_PROVIDER_ORDER), "max_phases": MAX_QUERY_PHASES, "primary": primary, "fallback": fallback}


def extract_market_topics(story: dict[str, Any]) -> list[str]:
    text = f" {_text(story)} "
    return [topic for topic, terms in TOPIC_TERMS.items() if any(term in text for term in terms)] or ["OTHER_MACRO"]


def extract_causal_language(story: dict[str, Any]) -> tuple[list[str], str]:
    text = _text(story)
    phrases: list[str] = []
    link_type = "NO_EXPLICIT_LINK"
    for pattern, kind in CAUSAL_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            phrases.append(match.group(0).strip())
            if link_type == "NO_EXPLICIT_LINK":
                link_type = kind
    return phrases, link_type


def _timing_relation(published_at: Any, market_date: str) -> str:
    published = _date_only(published_at)
    if not published:
        return "UNKNOWN"
    if published > market_date:
        return "FUTURE_EVENT"
    return "BEFORE_SESSION" if published < market_date else "DURING_SESSION"


def normalize_market_story(story: dict[str, Any], *, market_date: str, provider: str | None = None) -> dict[str, Any]:
    """Normalize provider metadata without applying stock subject scoring."""
    title = str(story.get("title") or story.get("headline") or "").strip()
    summary = str(story.get("summary") or story.get("description") or story.get("snippet") or "").strip()
    base = {**story}
    base.update({
        "title": title, "summary": summary,
        "published_at": story.get("published_at") or story.get("published") or story.get("datetime"),
        "source_name": story.get("source_name") or story.get("source") or story.get("discovery_source"),
        "source_url": story.get("source_url") or story.get("url"),
        "provider": provider or story.get("provider") or story.get("discovery_source"),
        "market_topics": story.get("market_topics") or extract_market_topics({**story, "title": title, "summary": summary}),
        "macro_entities": story.get("macro_entities") or [], "asset_classes": story.get("asset_classes") or [],
        "event_type": story.get("event_type") or "OTHER_MACRO", "market_scope": story.get("market_scope") or "UNKNOWN",
        "timing_relation": story.get("timing_relation") or _timing_relation(story.get("published_at") or story.get("published"), market_date),
    })
    phrases, link_type = extract_causal_language(base)
    base["causal_phrases"] = story.get("causal_phrases") or phrases
    base["causal_link_type"] = story.get("causal_link_type") or link_type
    base["is_market_wide"] = bool(story.get("is_market_wide", False))
    base["is_commentary"] = bool(story.get("is_commentary", False))
    return base


def market_subject_score(story: dict[str, Any]) -> float:
    """Score broad market relevance; mover symbols are intentionally ignored."""
    text = _text(story)
    score = min(45.0, sum(12.0 for term in ("s&p 500", "nasdaq", "dow", "wall street", "stocks", "equities", "markets") if term in text))
    score += min(35.0, sum(7.0 for term in {term for terms in TOPIC_TERMS.values() for term in terms} if term in text))
    if story.get("is_market_wide") or str(story.get("market_scope", "")).upper() in {"MARKET", "MARKET_WIDE", "MACRO"}:
        score += 20.0
    if story.get("causal_link_type") not in {None, "NO_EXPLICIT_LINK"}:
        score += 15.0
    if story.get("affected_symbols") and not any(term in text for term in ("s&p 500", "nasdaq", "dow", "wall street", "stocks", "equities", "markets")):
        score -= 35.0
    if any(term in text for term in ("stock to watch", "stock pick", "valuation", "price target")):
        score -= 25.0
    return round(max(0.0, min(100.0, score)), 2)


def hypothesis_match_score(story: dict[str, Any], hypothesis: dict[str, Any]) -> float:
    topics = {str(item).upper() for item in story.get("market_topics", [])}
    key = str(hypothesis.get("hypothesis_id") or hypothesis.get("signal_type") or "").upper()
    wanted = {"RISING_YIELDS": {"TREASURY_YIELDS", "FED"}, "RATE_PRESSURE_ON_GROWTH": {"TREASURY_YIELDS", "SEMICONDUCTORS", "AI"}, "RISK_OFF_VOLATILITY_SHOCK": {"VIX", "RISK_OFF"}, "VOLATILITY_SPIKE": {"VIX", "RISK_OFF"}, "OIL_RALLY_SUPPORTING_ENERGY": {"OIL"}, "OIL_SHOCK_UP": {"OIL"}, "USD_STRENGTH_HEADWIND": {"DOLLAR"}, "USD_RALLY": {"DOLLAR"}, "MARKET_BREADTH_WEAKNESS": {"RISK_OFF", "EARNINGS_BREADTH"}}.get(key, set())
    score = min(45.0, 22.5 * len(topics & wanted))
    score += 25.0 if story.get("causal_link_type") not in {None, "NO_EXPLICIT_LINK"} else 0.0
    score += 15.0 if story.get("is_market_wide") or str(story.get("market_scope", "")).upper() in {"MARKET", "MARKET_WIDE", "MACRO"} else 0.0
    score += 15.0 if story.get("timing_relation") in {"BEFORE_SESSION", "DURING_SESSION", "POST_SESSION_SAME_EVENT"} else -40.0 if story.get("timing_relation") in {"FUTURE_EVENT", "NEXT_DAY_NEW_EVENT"} else 0.0
    return round(max(0.0, min(100.0, score)), 2)


def filter_market_candidate(story: dict[str, Any], hypothesis: dict[str, Any], *, subject_threshold: float = 30.0, match_threshold: float = 30.0) -> dict[str, Any]:
    normalized = dict(story)
    normalized["market_subject_score"] = float(story.get("market_subject_score", market_subject_score(story)))
    normalized["hypothesis_match_score"] = float(story.get("hypothesis_match_score", hypothesis_match_score(story, hypothesis)))
    if normalized.get("subject_gate") == "FAIL":
        return {**normalized, "market_eligible": False, "rejection_reason": "SUBJECT_GATE_FAIL"}
    if normalized.get("is_commentary") or (any(term in _text(normalized) for term in COMMENTARY_TERMS) and not normalized.get("causal_phrases")):
        return {**normalized, "market_eligible": False, "rejection_reason": "COMMENTARY_ONLY"}
    if normalized.get("timing_relation") in {"FUTURE_EVENT", "NEXT_DAY_NEW_EVENT"}:
        return {**normalized, "market_eligible": False, "rejection_reason": normalized["timing_relation"]}
    if normalized["market_subject_score"] < subject_threshold:
        return {**normalized, "market_eligible": False, "rejection_reason": "LOW_MARKET_SUBJECT_RELEVANCE"}
    if normalized["hypothesis_match_score"] < match_threshold:
        return {**normalized, "market_eligible": False, "rejection_reason": "LOW_HYPOTHESIS_MATCH"}
    return {**normalized, "market_eligible": True, "rejection_reason": None}


def market_event_signature(story: dict[str, Any], market_date: str) -> str:
    topics = ",".join(sorted(str(item).upper() for item in story.get("market_topics", []))) or "OTHER_MACRO"
    assets = ",".join(sorted(str(item).upper() for item in story.get("asset_classes", []))) or "MARKET"
    return f"{market_date}|{topics}|{story.get('causal_link_type') or 'NO_EXPLICIT_LINK'}|{assets}"


def deduplicate_market_stories(stories: Iterable[dict[str, Any]], market_date: str) -> list[dict[str, Any]]:
    clusters: dict[str, dict[str, Any]] = {}
    for story in stories:
        item = dict(story)
        signature = item.get("market_event_signature") or market_event_signature(item, market_date)
        item["market_event_signature"] = signature
        entry = clusters.setdefault(signature, {**item, "_sources": set(), "syndicated_count": 0})
        entry["syndicated_count"] += 1
        entry["_sources"].add(str(item.get("source_name") or item.get("provider") or "unknown"))
        if item.get("market_subject_score", 0) > entry.get("market_subject_score", 0):
            entry.update({key: value for key, value in item.items() if key not in {"_sources", "syndicated_count"}})
    result = []
    for entry in clusters.values():
        entry["independent_source_count"] = len(entry.pop("_sources"))
        result.append(entry)
    return result


def rank_market_candidates(stories: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(stories, key=lambda item: (bool(item.get("causal_eligible")), item.get("causal_gate") == "PASS", bool(item.get("market_eligible")), float(item.get("hypothesis_match_score", 0)), float(item.get("market_subject_score", 0)), float(item.get("independent_source_count", 1))), reverse=True)


def build_market_discovery_result(hypothesis: dict[str, Any], stories: Iterable[dict[str, Any]], market_date: str, *, queries_generated: list[dict[str, Any]] | None = None, providers_attempted: list[str] | None = None, phase: int = 1) -> dict[str, Any]:
    normalized = [normalize_market_story(item, market_date=market_date) for item in stories]
    filtered = []
    from .cross_asset import market_driver_causal_gate
    for item in normalized:
        candidate = filter_market_candidate(item, hypothesis)
        gate = market_driver_causal_gate(candidate, hypothesis, market_date)
        candidate.update(gate)
        candidate["requested_market_date"] = market_date
        candidate["point_in_time_status"] = "PASS" if candidate.get("timing_relation") in {"BEFORE_SESSION", "DURING_SESSION", "POST_SESSION_SAME_EVENT"} else "FAIL"
        if candidate.get("market_eligible") and not candidate.get("causal_eligible"):
            candidate["market_eligible"] = False
            candidate["rejection_reason"] = candidate.get("causal_reason") or "CAUSAL_GATE_FAIL"
        filtered.append(candidate)
    clusters = deduplicate_market_stories(filtered, market_date)
    ranked = rank_market_candidates(clusters)
    return {"market_date": market_date, "hypothesis_id": hypothesis.get("hypothesis_id"), "phase": phase, "queries_generated": queries_generated or [], "providers_attempted": providers_attempted or [], "candidates_found": len(normalized), "candidates_after_market_filter": sum(bool(item.get("market_eligible")) for item in filtered), "clusters_created": len(clusters), "top_candidates": ranked[:5], "rejected_candidates": [{key: item.get(key) for key in ("title", "source_name", "market_subject_score", "hypothesis_match_score", "causal_gate", "timing_relation", "rejection_reason")} for item in filtered if not item.get("market_eligible")], "market_eligible_candidates": [item for item in ranked if item.get("market_eligible") and item.get("causal_eligible") is True], "metrics": {"query_count": len(queries_generated or []), "candidate_count": len(normalized), "eligible_count": sum(bool(item.get("market_eligible") and item.get("causal_eligible") is True) for item in filtered), "top_score": ranked[0].get("hypothesis_match_score", 0) if ranked else 0}}


def market_discovery_cache_path(cache_root: Path, market_date: str) -> Path:
    return cache_root / "market_driver_news" / f"{market_date}.json"


def save_market_discovery_cache(cache_root: Path, market_date: str, payload: dict[str, Any]) -> Path:
    path = market_discovery_cache_path(cache_root, market_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def load_market_discovery_cache(cache_root: Path, market_date: str) -> dict[str, Any] | None:
    """Backward-compatible payload loader; invalid caches return ``None``."""
    result = load_market_discovery_cache_state(cache_root, market_date)
    return result["payload"] if result["state"] == "HIT" else None


def load_market_discovery_cache_state(cache_root: Path, market_date: str) -> dict[str, Any]:
    """Load and validate the canonical date-scoped market discovery cache."""
    path = market_discovery_cache_path(cache_root, market_date)
    if not path.exists():
        return {"state": "MISS", "path": str(path), "payload": None, "eligible_candidates": [], "diagnostics": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "INVALID_SCHEMA", "path": str(path), "payload": None, "eligible_candidates": [], "diagnostics": {}}
    if not isinstance(value, dict):
        state = "INVALID_SCHEMA"
    elif value.get("market_date") != market_date:
        state = "DATE_MISMATCH"
    elif value.get("offline_ready") is not True or value.get("discovery_completed") is not True:
        state = "INVALID_SCHEMA"
    elif value.get("point_in_time_status") != "PASS":
        state = "INTEGRITY_FAIL"
    elif not isinstance(value.get("hypotheses"), list) or not isinstance(value.get("provider_metrics"), dict):
        state = "INVALID_SCHEMA"
    else:
        state = "HIT"
    candidates = value.get("market_eligible_candidates", []) if isinstance(value, dict) else []
    if not candidates and isinstance(value, dict):
        candidates = [item for hypothesis in value.get("hypotheses", []) if isinstance(hypothesis, dict) for item in hypothesis.get("market_eligible_candidates", []) if isinstance(item, dict)]
    diagnostics = {
        "hypotheses": value.get("hypotheses", []) if isinstance(value, dict) else [],
        "provider_metrics": value.get("provider_metrics", {}) if isinstance(value, dict) else {},
        "raw_candidate_count": _first_present(value, ("raw_candidate_count", "raw_candidates_count", "raw_candidates"), hypotheses_key="candidates_found"),
        "filtered_candidate_count": _first_present(value, ("filtered_candidate_count", "filtered_candidates_count", "filtered_candidates"), hypotheses_key="candidates_after_market_filter"),
        "eligible_candidate_count": _first_present(value, ("eligible_candidate_count", "eligible_candidates_count"), default=len(candidates)),
        "top_candidates": value.get("top_candidates", []) if isinstance(value, dict) else [],
        "rejections": value.get("rejections", []) if isinstance(value, dict) else [],
    }
    return {"state": state, "path": str(path), "payload": value if state == "HIT" else None, "eligible_candidates": candidates if state == "HIT" else [], "diagnostics": diagnostics}


def _first_present(payload: dict[str, Any], keys: tuple[str, ...], default: Any = None, hypotheses_key: str | None = None) -> Any:
    """Return the first explicitly persisted metric without inventing zeroes."""
    if not isinstance(payload, dict):
        return default
    for key in keys:
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        if isinstance(value, list):
            return len(value)
    hypotheses = payload.get("hypotheses")
    if hypotheses_key and isinstance(hypotheses, list) and hypotheses and all(isinstance(item, dict) and isinstance(item.get(hypotheses_key), int) for item in hypotheses):
        return sum(item[hypotheses_key] for item in hypotheses)
    return default
