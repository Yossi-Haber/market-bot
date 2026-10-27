"""Conservative, cache-only evidence promotion for sector hypotheses.

This layer consumes articles already acquired for mover symbols. It does not
discover news, verify sources, or alter stock-specific candidate selection.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from macro_freshness import current_macro

from .causal import publication_time_relation


_SECTOR_PATTERNS = {
    "ENERGY": (r"oil producers", r"energy equities", r"oil and gas exploration", r"exploration and production", r"energy sector", r"oil and gas companies"),
    "SEMICONDUCTORS": (r"chipmakers", r"semiconductor stocks", r"chip and memory stocks"),
    "TECHNOLOGY": (r"technology sector", r"technology stocks", r"tech sector", r"tech stocks"),
}
_CAUSAL = re.compile(r"\b(as|because|due to|driven by|pressured by|after|amid|sent .{1,50} (?:soaring|surging|higher)|led the rebound|on .{1,70})\b", re.I)
_POSITIVE = re.compile(r"\b(rall(?:y|ied|ies)|rebound(?:ed)?|surge[sd]?|soar(?:ed|s)?|gains?|higher|climb(?:s|ed)?|lifting|led the rebound)\b", re.I)
_NEGATIVE = re.compile(r"\b(sell-?off|slide[sd]?|drop(?:s|ped)?|fall(?:s|en)?|retreat(?:s|ed)?|declin(?:e|es|ed)|lower|loss(?:es)?)\b", re.I)
_MOVEMENT_RECAP = re.compile(r"\b(third straight session|shares? (?:jump|rise|fall|drop)|stock (?:jumps|rises|falls|drops)|what(?:'s| is) happening)\b", re.I)


def _text(article: dict[str, Any]) -> str:
    return " ".join(str(article.get(key) or "") for key in ("title", "summary", "canonical_event_title")).strip()


def classify_sector_scope(article: dict[str, Any]) -> dict[str, Any]:
    """Classify breadth from explicit story text; incidental ticker metadata is ignored."""
    text = _text(article)
    lower = text.lower()
    hits = [sector for sector, patterns in _SECTOR_PATTERNS.items() if any(re.search(pattern, lower, re.I) for pattern in patterns)]
    affected = article.get("affected_symbols", [])
    if isinstance(affected, str):
        affected = [affected]
    if len(set(str(item).upper() for item in affected or [])) == 1 and _MOVEMENT_RECAP.search(str(article.get("title") or "")):
        return {"scope": "STOCK_SPECIFIC", "sector_tags": [], "scope_reason": "SINGLE_STOCK_RECAP_NOT_SECTOR_EVIDENCE"}
    event_type = str(article.get("event_type") or article.get("category") or "").upper()
    if not hits:
        return {"scope": "STOCK_SPECIFIC" if article.get("affected_symbols") else "UNRELATED", "sector_tags": [], "scope_reason": "NO_EXPLICIT_SECTOR_SCOPE"}
    if any(word in event_type for word in ("EARNINGS", "GUIDANCE", "ANALYST")) and not re.search(r"\b(peers|across the sector|sector-wide|other .* companies|industry-wide)\b", lower):
        return {"scope": "STOCK_SPECIFIC", "sector_tags": [], "scope_reason": "SINGLE_COMPANY_EVENT"}
    broad = bool(re.search(r"\b(oil producers|energy equities|exploration and production names|other .* companies|across the sector|sector-wide|chipmakers led|chip and memory stocks|semiconductor stocks)\b", lower))
    if not broad:
        return {"scope": "STOCK_SPECIFIC", "sector_tags": [], "scope_reason": "NO_BROAD_SUBJECT_LANGUAGE"}
    if "ENERGY" in hits and re.search(r"\b(crude|oil prices?|commodity)\b", lower):
        scope = "COMMODITY_WITH_SECTOR_IMPACT"
    elif re.search(r"\b(fed|federal reserve|yields?|rates?|policy|macro|wall street)\b", lower) and len(hits) > 0:
        scope = "MACRO_WITH_SECTOR_IMPACT"
    elif "SEMICONDUCTORS" in hits or "TECHNOLOGY" in hits:
        scope = "THEME_WIDE"
    else:
        scope = "SECTOR_WIDE"
    return {"scope": scope, "sector_tags": sorted(set(hits)), "scope_reason": "EXPLICIT_SECTOR_SUBJECT"}


def build_sector_evidence_pool(raw_articles: list[dict[str, Any]], market_date: str) -> list[dict[str, Any]]:
    """Deduplicate cached articles across mover caches and annotate narrow gates."""
    dedup: dict[str, dict[str, Any]] = {}
    for raw in raw_articles:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        published = item.get("published_at")
        relation = publication_time_relation(published, market_date)
        scope = classify_sector_scope(item)
        text = _text(item)
        key = str(item.get("source_url") or item.get("url") or (str(item.get("title") or "").casefold() + "|" + str(published or "")))
        existing = dedup.get(key)
        if existing:
            existing["cache_symbols"] = sorted(set(existing.get("cache_symbols", [])) | set(item.get("cache_symbols", [])))
            existing["affected_symbols"] = sorted(set(existing.get("affected_symbols", [])) | set(item.get("affected_symbols", [])))
            sources = set(existing.get("independent_sources", []))
            source = str(item.get("source_domain") or item.get("source_name") or "").strip()
            if source:
                sources.add(source)
            existing["independent_sources"] = sorted(sources)
            continue
        positive_sector_impact = bool(re.search(r"\b(lifting|boosting|sending .* higher|stocks? surging|sector gains?|led the rebound)\b", text, re.I))
        negative_sector_impact = bool(re.search(r"\b(pressuring|weighing on|pain landed|stocks? slid|sector sell-?off)\b", text, re.I))
        direction = "up" if positive_sector_impact or (_POSITIVE.search(text) and not _NEGATIVE.search(text)) else "down" if negative_sector_impact or (_NEGATIVE.search(text) and not _POSITIVE.search(text)) else None
        causal = bool(_CAUSAL.search(text)) and not bool(_MOVEMENT_RECAP.search(text) and not re.search(r"\b(because|due to|driven by|pressured by|as .* (?:retreat|rise|fall|surge))\b", text, re.I))
        item.update(scope)
        sector_event_type = {
            "COMMODITY_WITH_SECTOR_IMPACT": "COMMODITY_MOVE_WITH_SECTOR_IMPACT",
            "MACRO_WITH_SECTOR_IMPACT": "MACRO_EVENT_WITH_SECTOR_IMPACT",
            "THEME_WIDE": "INDUSTRY_THEME_EVENT",
            "SECTOR_WIDE": "SECTOR_EVENT",
        }.get(scope["scope"], "STOCK_SPECIFIC" if scope["scope"] == "STOCK_SPECIFIC" else "UNRELATED")
        item.update({
            "sector_event_type": sector_event_type,
            "event_time_relation": relation,
            "candidate_direction": direction,
            "sector_causal_eligible": bool(scope["scope"] in {"SECTOR_WIDE", "THEME_WIDE", "MACRO_WITH_SECTOR_IMPACT", "COMMODITY_WITH_SECTOR_IMPACT"} and causal and relation == "BEFORE_OR_DURING_SESSION" and direction),
            "sector_causal_reason": "PASS" if scope["scope"] in {"SECTOR_WIDE", "THEME_WIDE", "MACRO_WITH_SECTOR_IMPACT", "COMMODITY_WITH_SECTOR_IMPACT"} and causal and relation == "BEFORE_OR_DURING_SESSION" and direction else "SCOPE_CAUSAL_DIRECTION_OR_TIMING_GATE",
            "cache_symbols": sorted(set(item.get("cache_symbols", []))),
            "deduplicated_article_count": 1,
            "article_count": 1,
            "canonical_event_title": item.get("canonical_event_title") or item.get("title"),
            "independent_sources": [str(item.get("source_domain") or item.get("source_name"))] if (item.get("source_domain") or item.get("source_name")) else [],
        })
        item["causal_eligible"] = item["sector_causal_eligible"]
        item["causal_gate"] = "PASS" if item["sector_causal_eligible"] else "FAIL"
        item["subject_gate"] = "PASS" if scope["scope"] != "STOCK_SPECIFIC" else "FAIL"
        dedup[key] = item
    return list(dedup.values())


def promote_sector_evidence(driver: dict[str, Any], pool: list[dict[str, Any]], report_data: dict[str, Any]) -> dict[str, Any]:
    """Attach qualifying, point-in-time evidence without ever confirming it."""
    sector = str(driver.get("related_sector") or driver.get("scope") or "").upper().replace("_", " ")
    tags = {"ENERGY": {"ENERGY"}, "TECHNOLOGY": {"TECHNOLOGY", "SEMICONDUCTORS"}, "COMMUNICATION_SERVICES": {"COMMUNICATION_SERVICES"}, "FINANCIALS": {"FINANCIALS"}, "INDUSTRIALS": {"INDUSTRIALS"}}
    allowed = tags.get(sector, {sector.replace(" ", "_")})
    materiality = 1.5
    integrity = str(report_data.get("integrity_status") or report_data.get("data_integrity_status") or "").upper()
    integrity_failed = bool(report_data.get("integrity_violation")) or integrity in {"FAIL", "FAILED", "INTEGRITY_FAIL", "INVALID"}
    if integrity_failed or abs(float(driver.get("strength", 0) or 0)) < materiality:
        eligible: list[dict[str, Any]] = []
    else:
        eligible = [dict(item) for item in pool if item.get("sector_causal_eligible") and allowed.intersection(item.get("sector_tags", [])) and item.get("candidate_direction") == driver.get("direction")]
    macro = report_data.get("macro", {}) if isinstance(report_data.get("macro"), dict) else {}
    if sector == "ENERGY":
        wti = macro.get("WTI", {}) if isinstance(macro.get("WTI"), dict) else {}
        try:
            wti_move = abs(float(wti.get("change_pct", wti.get("change", 0)))) if current_macro(wti, report_data.get("market_date")) else 0.0
        except (TypeError, ValueError):
            wti_move = 0.0
        if wti_move < 2.0:
            eligible = []
    driver["candidate_stories"] = eligible
    driver["verified_story"] = None
    driver["verification_status"] = "PLAUSIBLE_NOT_VERIFIED" if eligible else "NO_CATALYST_FOUND"
    driver["hypothesis_status"] = "EVIDENCE_SUPPORTED" if eligible else "HYPOTHESIS_ONLY"
    driver["eligible_for_inheritance"] = bool(eligible)
    driver["confidence_reason"] = "Timely, direction-matched sector evidence from existing cached discovery; not independently verified." if eligible else "No qualifying timely sector-scoped causal evidence in the existing cache."
    return driver
