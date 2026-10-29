"""Finnhub Company News discovery adapter; never a verification source."""

from __future__ import annotations

import os
from datetime import datetime, timezone
import re
from typing import Any, Callable

import requests
import json
import os
import tempfile
from pathlib import Path

from cache_config import cache_path
from news.schema import NewsCandidate
from news.evidence_capture import observe, active as capture_active
from news.event_language import asserted_event_type, is_future_preview

API_URL = "https://finnhub.io/api/v1/company-news"
CACHE_DIR = cache_path("finnhub")
CACHE_SCHEMA_VERSION = "finnhub-discovery-v1"
MIN_TARGETED_SUBJECT_RELEVANCE = 30
DRIVER_CONFIG_PATH = Path(__file__).resolve().parents[2] / "drivers" / "config" / "sector_mappings.json"


def classify_finnhub_event(title: str) -> tuple[str, int]:
    """Classify explicit event language for driver-oriented ranking."""
    text = re.sub(r"\s+", " ", title.lower()).strip()
    if is_future_preview(title):
        return "OTHER", 0
    # Regulatory language must win before generic contract/order language.
    if any(term in text for term in ("faa", "sec ", "doj", "ftc", "fda", "regulator", "regulatory", "certification halted", "certification delay", "certification delayed", "investigation")):
        return "REGULATORY", 48
    asserted = asserted_event_type(title)
    if asserted == "GUIDANCE_CHANGE":
        return asserted, 48
    if asserted == "ANALYST_ACTION":
        return asserted, 36
    rules = (
        ("ANALYST_ACTION", ("upgraded", "downgraded", "analyst", "ubs calls", "price target"), 36),
        ("EARNINGS_RESULTS", ("earnings", "revenue", "profit", "quarterly results"), 48),
        ("GUIDANCE_CHANGE", ("guidance", "outlook"), 48),
        ("M_AND_A", ("acquisition", "acquire", "merger", "takeover"), 46),
        ("REGULATORY", ("regulator", "regulatory", "approved", "approval", "lawsuit"), 42),
        ("CONTRACT", ("contract", "order", "agreement", "awarded"), 42),
    )
    for event_type, terms, score in rules:
        if any(term in text for term in terms):
            return event_type, score
    return "OTHER", 8


def _event_config() -> tuple[dict[str, list[str]], dict[str, Any]]:
    try:
        config = json.loads(DRIVER_CONFIG_PATH.read_text(encoding="utf-8"))
        return config.get("event_concepts", {}), config.get("event_clustering", {})
    except (OSError, ValueError, TypeError):
        return {}, {}


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 2}


def _headline_key(text: str) -> str:
    """Normalize exact headline copies commonly produced by syndication."""
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def extract_event_signature(candidate: NewsCandidate, symbol: str, company_name: str = "") -> dict[str, Any]:
    """Extract canonical topic/action/actor concepts independent of classifier label."""
    concepts, _ = _event_config()
    text = f"{candidate.title} {candidate.summary or ''}".lower()
    headline_text = str(candidate.title or "").lower()
    topic_family = ""
    for name in ("737_MAX", "STARLINER", "UNION_AGREEMENT", "ANALYST_UPGRADE", "AIRCRAFT_ORDER", "DELIVERY", "PRODUCTION", "GUIDANCE", "EARNINGS"):
        if any(alias.lower() in headline_text for alias in concepts.get(name, [])):
            topic_family = name
            break
    actions = [name for name in ("SOFTWARE_GLITCH", "CERTIFICATION_DELAY", "REGULATORY_REVIEW", "UNION_AGREEMENT", "ANALYST_UPGRADE", "AIRCRAFT_ORDER", "DELIVERY", "PRODUCTION", "GUIDANCE", "EARNINGS") if any(alias.lower() in headline_text for alias in concepts.get(name, []))]
    # Summary text may support subject relevance, but generic summary words
    # must not create a new event action (for example EARNINGS on an analyst
    # commentary headline).
    if not actions and any(term in text for term in ("software issue", "certification delay", "faa", "regulatory")):
        actions = [name for name in ("SOFTWARE_GLITCH", "CERTIFICATION_DELAY", "REGULATORY_REVIEW") if any(alias.lower() in text for alias in concepts.get(name, []))]
    actors = []
    for actor in ("FAA", "SEC", "DOJ", "FTC", "FDA", "BOEING", "GE", "UBS"):
        if re.search(rf"\b{re.escape(actor.lower())}\b", text):
            actors.append(actor)
    if topic_family == "737_MAX" and "STARLINER" in actions:
        actions.remove("STARLINER")
    strong = bool(topic_family and actions) or bool(actors and actions)
    signature = f"{symbol.upper()}|{topic_family or 'SINGLETON'}|{','.join(sorted(set(actions)))}|{','.join(sorted(set(actors)))}"
    return {"event_signature": signature, "topic_family": topic_family or "UNSPECIFIED", "event_actions": sorted(set(actions)), "actors": sorted(set(actors)), "strong": strong}


