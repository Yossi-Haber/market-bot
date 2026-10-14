"""Public-safe editorial story selection, freshness ranking, and clustering."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlparse

CATEGORY_MAP = {
    "EARNINGS": "EARNINGS", "GUIDANCE": "EARNINGS", "M_AND_A": "M&A", "FDA": "REGULATORY", "REGULATORY": "REGULATORY",
    "FILING": "COMPANY", "CONTRACT": "COMPANY", "PRODUCT": "TECH_PRODUCT", "TECH_PRODUCT": "TECH_PRODUCT", "MACRO_RELEASE": "MACRO_RELEASE", "CENTRAL_BANK": "CENTRAL_BANK",
    "GEOPOLITICS": "GEOPOLITICS", "COMMODITIES": "COMMODITIES", "COMPANY": "COMPANY", "OTHER": "OTHER",
}
EVENT_CATEGORIES = {"COMPANY", "EARNINGS", "M&A", "REGULATORY", "MACRO_RELEASE", "CENTRAL_BANK", "TECH_PRODUCT", "GEOPOLITICS", "COMMODITIES"}


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def freshness_metadata(published_at: str | None, market_date: str, reference_at: datetime | None = None) -> dict[str, Any]:
    """Return safe freshness fields used for ranking; never invent publication time."""
    published = _parse_time(published_at)
    market = date.fromisoformat(market_date)
    reference = reference_at or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    age_minutes = None if published is None else max(0, int((reference - published).total_seconds() // 60))
    return {"published_at": published_at, "age_minutes": age_minutes, "same_market_day": bool(published and published.date() == market)}


def _domain(source: dict[str, Any]) -> str:
    domain = str(source.get("publisher_domain") or source.get("source_domain") or "").lower().removeprefix("www.")
    if domain:
        return domain
    return urlparse(str(source.get("url") or "")).netloc.lower().removeprefix("www.")


def source_quality(source: dict[str, Any]) -> int:
    """Resolve the existing source tier without exposing evidence internals."""
    try:
        tier = int(source.get("source_tier", 4))
        if tier in (1, 2, 3, 4):
            return tier
    except (TypeError, ValueError):
        pass
    domain = _domain(source)
    if any(name in domain for name in ("sec.gov", "federalreserve.gov", "bls.gov", "bea.gov", "treasury.gov")):
        return 1
    if domain.endswith(("reuters.com", "apnews.com")):
        return 2
    return 3


def _normalized_title(value: str) -> str:
    return re.sub(r"[^a-z0-9א-ת]+", " ", value.lower()).strip()


def cluster_stories(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cluster equivalent event articles into one candidate while counting support."""
    clusters: list[dict[str, Any]] = []
    for candidate in candidates:
        title = str(candidate.get("title") or candidate.get("title_he") or candidate.get("headline") or "").strip()
        category = str(candidate.get("category") or "OTHER")
        event_day = str(candidate.get("published_at") or candidate.get("event_datetime") or "")[:10]
        key = (_normalized_title(title), category, event_day)
        candidate_tokens = set(_normalized_title(title).split())
        def same_event(item: dict[str, Any]) -> bool:
            symbols_a = set(item.get("affected_symbols", []))
            symbols_b = set(candidate.get("affected_symbols", []))
            item_tokens = set(_normalized_title(str(item.get("title") or item.get("title_he") or item.get("headline") or "")).split())
            overlap = len(candidate_tokens & item_tokens) / len(candidate_tokens | item_tokens) if candidate_tokens and item_tokens else 0
            return bool(symbols_a & symbols_b and overlap >= 0.3) or bool(not symbols_a and not symbols_b and item["_cluster_key"][1] == key[1] and item["_cluster_key"][2] == key[2] and overlap >= 0.6)
        match = next((item for item in clusters if item["_cluster_key"] == key or same_event(item)), None)
        if match is None:
            match = {**candidate, "_cluster_key": key, "supporting_sources_count": 1, "_source_domains": [_domain(candidate.get("primary_source") or candidate)]}
            clusters.append(match)
        else:
            domain = _domain(candidate.get("primary_source") or candidate)
            if domain and domain not in match["_source_domains"]:
                match["_source_domains"].append(domain)
            match["supporting_sources_count"] = len(match["_source_domains"])
            if candidate.get("official") and not match.get("official"):
                for field in ("title", "summary", "published_at", "source_name", "source_domain", "source_tier", "source_url", "category", "discovery_source", "official"):
                    if field in candidate:
                        match[field] = candidate[field]
            match["affected_symbols"] = sorted(set(match.get("affected_symbols", [])) | set(candidate.get("affected_symbols", [])))
    for item in clusters:
        item.pop("_cluster_key", None)
        item.pop("_source_domains", None)
    return clusters


