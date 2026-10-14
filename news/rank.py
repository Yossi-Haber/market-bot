"""Deterministic catalyst evidence scoring and status rules."""

from __future__ import annotations

import re
from datetime import date

from news.sources import Evidence


def score_evidence(evidence: Evidence, market_date: date) -> int:
    """Score authority, timing, directness, and explicit causal language."""
    score = {1: 4, 2: 3, 3: 1, 4: 0}.get(evidence.source_tier, 0)
    if evidence.event_datetime:
        try:
            score += 3 if date.fromisoformat(evidence.event_datetime[:10]) == market_date else 1
        except ValueError:
            pass
    if evidence.supports_catalyst:
        score += 3
    if evidence.explicit_market_link:
        score += 2
    if evidence.catalyst_type in {"EARNINGS", "GUIDANCE", "M_AND_A", "FDA", "CONTRACT", "REGULATORY"}:
        score += 2
    evidence.evidence_score = score
    return score


def deduplicate_candidates(candidates: list[Evidence]) -> list[Evidence]:
    """Collapse syndicated copies by normalized title/date/type."""
    seen: set[tuple[str, str, str]] = set()
    output: list[Evidence] = []
    for item in candidates:
        key = (re.sub(r"[^a-z0-9]+", " ", item.title.lower()).strip(), (item.event_datetime or "")[:10], item.catalyst_type)
        if key not in seen:
            seen.add(key)
            output.append(item)
    return output


def choose_status(candidates: list[Evidence], market_date: date, search_complete: bool) -> tuple[str, Evidence | None, list[Evidence], int]:
    """Choose status while preventing incomplete searches from becoming no-event."""
    unique = deduplicate_candidates(candidates)
    if not unique:
        return ("NO_RELEVANT_EVENT_FOUND" if search_complete else "NOT_CONFIRMED"), None, [], 0
    for candidate in unique:
        score_evidence(candidate, market_date)
    ranked = sorted(unique, key=lambda item: (item.evidence_score, -item.source_tier), reverse=True)
    primary = ranked[0]
    independent_count = len({item.source_group for item in unique if item.source_group})
    authoritative_non_discovery = any(item.source_tier <= 2 and (not item.is_discovery or item.is_direct) for item in unique)
    direct_credible = [item for item in unique if item.is_direct and item.source_tier <= 3]
    direct_same_event = [item for item in direct_credible if item.catalyst_type == primary.catalyst_type and (item.event_datetime or "")[:10] == (primary.event_datetime or "")[:10]]
    direct_supports_primary = bool(direct_same_event)
    if not search_complete:
        status = "NOT_CONFIRMED"
    elif primary.source_tier <= 2 and authoritative_non_discovery and (primary.explicit_market_link or independent_count >= 2) and primary.evidence_score >= 8:
        status = "CONFIRMED"
    elif direct_supports_primary or (len(direct_same_event) >= 2 and primary.evidence_score >= 5):
        status = "POSSIBLE"
    else:
        status = "NOT_CONFIRMED"
    return status, primary, ranked[1:], independent_count