def _published_datetime(candidate: NewsCandidate) -> datetime | None:
    try:
        return datetime.fromisoformat(str(candidate.published_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _signature_coherence(left: dict[str, Any], right: dict[str, Any], left_candidate: NewsCandidate, right_candidate: NewsCandidate, window_hours: float = 72.0) -> float:
    if left["topic_family"] == "UNSPECIFIED" or right["topic_family"] == "UNSPECIFIED" or left["topic_family"] != right["topic_family"]:
        return 0.0
    topic = 0.35
    actions = set(left["event_actions"]) & set(right["event_actions"])
    action_family = bool(actions) or (set(left["actors"]) & set(right["actors"]) and set(left["event_actions"]) & {"SOFTWARE_GLITCH", "CERTIFICATION_DELAY", "REGULATORY_REVIEW"} and set(right["event_actions"]) & {"SOFTWARE_GLITCH", "CERTIFICATION_DELAY", "REGULATORY_REVIEW"})
    if not action_family:
        return 0.0
    action_score = 0.35 if action_family else 0.0
    actor_score = 0.15 if set(left["actors"]) & set(right["actors"]) else 0.0
    first, second = _published_datetime(left_candidate), _published_datetime(right_candidate)
    time_score = 0.15 if first and second and abs((first - second).total_seconds()) <= window_hours * 3600 else 0.0
    return round(min(1.0, topic + action_score + actor_score + time_score), 3)


def _company_config(symbol: str, company_name: str = "") -> dict[str, Any]:
    try:
        config = json.loads(DRIVER_CONFIG_PATH.read_text(encoding="utf-8"))
        item = config.get("company_aliases", {}).get(symbol.upper(), {})
        return {"name": company_name or item.get("name", ""), "aliases": item.get("aliases", []), "products": item.get("products", [])}
    except (OSError, ValueError, TypeError):
        return {"name": company_name, "aliases": [symbol], "products": []}


def finnhub_subject_role(candidate: NewsCandidate, symbol: str, company_name: str = "") -> tuple[str, int, str]:
    """Return primary/direct/secondary/incidental role and relevance score."""
    title = str(candidate.title or "")
    summary = str(candidate.summary or "")
    text = f"{title} {summary}".lower()
    title_lower = title.lower()
    settings = _company_config(symbol, company_name)
    name = str(settings.get("name") or "").lower()
    aliases = [str(item).lower() for item in settings.get("aliases", []) if item]
    products = [str(item).lower() for item in settings.get("products", []) if item]
    subject_terms = ("shares", "stock", "reports", "announces", "rebound", "slides", "upgraded", "downgraded", "raises", "cuts")
    # Keep the existing strict name match: a shortened brand is not enough to
    # establish that a headline is about the mover rather than a subsidiary,
    # roundup, or incidental mention.
    company_in_title = bool(name and name in title_lower)
    ticker_context = bool(re.search(rf"\b{re.escape(symbol.upper())}\b\s+(?:{'|'.join(subject_terms)})", title, re.I))
    product_in_title = any(product in title_lower for product in products)
    direct_event = product_in_title and any(term in text for term in ("faa", "certification", "certification", "software issue", "delay", "halts", "grounded"))
    acquisition_reference = any(term in title_lower for term in ("acquires", "acquisition", "buys", "purchase")) and product_in_title
    if acquisition_reference and not ticker_context and not title_lower.startswith(name):
        return "SECONDARY_MENTION", 12, "TARGET_IS_ASSET_OR_PRODUCT_REFERENCE"
    if company_in_title or ticker_context:
        return "PRIMARY_SUBJECT", 48, "COMPANY_OR_TICKER_IS_HEADLINE_SUBJECT"
    if direct_event:
        return "DIRECT_PRODUCT_EVENT", 42, "TARGET_PRODUCT_EVENT_WITHOUT_COMPANY_NAME"
    if any(alias in text for alias in aliases + products):
        return "INCIDENTAL_MENTION", 0, "ALIAS_OR_PRODUCT_ONLY"
    return "INCIDENTAL_MENTION", 0, "NO_PRIMARY_SUBJECT_SIGNAL"


def finnhub_subject_relevance(candidate: NewsCandidate, symbol: str, company_name: str = "") -> tuple[int, str]:
    _, score, reason = finnhub_subject_role(candidate, symbol, company_name)
    return score, reason


_LEGAL_SUFFIXES = re.compile(r"\b(?:incorporated|inc|corporation|corp|company|co|limited|ltd|plc|llc|holdings|group)\b\.?", re.I)
_ROUNDUP_TITLE = re.compile(r"\b(?:top\s+(?:\d+|five|ten)|roundup|round-up|stocks? to watch|what's the best|which stocks?)\b", re.I)
_RATING = r"(?:strong\s+buy|buy|overweight|equal[- ]weight|neutral|hold|sell|underweight|market perform|outperform|underperform)"


def _company_title_match(title: str, company_name: str, aliases: list[str]) -> bool:
    """Match a normalized company phrase in the headline, ignoring legal suffixes."""
    names = [company_name, *aliases]
    for name in names:
        normalized = re.sub(r"\s+", " ", _LEGAL_SUFFIXES.sub(" ", str(name or ""))).strip(" ,.")
        if len(normalized) < 3:
            continue
        if re.search(rf"(?<![\w]){re.escape(normalized)}(?![\w])", title, re.I):
            return True
    return False


def _target_ticker_in_title(title: str, symbol: str) -> bool:
    ticker = re.escape(symbol.upper())
    return bool(re.search(rf"(?:\(\s*{ticker}\s*\)|\b(?:NASDAQ|NYSE|NYSEAMERICAN):\s*{ticker}\b|\b{ticker}\s+(?:stock|shares)\b)", title, re.I))


def _analyst_action_targets_headline_entity(title: str, company_name: str, aliases: list[str], symbol: str) -> bool:
    """Require an analyst-action verb to attach to the headline's target entity."""
    action = r"(?:upgrades?|upgraded|downgrades?|downgraded|initiates?|initiated|maintains?|maintained|reiterates?|reiterated|raises?|raised|cuts?|cut|lowers?|lowered)"
    names = [company_name, *aliases]
    for name in names:
        normalized = re.sub(r"\s+", " ", _LEGAL_SUFFIXES.sub(" ", str(name or ""))).strip(" ,.")
        if len(normalized) < 3:
            continue
        entity = re.escape(normalized)
        if re.search(rf"\b{action}\b.{{0,70}}(?:\bon\b\s+)?{entity}(?![\w])", title, re.I):
            return True
        if re.search(rf"(?<![\w]){entity}(?![\w]).{{0,70}}\b{action}\b", title, re.I):
            return True
    ticker = re.escape(symbol.upper())
    return bool(
        re.search(rf"\b{action}\b.{{0,45}}(?:\bon\b\s+)?(?:\(\s*{ticker}\s*\)|\b{ticker}\b)", title, re.I)
        or re.search(rf"(?:\(\s*{ticker}\s*\)|\b{ticker}\b).{{0,55}}\b{action}\b", title, re.I)
    )


def _headline_target_is_primary(title: str, company_name: str, aliases: list[str], symbol: str, analyst_firm: str | None) -> bool:
    """Reject peer/listicle mentions unless the target leads the action headline."""
    normalized_title = title.strip()
    for name in [company_name, *aliases]:
        normalized = re.sub(r"\s+", " ", _LEGAL_SUFFIXES.sub(" ", str(name or ""))).strip(" ,.")
        if len(normalized) >= 3 and re.match(rf"^{re.escape(normalized)}(?![\w])", normalized_title, re.I):
            return True
    if re.match(rf"^{re.escape(symbol.upper())}(?:\b|[:)])", normalized_title, re.I):
        return True
    if analyst_firm and re.match(rf"^{re.escape(analyst_firm)}\b", normalized_title, re.I):
        prefix = normalized_title[:]
        firm_action = re.match(
                rf"^{re.escape(analyst_firm)}\s+(?:analyst\s+)?(?:upgrades?|downgrades?|initiates?|maintains?|reiterates?|raises?|cuts?|lowers?)\b",
            prefix,
            re.I,
        )
        if firm_action:
            return True
    return False


def parse_analyst_action(candidate: NewsCandidate, symbol: str, company_name: str = "") -> dict[str, Any]:
    """Extract a conservative, deterministic analyst action and direct target.

    Subject confidence is derived from headline evidence plus the provider's
    normalized symbol association. Body-only company mentions never suffice.
    """
    title = str(candidate.title or "")
    summary = str(candidate.summary or "")
    text = f"{title} {summary}"
    company = _company_config(symbol, company_name)
    company_in_title = _company_title_match(title, str(company.get("name") or company_name), company.get("aliases", []))
    ticker_in_title = _target_ticker_in_title(title, symbol)
    action_targets_title_entity = _analyst_action_targets_headline_entity(
        title, str(company.get("name") or company_name), company.get("aliases", []), symbol
    )
    affected = {str(value).upper() for value in candidate.affected_symbols}
    exact_provider_target = affected == {symbol.upper()}

    analyst_firm = None
    firm_match = re.match(r"^\s*([A-Z][A-Za-z0-9&. -]{1,45}?)\s+analyst\b", summary, re.I)
    if not firm_match:
        firm_match = re.match(
            r"^\s*([A-Z][A-Za-z0-9&. -]{1,45}?)\s+(?:upgrades?\b|downgrades?\b|initiates?\b|maintains?\b|reiterates?\b)",
            summary,
            re.I,
        )
    if firm_match:
        analyst_firm = re.sub(r"\s+", " ", firm_match.group(1)).strip()
    else:
        title_firm = re.match(r"^\s*([A-Z][A-Z0-9&.-]{1,9})\s+(?:Maintains?|Reiterates?|Upgrades?|Downgrades?)\b", title)
        if title_firm:
            analyst_firm = title_firm.group(1)

    rating_from = rating_to = None
    rating_match = re.search(rf"from\s+({_RATING})\s+to\s+({_RATING})", text, re.I)
    if rating_match:
        rating_from, rating_to = (re.sub(r"\s+", " ", value).strip().upper() for value in rating_match.groups())
    elif (rating_match := re.search(rf"to\s+({_RATING})\s+from\s+({_RATING})", text, re.I)):
        rating_to, rating_from = (re.sub(r"\s+", " ", value).strip().upper() for value in rating_match.groups())

    pt_from = pt_to = None
    number = r"\$?\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)"
    pt_match = re.search(rf"(?:price\s+)?target\s+from\s+{number}\s+to\s+{number}", text, re.I)
    if pt_match:
        pt_from, pt_to = (float(value.replace(",", "")) for value in pt_match.groups())
    elif (pt_match := re.search(rf"(?:price\s+)?target\s+to\s+{number}\s+from\s+{number}", text, re.I)):
        pt_to, pt_from = (float(value.replace(",", "")) for value in pt_match.groups())
    else:
        single_target = re.search(rf"(?:price\s+)?target\s+(?:of|at|to)\s+{number}", text, re.I)
        if not single_target:
            single_target = re.search(rf"{number}\s+(?:price\s+)?target\b", text, re.I)
        if single_target:
            pt_to = float(single_target.group(1).replace(",", ""))

    has_upgrade = bool(re.search(r"\b(?:upgrades?|upgraded|raises? (?:its |the )?rating)\b", text, re.I))
    has_downgrade = bool(re.search(r"\b(?:downgrades?|downgraded|cuts? (?:its |the )?rating)\b", text, re.I))
    initiation = bool(re.search(r"\b(?:initiates? coverage|starts? coverage|begins? coverage)\b", text, re.I))
    maintains = bool(re.search(r"\b(?:maintains?|reiterates?)\b", text, re.I))
    pt_raise = bool(re.search(r"\b(?:raises?|raised|increases?|increased|hikes?|hiked)\b.{0,45}\b(?:price\s+)?target\b|\b(?:price\s+)?target\b.{0,30}\b(?:raised|increased|hiked|higher)\b", text, re.I))
    pt_cut = bool(re.search(r"\b(?:cuts?|cut|lowers?|lowered|reduces?|reduced)\b.{0,45}\b(?:price\s+)?target\b|\b(?:price\s+)?target\b.{0,30}\b(?:cut|lowered|reduced)\b", text, re.I))

    rating_scale = {
        "SELL": -2, "UNDERPERFORM": -1, "UNDERWEIGHT": -1, "EQUAL-WEIGHT": 0,
        "EQUAL WEIGHT": 0, "NEUTRAL": 0, "HOLD": 0, "MARKET PERFORM": 0,
        "BUY": 1, "STRONG BUY": 2, "OVERWEIGHT": 1, "OUTPERFORM": 1,
    }
    rating_delta = rating_scale.get(rating_to or "", 0) - rating_scale.get(rating_from or "", 0)
    if has_upgrade:
        action_type = "UPGRADE"
        action_direction = "POSITIVE" if rating_delta > 0 else "NEGATIVE" if rating_delta < 0 else "UNKNOWN" if rating_from and rating_to else "POSITIVE"
    elif has_downgrade:
        action_type = "DOWNGRADE"
        action_direction = "NEGATIVE" if rating_delta < 0 else "POSITIVE" if rating_delta > 0 else "UNKNOWN" if rating_from and rating_to else "NEGATIVE"
    elif initiation:
        action_type = "INITIATION"
        action_direction = "POSITIVE" if rating_to in {"BUY", "STRONG BUY", "OVERWEIGHT", "OUTPERFORM"} else "NEGATIVE" if rating_to in {"SELL", "UNDERWEIGHT", "UNDERPERFORM"} else "UNKNOWN"
    elif pt_from is not None and pt_to is not None and maintains:
        action_type = "REITERATION_WITH_TARGET_CHANGE"
        action_direction = "POSITIVE" if pt_to > pt_from else "NEGATIVE" if pt_to < pt_from else "UNKNOWN"
    elif pt_raise:
        action_type, action_direction = "PRICE_TARGET_RAISE", "POSITIVE"
    elif pt_cut:
        action_type, action_direction = "PRICE_TARGET_CUT", "NEGATIVE"
    elif maintains or re.search(r"\breiterates?\b", text, re.I):
        action_type, action_direction = "OTHER_ANALYST_ACTION", "UNKNOWN"
    else:
        action_type, action_direction = None, "UNKNOWN"

    listicle = bool(_ROUNDUP_TITLE.search(title))
    direct_headline_target = (
        (company_in_title or ticker_in_title)
        and action_targets_title_entity
        and _headline_target_is_primary(title, str(company.get("name") or company_name), company.get("aliases", []), symbol, analyst_firm)
    )
    subject_confidence = "INCIDENTAL"
    target_symbol = None
    if action_type and action_type != "OTHER_ANALYST_ACTION" and direct_headline_target and exact_provider_target and analyst_firm and not listicle:
        subject_confidence, target_symbol = "DIRECT_TARGET", symbol.upper()
    elif action_type and direct_headline_target and symbol.upper() in affected and not listicle:
        subject_confidence, target_symbol = "STRONG_TARGET", symbol.upper()
    elif (company_in_title or ticker_in_title) and symbol.upper() in affected:
        subject_confidence = (
            "AMBIGUOUS"
            if _headline_target_is_primary(title, str(company.get("name") or company_name), company.get("aliases", []), symbol, analyst_firm)
            else "INCIDENTAL"
        )

    return {
        "analyst_firm": analyst_firm,
        "action_type": action_type,
        "rating_from": rating_from,
        "rating_to": rating_to,
        "price_target_from": pt_from,
        "price_target_to": pt_to,
        "target_symbol": target_symbol,
        "subject_confidence": subject_confidence,
        "action_direction": action_direction,
    }


def analyst_action_direction_compatible(action: dict[str, Any] | None, move_pct: float | None) -> bool:
    """Require the parsed action to align with the stock's observed direction."""
    if not action or action.get("subject_confidence") not in {"DIRECT_TARGET", "STRONG_TARGET"}:
        return False
    try:
        move = float(move_pct)
    except (TypeError, ValueError):
        return False
    direction = action.get("action_direction")
    return (move > 0 and direction == "POSITIVE") or (move < 0 and direction == "NEGATIVE")


def rank_finnhub_candidates(candidates: list[NewsCandidate], symbol: str, company_name: str = "") -> list[dict[str, Any]]:
    """Apply the Finnhub subject gate and shared driver-oriented ranking."""
    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        subject_score, subject_reason = finnhub_subject_relevance(candidate, symbol, company_name)
        target_role, _, _ = finnhub_subject_role(candidate, symbol, company_name)
        event_type, event_score = classify_finnhub_event(candidate.title)
        analyst_action = parse_analyst_action(candidate, symbol, company_name)
        if event_type == "ANALYST_ACTION" and analyst_action["subject_confidence"] in {"DIRECT_TARGET", "STRONG_TARGET"}:
            subject_score = 70 if analyst_action["subject_confidence"] == "DIRECT_TARGET" else 60
            target_role = "PRIMARY_SUBJECT"
            subject_reason = "DIRECT_ANALYST_ACTION_SUBJECT"
        analysis_penalty = 24 if any(term in f"{candidate.title} {candidate.summary or ''}".lower() for term in ("stock market today", "dow jones", "great ai dilemma", "what investors should know", "forecast")) else 0
        selection_score = subject_score + event_score - analysis_penalty
        if target_role in {"SECONDARY_MENTION", "INCIDENTAL_MENTION"}:
            analysis_penalty += 20
            selection_score -= 20
        ranked.append({"candidate": candidate, "headline": candidate.title, "subject_relevance_score": subject_score, "subject_gate": "PASS" if subject_score >= MIN_TARGETED_SUBJECT_RELEVANCE else "FAIL", "subject_reason": subject_reason, "target_role": target_role, "event_type": event_type, "event_score": event_score, "selection_score": selection_score, "analysis_penalty": analysis_penalty, "analyst_action": analyst_action})
    ranked.sort(key=lambda item: item["selection_score"], reverse=True)
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index
    return ranked


def cluster_finnhub_candidates(candidates: list[NewsCandidate], symbol: str, company_name: str = "") -> list[dict[str, Any]]:
    """Cluster related event reports before ranking them."""
    ranked = rank_finnhub_candidates(candidates, symbol, company_name)
    _, clustering = _event_config()
    window_hours = float(clustering.get("event_window_hours", 72) or 72)
    threshold = float(clustering.get("minimum_coherence_score", 0.55) or 0.55)
    groups: list[dict[str, Any]] = []
    for item in ranked:
        candidate = item["candidate"]
        signature = extract_event_signature(candidate, symbol, company_name)
        item.update(signature)
        placed = False
        for group in groups:
            existing_roles = {entry["target_role"] for entry in group["items"]}
            primary_role = item["target_role"] in {"PRIMARY_SUBJECT", "DIRECT_PRODUCT_EVENT"}
            group_is_primary = bool(existing_roles & {"PRIMARY_SUBJECT", "DIRECT_PRODUCT_EVENT"})
            if primary_role != group_is_primary and (primary_role or group_is_primary):
                continue
            coherence = _signature_coherence(signature, group["signature"], candidate, group["items"][0]["candidate"], window_hours)
            if coherence >= threshold:
                left, right = signature, group["signature"]
                item["merge_reason"] = "PRIMARY_OR_DIRECT_EVENT_WITH_SHARED_SIGNATURE" if primary_role else "SECONDARY_EVENT_WITH_SHARED_SIGNATURE"
                item["role"] = item["target_role"]
                item["topic_overlap"] = left["topic_family"] == right["topic_family"]
                item["action_overlap"] = bool(set(left["event_actions"]) & set(right["event_actions"]))
                item["actor_overlap"] = bool(set(left["actors"]) & set(right["actors"]))
                group["items"].append(item)
                group["coherences"].append(coherence)
                placed = True
                break
        if not placed:
            item["merge_reason"] = "CLUSTER_SEED"
            item["role"] = item["target_role"]
            item["topic_overlap"] = True
            item["action_overlap"] = True
            item["actor_overlap"] = bool(signature["actors"])
            groups.append({"signature": signature, "items": [item], "coherences": [1.0]})
    clusters: list[dict[str, Any]] = []
    for group in groups:
        signature = group["signature"]
        items = group["items"]
        source_keys: set[str] = set()
        seen_headlines: set[str] = set()
        for item in items:
            candidate = item["candidate"]
            headline_key = _headline_key(candidate.title)
            source = str(candidate.source_name or candidate.raw_metadata.get("source") or "unknown").lower()
            # Exact headline copies are one underlying discovery item even if
            # Finnhub labels the publishers differently (for example Yahoo
            # carrying a wire story).
            if headline_key and headline_key in seen_headlines:
                continue
            seen_headlines.add(headline_key)
            source_keys.add(source)
        sources = sorted(source_keys)
        best = max(items, key=lambda item: item["selection_score"])
        cluster_score = best["selection_score"] + max(0, len(sources) - 1) * 5
        clusters.append({"event_cluster_id": signature["event_signature"], "event_signature": signature["event_signature"], "topic_family": signature["topic_family"], "event_actions": signature["event_actions"], "actors": signature["actors"], "canonical_event_title": best["headline"], "target_symbol": symbol.upper(), "event_type": "REGULATORY" if "REGULATORY_REVIEW" in signature["event_actions"] else best["event_type"], "target_role": best["target_role"], "subject_score": best["subject_relevance_score"], "analyst_action": best.get("analyst_action"), "cluster_coherence_score": round(sum(group["coherences"]) / len(group["coherences"]), 3), "independent_source_count": len(sources), "sources": sources, "article_count": len(items), "articles": [item["candidate"] for item in items], "article_diagnostics": [{key: item.get(key) for key in ("headline", "merge_reason", "role", "topic_overlap", "action_overlap", "actor_overlap")} for item in items], "best_candidate": best["candidate"], "selection_score": cluster_score, "ranked_candidates": items})
    clusters.sort(key=lambda item: item["selection_score"], reverse=True)
    for index, item in enumerate(clusters, start=1):
        item["cluster_rank"] = index
    return clusters


class FinnhubProvider:
    name = "finnhub"

    def __init__(self, token: str | None = None, request_get: Callable[..., Any] | None = None, timeout: tuple[int, int] = (5, 10), cache_dir: Path | None = None, refresh_cache: bool = False) -> None:
        self.token = (token if token is not None else os.getenv("FINNHUB_API_KEY", "")).strip()
        self.request_get = request_get or requests.get
        self.timeout = timeout
        self.cache_dir = cache_dir or cache_path("finnhub")
        self.refresh_cache = refresh_cache
        self.request_count = 0
        self.last_http_status: int | None = None
        self.last_raw_count = 0
        self.last_normalized_count = 0
        self.last_error: str | None = None
        self.cache_hits = 0
        self.cache_misses = 0
        self.live_requests = 0
        self.cache_writes = 0
        self.cache_incompatible = 0
        self.last_cache_path: Path | None = None
        self.last_cache_status = "NOT_CHECKED"
        self.status = "DISABLED_NOT_CONFIGURED" if not self.token else "READY"

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def search_company(self, symbol: str, start_date: str, end_date: str) -> list[NewsCandidate]:
        symbol = symbol.upper()
        cached = self._load_cache(symbol, start_date, end_date)
        if cached is not None:
            if capture_active():
                observe("finnhub", [{**c.as_dict(), **(c.raw_metadata if isinstance(c.raw_metadata, dict) else {}), "affected_symbols": c.affected_symbols,
                    "publication_timestamp_original": str((c.raw_metadata if isinstance(c.raw_metadata, dict) else {}).get("datetime", c.published_at))} for c in cached], scope="COMPANY_CACHE", symbols=[symbol], limitations=["CACHE_ORIGINAL_RETRIEVAL_TIME_MAY_BE_UNAVAILABLE"])
            self.last_normalized_count = len(cached)
            self.status = "CACHE_HIT"
            return cached
        if not self.enabled:
            observe("finnhub", [], scope="COMPANY", symbols=[symbol], state="UNAVAILABLE", limitations=["DISABLED_OR_UNCONFIGURED"])
            self.status = "DISABLED_NOT_CONFIGURED"
            return []
        self.live_requests += 1
        self.request_count += 1
        try:
            response = self.request_get(API_URL, params={"symbol": symbol, "from": start_date, "to": end_date, "token": self.token}, timeout=self.timeout, headers={"User-Agent": "market_bot/1.0"})
            self.last_http_status = getattr(response, "status_code", None)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, OSError, ValueError):
            observe("finnhub", [], scope="COMPANY", symbols=[symbol], state="UNAVAILABLE")
            self.status = "REQUEST_FAILED"
            self.last_error = "REQUEST_FAILED"
            return []
        observe("finnhub", payload, scope="COMPANY", symbols=[symbol])
        self.status = "OK"
        self.last_error = None
        self.last_raw_count = len(payload) if isinstance(payload, list) else 0
        normalized = [self._normalize(item, symbol) for item in payload if isinstance(item, dict) and item.get("headline") and item.get("url")]
        self.last_normalized_count = len(normalized)
        if self._has_valid_cache(symbol, start_date, end_date) and (not normalized or len(normalized) < len(payload)):
            # A refresh response that is empty or structurally partial must not
            # destroy an already-valid production cache.
            self.last_cache_status = "WRITE_SKIPPED_PARTIAL"
            return normalized
        self._write_cache(symbol, start_date, end_date, normalized)
        return normalized

    def _cache_path(self, symbol: str, start_date: str, end_date: str) -> Path:
        return self.cache_dir / symbol.upper() / f"{start_date}_{end_date}.json"

    def _has_valid_cache(self, symbol: str, start_date: str, end_date: str) -> bool:
        """Check whether an existing cache is safe to preserve during refresh."""
        path = self._cache_path(symbol, start_date, end_date)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return bool(
            isinstance(payload, dict)
            and payload.get("schema_version") == CACHE_SCHEMA_VERSION
            and payload.get("provider") == "FINNHUB"
            and payload.get("symbol") == symbol.upper()
            and payload.get("requested_from_date") == start_date
            and payload.get("requested_to_date") == end_date
            and isinstance(payload.get("normalized_candidates"), list)
        )

    def _load_cache(self, symbol: str, start_date: str, end_date: str) -> list[NewsCandidate] | None:
        path = self._cache_path(symbol, start_date, end_date)
        self.last_cache_path = path
        if self.refresh_cache or not path.exists():
            self.cache_misses += 1
            self.last_cache_status = "MISS"
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache_misses += 1
            self.last_cache_status = "MISS"
            return None
        if not isinstance(payload, dict) or payload.get("schema_version") != CACHE_SCHEMA_VERSION or payload.get("provider") != "FINNHUB" or payload.get("symbol") != symbol.upper() or payload.get("requested_from_date") != start_date or payload.get("requested_to_date") != end_date:
            self.cache_incompatible += 1
            self.cache_misses += 1
            self.last_cache_status = "INCOMPATIBLE"
            return None
        items = payload.get("normalized_candidates")
        if not isinstance(items, list):
            self.cache_incompatible += 1
            self.cache_misses += 1
            self.last_cache_status = "INCOMPATIBLE"
            return None
        try:
            candidates = [NewsCandidate(**item) for item in items if isinstance(item, dict)]
        except (TypeError, ValueError):
            self.cache_incompatible += 1
            self.cache_misses += 1
            self.last_cache_status = "INCOMPATIBLE"
            return None
        self.cache_hits += 1
        self.last_cache_status = "HIT"
        return candidates

    def _write_cache(self, symbol: str, start_date: str, end_date: str, candidates: list[NewsCandidate]) -> None:
        path = self._cache_path(symbol, start_date, end_date)
        payload = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "provider": "FINNHUB",
            "symbol": symbol.upper(),
            "requested_from_date": start_date,
            "requested_to_date": end_date,
            "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
            "http_status": self.last_http_status,
            "normalized_candidates": [candidate.as_dict() for candidate in candidates],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as temporary:
                    temporary.write(serialized)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.replace(temporary_name, path)
            except Exception:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
                raise
            self.cache_writes += 1
            self.last_cache_path = path
            self.last_cache_status = "WRITTEN"
        except OSError:
            self.last_cache_status = "WRITE_FAILED"

    def search_general(self, query: str, start_date: str, end_date: str) -> list[NewsCandidate]:
        return []

    def search_topic(self, topic: str, start_date: str, end_date: str) -> list[NewsCandidate]:
        return []

    @staticmethod
    def _normalize(item: dict[str, Any], symbol: str) -> NewsCandidate:
        timestamp = item.get("datetime")
        published = datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat() if timestamp else None
        candidate = NewsCandidate(str(item.get("headline")), item.get("summary"), published, str(item.get("source") or "Finnhub"), "", 3, str(item.get("url")), "OTHER", [symbol.upper()], "finnhub", False, "NOT_CONFIRMED", "OTHER", False, "NEWS_EVENT", 0.0, [], str(item.get("id") or item.get("url") or ""), dict(item))
        return candidate