def _rank_key(story: dict[str, Any]) -> tuple[int, int, int, int]:
    freshness = story.get("freshness", {})
    same_day = 1 if freshness.get("same_market_day") else 0
    age = freshness.get("age_minutes")
    recency_bucket = 0 if age is None else 1 if age <= 12 * 60 else 2 if age <= 24 * 60 else 3
    tier = int(story.get("source_quality_tier", 4))
    return (same_day, -recency_bucket, -tier, -int(story.get("supporting_sources_count", 1)))


def build_public_stories(catalysts: dict[str, Any], market_date: str | None = None, reference_at: datetime | None = None) -> list[dict[str, Any]]:
    """Select only approved event stories and return a public-safe schema."""
    candidates: list[dict[str, Any]] = []
    for symbol, catalyst in catalysts.items():
        if not isinstance(catalyst, dict) or catalyst.get("catalyst_status") not in {"CONFIRMED", "POSSIBLE"}:
            continue
        if catalyst.get("editorial_eligible") is not True:
            continue
        if catalyst.get("catalyst_status") == "POSSIBLE" and catalyst.get("evidence_quality") != "STRONG":
            continue
        source = catalyst.get("primary_source") if isinstance(catalyst.get("primary_source"), dict) else {}
        category = CATEGORY_MAP.get(str(catalyst.get("category") or catalyst.get("catalyst_type") or "OTHER"), "OTHER")
        if category not in EVENT_CATEGORIES:
            # Keep the existing conservative POSSIBLE policy, but never publish a price-only item.
            if not catalyst.get("explicit_market_link") and not catalyst.get("event_category"):
                continue
            category = "COMPANY"
        title = str(catalyst.get("headline") or "").strip()
        if not title:
            continue
        published_at = source.get("published_at") or catalyst.get("published_at") or catalyst.get("event_datetime")
        story = {
            "title_he": title,
            "summary_he": str(catalyst.get("summary_he") or title),
            "summary": str(catalyst.get("summary") or catalyst.get("summary_he") or title),
            "category": category,
            "published_at": published_at,
            "source_name": source.get("source_name") or catalyst.get("source_name") or "מקור מאומת",
            "source_domain": _domain(source),
            "affected_symbols": [symbol],
            "verification": "VERIFIED",
            "editorial_eligible": True,
            "source_quality_tier": source_quality(source),
            "verification_level": "VERIFIED",
            "primary_source_name": source.get("source_name") or catalyst.get("source_name") or "מקור מאומת",
            "primary_source_domain": _domain(source),
            "primary_source_url": source.get("url") or source.get("source_url"),
            "primary_source_type": "OFFICIAL" if source_quality(source) == 1 else "TIER2" if source_quality(source) == 2 else "OTHER",
        }
        if not published_at:
            continue
        if market_date:
            story["freshness"] = freshness_metadata(published_at, market_date, reference_at)
            if story["freshness"].get("age_minutes") is None or story["freshness"]["age_minutes"] > 24 * 60:
                continue
        candidates.append(story)
    clustered = cluster_stories(candidates)
    for story in clustered:
        story.setdefault("freshness", {})
        story["supporting_sources_count"] = story.get("supporting_sources_count", 1)
        story.pop("source_quality_tier", None)
    return sorted(clustered, key=_rank_key, reverse=True)
