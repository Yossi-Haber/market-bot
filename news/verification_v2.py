"""Bounded, evidence-first verification for filtered discovery candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

MATERIAL_TERMS = ("earnings", "revenue", "guidance", "acquisition", "merger", "partnership", "approved", "regulator", "regulatory", "investigation", "product launch", "launched", "executive", "ceo", "financing", "bankruptcy", "distress", "filed")
NON_MATERIAL_TERMS = ("commentary", "opinion", "price target", "forecast", "outlook", "week ahead", "market roundup")
EVENT_EQUIVALENTS = {
    "GUIDANCE": ("guidance", "outlook", "raises", "updates", "updated", "revises", "revised"),
    "EARNINGS": ("earnings", "revenue", "profit", "quarter", "financial results"),
    "M&A": ("acquisition", "acquire", "merger", "merges", "takeover", "merger agreement", "exploratory talks", "shareholder approval"),
    "REGULATORY": ("regulator", "regulatory", "approved", "approval", "investigation", "lawsuit"),
    "PARTNERSHIP": ("partnership", "partner", "agreement", "contract"),
    "EXECUTIVE": ("executive", "ceo", "chief executive", "appoints", "resigns"),
}


@dataclass
class VerificationDiagnostics:
    attempted_sources: list[dict[str, str]] = field(default_factory=list)
    verification_route: list[str] = field(default_factory=list)
    event_type: str | None = None
    actors: list[str] = field(default_factory=list)
    regulator_domain: str | None = None
    regulator_requested_url: str | None = None
    regulator_http_status: int | None = None
    regulator_error_code: str | None = None
    regulator_final_url: str | None = None
    regulator_content_type: str | None = None
    regulator_fallback_used: bool = False
    matched_source_type: str | None = None
    matched_domain: str | None = None
    reason: str = "no_independent_confirmation"

    def as_dict(self) -> dict[str, Any]:
        return {"attempted_sources": self.attempted_sources, "verification_route": self.verification_route, "event_type": self.event_type, "actors": self.actors, "regulator_domain": self.regulator_domain, "regulator_requested_url": self.regulator_requested_url, "regulator_http_status": self.regulator_http_status, "regulator_error_code": self.regulator_error_code, "regulator_final_url": self.regulator_final_url, "regulator_content_type": self.regulator_content_type, "regulator_fallback_used": self.regulator_fallback_used, "matched_source_type": self.matched_source_type, "matched_domain": self.matched_domain, "reason": self.reason}


def _tokens(value: Any) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", str(value or "").lower()))


def event_types(value: Any) -> set[str]:
    """Return deterministic semantic buckets without inferring causality."""
    text = str(value or "").lower()
    return {name for name, terms in EVENT_EQUIVALENTS.items() if any(term in text for term in terms)}


def extract_entities(value: Any) -> set[str]:
    """Extract explicit symbols/names and meaningful title entities."""
    if isinstance(value, dict):
        parts = [value.get("title"), value.get("summary"), value.get("company_name"), value.get("counterparty")]
        symbols = list(value.get("affected_symbols", []) or [])
        explicit = value.get("entity_names", []) or []
        for entity in value.get("entities", []) or []:
            if isinstance(entity, dict):
                parts.extend((entity.get("name"), entity.get("company_name")))
                if entity.get("symbol"):
                    symbols.append(entity["symbol"])
            else:
                parts.append(entity)
        text = " ".join(str(part or "") for part in parts)
        return {str(item).lower().strip() for item in [*symbols, *explicit] if str(item).strip()} | {token for token in _tokens(text) if token not in {"company", "announces", "announced", "raises", "updates", "regarding", "potential", "third", "quarter"}}
    return _tokens(value)


def _date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def titles_match(candidate: dict[str, Any], evidence: dict[str, Any]) -> bool:
    """Match the underlying event, not only identical headline wording."""
    left = _tokens(" ".join(str(candidate.get(key) or "") for key in ("title", "summary")))
    right = _tokens(" ".join(str(evidence.get(key) or "") for key in ("title", "summary", "exhibit_text", "filing_items")))
    overlap = len(left & right) / len(left | right) if left and right else 0.0
    symbols = {str(item).lower() for item in candidate.get("affected_symbols", [])} & {str(item).lower() for item in evidence.get("affected_symbols", [])}
    names = extract_entities(candidate) & extract_entities(evidence)
    shared_entity = bool(symbols or names)
    shared_event = bool(event_types(" ".join(str(evidence.get(key) or "") for key in ("title", "summary", "exhibit_text", "filing_items"))) & event_types(" ".join(str(candidate.get(key) or "") for key in ("title", "summary"))))
    return shared_entity and shared_event and (overlap >= 0.12 or bool(symbols) or len(names) >= 2)


def evidence_is_fresh(candidate: dict[str, Any], evidence: dict[str, Any], max_age_hours: int = 24) -> bool:
    candidate_time, evidence_time = _date(candidate.get("published_at")), _date(evidence.get("published_at"))
    return bool(candidate_time and evidence_time and abs((candidate_time - evidence_time).total_seconds()) <= max_age_hours * 3600)


def is_material_event(candidate: dict[str, Any]) -> bool:
    text = str(candidate.get("title") or "").lower()
    category = str(candidate.get("category") or "")
    return category in {"EARNINGS", "M&A", "REGULATORY", "TECH_PRODUCT", "CENTRAL_BANK", "MACRO_RELEASE"} or (any(term in text for term in MATERIAL_TERMS) and not any(term in text for term in NON_MATERIAL_TERMS))


def verify_candidate(candidate: dict[str, Any], evidence: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], VerificationDiagnostics]:
    """Verify one candidate against supplied official/Tier-2 evidence only."""
    evidence = evidence or []
    diagnostics = VerificationDiagnostics()
    matching: list[dict[str, Any]] = []
    for item in evidence:
        source_type = str(item.get("source_type") or "").upper()
        domain = str(item.get("source_domain") or "")
        if source_type:
            diagnostics.attempted_sources.append(source_type)
        if item.get("conflict") is True:
            diagnostics.reason = "conflicting_evidence"
            return _result(candidate, "REJECTED_CONFLICT", False), diagnostics
        if source_type not in {"OFFICIAL", "TIER2"}:
            continue
        if titles_match(candidate, item) and evidence_is_fresh(candidate, item):
            matching.append(item)
    official = next((item for item in matching if str(item.get("source_type", "")).upper() == "OFFICIAL"), None)
    tier2 = next((item for item in matching if str(item.get("source_type", "")).upper() == "TIER2"), None)
    selected = official or tier2
    if selected:
        diagnostics.matched_source_type = str(selected.get("source_type")).upper()
        diagnostics.matched_domain = selected.get("source_domain")
        if official:
            diagnostics.reason = "official_sec_exhibit_match" if selected.get("sec_exhibit_match") else "official_ir_match" if selected.get("source_domain") not in {"sec.gov", "secfilings.nasdaq.com"} else "official_regulator_match"
        else:
            diagnostics.reason = "tier2_independent_match"
        return _result(candidate, "VERIFIED", bool(is_material_event(candidate)), selected, matching), diagnostics
    return _result(candidate, "NOT_CONFIRMED", False), diagnostics


def _result(candidate: dict[str, Any], verification: str, eligible: bool, selected: dict[str, Any] | None = None, matching: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    result = dict(candidate)
    result["verification"] = verification
    result["editorial_eligible"] = eligible
    if verification == "VERIFIED" and selected:
        result.setdefault("discovery_source_name", result.get("source_name"))
        result.setdefault("discovery_source_domain", result.get("source_domain"))
        result.setdefault("discovery_source_url", result.get("source_url"))
        primary_domain = str(selected.get("source_domain") or "")
        primary_url = str(selected.get("source_url") or selected.get("canonical_url") or selected.get("url") or "")
        primary_name = str(selected.get("source_name") or ("SEC EDGAR" if primary_domain == "sec.gov" else primary_domain or "Verified source"))
        primary_type = str(selected.get("source_type") or "OFFICIAL").upper()
        result.update({
            "primary_source_name": primary_name,
            "primary_source_domain": primary_domain,
            "primary_source_url": primary_url,
            "primary_source_type": primary_type,
            "source_name": primary_name,
            "source_domain": primary_domain,
            "source_url": primary_url,
        })
        source_keys = set()
        for item in matching or [selected]:
            domain = str(item.get("source_domain") or "")
            url = str(item.get("source_url") or item.get("canonical_url") or item.get("url") or "")
            # A filing index and its exhibit are one evidence chain; domains are the safe unit.
            source_keys.add(domain or url)
        result["supporting_sources_count"] = len({key for key in source_keys if key})
    else:
        result["supporting_sources_count"] = int(result.get("supporting_sources_count", 0))
    return result


def public_story(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return only safe fields for the public live snapshot."""
    if candidate.get("verification") != "VERIFIED":
        return {key: candidate.get(key) for key in ("title", "summary", "category", "source_name", "source_domain", "source_url", "published_at", "affected_symbols", "verification", "editorial_eligible", "supporting_sources_count") if key in candidate}
    return {key: candidate.get(key) for key in ("title", "summary", "category", "affected_symbols", "published_at", "verification", "editorial_eligible", "primary_source_name", "primary_source_domain", "primary_source_url", "primary_source_type", "supporting_sources_count") if key in candidate}
