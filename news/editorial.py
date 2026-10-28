"""Public-safe editorial story selection, freshness ranking, and clustering."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlparse

from news.event_language import asserted_event_type, is_future_preview

CATEGORY_MAP = {
    "EARNINGS": "EARNINGS", "GUIDANCE": "EARNINGS", "M_AND_A": "M&A", "FDA": "REGULATORY", "REGULATORY": "REGULATORY",
    "FILING": "COMPANY", "CONTRACT": "COMPANY", "PRODUCT": "TECH_PRODUCT", "TECH_PRODUCT": "TECH_PRODUCT", "MACRO_RELEASE": "MACRO_RELEASE", "CENTRAL_BANK": "CENTRAL_BANK",
    "GEOPOLITICS": "GEOPOLITICS", "COMMODITIES": "COMMODITIES", "COMPANY": "COMPANY", "OTHER": "OTHER",
}
EVENT_CATEGORIES = {"COMPANY", "EARNINGS", "M&A", "REGULATORY", "MACRO_RELEASE", "CENTRAL_BANK", "TECH_PRODUCT", "GEOPOLITICS", "COMMODITIES"}


def public_story_relevance(story: dict[str, Any], *, tracked_symbols: set[str] | None = None, mover_symbols: set[str] | None = None) -> str | None:
    """Official provenance alone does not make a routine company filing news."""
    category = str(story.get("category") or story.get("catalyst_type") or "OTHER").upper()
    title = str(story.get("title") or story.get("headline") or story.get("title_he") or "")
    if is_future_preview(title, str(story.get("summary") or "")):
        return None
    if category in {"MACRO_RELEASE", "CENTRAL_BANK", "GEOPOLITICS", "COMMODITIES"}:
        return "MARKET_WIDE_EVENT"
    material = bool(asserted_event_type(title)) or bool(re.search(
        r"\b(?:announc\w*|report\w*|launch\w*|introduc\w*|sign\w*|award\w*|approv\w*|halt\w*|delay\w*|file\w*)\b.{0,65}\b(?:guidance|outlook|new (?:product|chip)|contract|agreement|lawsuit|bankruptcy|certification)\b", title, re.I))
    material = material or story.get("material_company_announcement") is True
    material = material or bool(re.search(r"(?:פרסמ[הו]|דיווח[הו]|הודיע[הו]|חתמ[הו]|עדכנ[הו]|אישר[הו]).{0,45}(?:תוצאות|תחזית|רכיש|מיזוג|חוזה|הסכם|מוצר|תביעה)", title))
    if not material:
        return None
    if tracked_symbols is None:
        from movers import load_universe
        tracked_symbols = {item.symbol.upper() for item in load_universe()}
    symbols = {str(symbol).upper() for symbol in story.get("affected_symbols", [])}
    if symbols & (mover_symbols or set()):
        return "MOVER_MATERIAL_EVENT"
    if symbols & tracked_symbols:
        return "TRACKED_MATERIAL_EVENT"
    # An explicit material announcement can merit publication outside the
    # curated universe. Routine ownership/forms remain excluded above.
    if story.get("explicit_market_link") is True or story.get("scope") in {"MARKET", "SECTOR"}:
        return "BROADER_MATERIAL_EVENT"
    return "UNTRACKED_MATERIAL_EVENT"


def public_hebrew_copy(story: dict[str, Any]) -> dict[str, Any]:
    """Preserve supplied Hebrew, or use narrowly supported factual templates.

    Unhandled English stays in the original fields and is explicitly marked;
    it never becomes Hebrew merely because of a field name.
    """
    hebrew = lambda value: bool(re.search(r"[א-ת]{2,}", str(value or "")))
    title = str(story.get("title_he") or story.get("title") or story.get("headline") or "")
    summary = str(story.get("summary_he") or story.get("summary") or "")
    source = story.get("primary_source_name") or story.get("source_name") or "המקור המאומת"
    if hebrew(title):
        return {"title_he": title, "summary_he": summary if hebrew(summary) else f"פרסום האירוע אומת מול {source}.", "translation_status": "HEBREW_SUPPLIED"}
    symbols = story.get("affected_symbols") or [story.get("symbol") or ""]
    symbol = str(symbols[0]) if len(symbols) == 1 else ""
    event = asserted_event_type(title)
    public_title = None
    if not is_future_preview(title, summary):
        if symbol and event == "EARNINGS_RESULTS":
            public_title = f"{symbol} פרסמה תוצאות כספיות"
        elif symbol and (event == "GUIDANCE_CHANGE" or re.search(r"\b(?:announc\w*|updat\w*|releas\w*)\b.{0,25}\b(?:guidance|outlook)\b", title, re.I)):
            action = "הורידה" if re.search(r"\b(?:cuts?|cut|lowers?|lowered|reduc\w*)\b", title, re.I) else "העלתה" if re.search(r"\b(?:raises?|raised)\b", title, re.I) else "עדכנה"
            metric = "תחזית ההכנסות" if re.search(r"\brevenue\b", title, re.I) else "התחזית"
            quarter = re.search(r"\bQ([1-4])\b", title, re.I)
            suffix = f" לרבעון { {'1': 'הראשון', '2': 'השני', '3': 'השלישי', '4': 'הרביעי'}[quarter.group(1)]}" if quarter else ""
            public_title = f"{symbol} {action} את {metric}{suffix}"
        elif symbol and event == "M_AND_A":
            public_title = f"{symbol} הודיעה על רכישה או מיזוג"
        elif symbol and re.search(r"\b(?:announc\w*|introduc\w*|launch\w*)\b.{0,30}\bnew (?:product|chip)\b", title, re.I):
            public_title = f"{symbol} הודיעה על מוצר חדש"
        elif str(story.get("category")) == "MACRO_RELEASE" and re.search(r"\b(?:cpi|inflation)\b", title, re.I):
            public_title = "פורסם דוח האינפלציה"
        elif str(story.get("category")) == "MACRO_RELEASE" and re.search(r"\b(?:gdp|gross domestic product)\b", title, re.I):
            public_title = "פורסם נתון התוצר"
        elif str(story.get("category")) == "CENTRAL_BANK" and re.search(r"\b(?:rate decision|interest rate)\b", title, re.I):
            public_title = "פורסמה החלטת ריבית"
    if public_title:
        return {"title_he": public_title, "summary_he": f"פרסום האירוע אומת מול {source}; לא אומת קשר סיבתי לתנועת המניה." if symbol else f"הנתון פורסם על ידי {source}.", "translation_status": "DETERMINISTIC_HEBREW"}
    category_notice = {"MACRO_RELEASE": "פורסם עדכון כלכלי מאומת", "CENTRAL_BANK": "פורסמה הודעה של בנק מרכזי", "GEOPOLITICS": "פורסם דיווח גאופוליטי מאומת", "COMMODITIES": "פורסם עדכון מאומת בשוק הסחורות"}.get(str(story.get("category")))
    if category_notice and not is_future_preview(title, summary):
        return {"title_he": category_notice, "summary_he": f"כותרת המקור לא תורגמה. הפרסום אומת מול {source}.", "translation_status": "UNTRANSLATED_CATEGORY_NOTICE"}
    return {"title_he": None, "summary_he": None, "translation_status": "UNTRANSLATED"}


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
        relevance = public_story_relevance({**catalyst, "title": title, "category": category, "affected_symbols": [symbol]})
        if relevance is None:
            continue
        published_at = source.get("published_at") or catalyst.get("published_at") or catalyst.get("event_datetime")
        story = {
            "title": title,
            "title_he": catalyst.get("title_he"),
            "summary_he": catalyst.get("summary_he"),
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
            "relevance_reason": relevance,
        }
        story.update(public_hebrew_copy(story))
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
