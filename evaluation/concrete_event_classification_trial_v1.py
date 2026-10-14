"""Offline-only concrete event classification trial (no production wiring)."""

from __future__ import annotations

import re
from typing import Any

from drivers.causal import publication_time_relation


_COMMENTARY = re.compile(
    r"\b(?:valuation|overvalued|undervalued|should you buy|when beating .* isn't enough|"
    r"post[- ]earnings (?:analysis|commentary|recap)|market recap|price target)\b",
    re.IGNORECASE,
)
_GUIDANCE_CONTEXT = re.compile(
    r"\b(?:guidance|outlook|eps|earnings guidance|benefit[- ]expense ratio)\b",
    re.IGNORECASE,
)
_GUIDANCE_ACTION = re.compile(
    r"\b(?:reaffirm(?:s|ed|ation)?|affirm(?:s|ed|ation)?|rais(?:e|es|ed)|"
    r"lower(?:s|ed)?|cut(?:s)?|maintain(?:s|ed)?|updat(?:e|es|ed))\b",
    re.IGNORECASE,
)
_OPERATIONAL_CONTEXT = re.compile(
    r"\b(?:supply (?:constraint|constraints|shortage|shortages|issue|issues|disruption)|"
    r"manufacturing (?:constraint|constraints|issue|issues|problem|problems)|"
    r"production (?:delay|delays|constraint|constraints|disruption)|"
    r"capacity (?:issue|issues|constraint|constraints)|operational disruption)\b",
    re.IGNORECASE,
)
_DISCLOSURE_ACTION = re.compile(
    r"\b(?:disclos(?:e|es|ed|ing)|report(?:s|ed|ing)?|announce(?:s|d)?|"
    r"executives? discuss(?:es|ed|ing)?|management discuss(?:es|ed|ing)?|"
    r"company discuss(?:es|ed|ing)?)\b",
    re.IGNORECASE,
)


def classify_concrete_event(title: str, summary: str | None = None) -> tuple[str, str]:
    """Return a narrow existing taxonomy type only for explicit new events."""
    text = " ".join(part for part in (title or "", summary or "") if part).strip()
    if not text or _COMMENTARY.search(text):
        return "OTHER", "COMMENTARY_OR_NO_EXPLICIT_EVENT"
    if _GUIDANCE_ACTION.search(text) and _GUIDANCE_CONTEXT.search(text):
        return "GUIDANCE", "EXPLICIT_GUIDANCE_ACTION_AND_CONTEXT"
    if _OPERATIONAL_CONTEXT.search(text) and _DISCLOSURE_ACTION.search(text):
        return "OPERATIONAL_UPDATE", "EXPLICIT_OPERATIONAL_CONDITION_DISCLOSED"
    return "OTHER", "NO_EXPLICIT_NEW_EVENT_GRAMMAR"


def evaluate_trial_candidate(
    candidate: dict[str, Any], market_date: str, move_pct: float | None,
    expected_symbol: str | None = None,
) -> dict[str, Any]:
    """Apply the trial classifier while leaving all existing gates intact."""
    event_type, classifier_reason = classify_concrete_event(
        str(candidate.get("title") or ""), candidate.get("summary")
    )
    current_type = str(candidate.get("event_type") or "OTHER").upper()
    recovered_type = event_type if current_type == "OTHER" else current_type
    role = str(candidate.get("target_role") or "").upper()
    subject_pass = role in {"PRIMARY_SUBJECT", "DIRECT_PRODUCT_EVENT"} or int(
        candidate.get("subject_score") or 0
    ) >= 30
    if expected_symbol:
        subjects = {str(value).upper() for value in candidate.get("affected_symbols", [])}
        explicit = str(candidate.get("target_symbol") or "").upper()
        subject_pass = subject_pass and (
            explicit == expected_symbol.upper()
            if explicit else expected_symbol.upper() in subjects
        )
    timing = publication_time_relation(candidate.get("published_at"), market_date)
    title = str(candidate.get("title") or "").lower()
    direction = "UNKNOWN"
    if re.search(r"\b(?:higher|jump(?:s|ed|ing)?|rise|rises|rose|gain(?:s|ed)?)\b", title):
        direction = "UP"
    elif re.search(r"\b(?:lower|fall(?:s|en)?|fell|drop(?:s|ped)?|declin(?:e|es|ed))\b", title):
        direction = "DOWN"
    # Explicit direction language must agree with the observed move. Absence of
    # directional language is not treated as proof of compatibility.
    direction_pass = bool(
        move_pct is not None
        and direction != "UNKNOWN"
        and ((move_pct > 0 and direction == "UP") or (move_pct < 0 and direction == "DOWN"))
    )
    candidate_event = recovered_type in {"GUIDANCE", "OPERATIONAL_UPDATE"}
    eligible = bool(
        current_type == "OTHER"
        and candidate_event
        and subject_pass
        and timing == "BEFORE_OR_DURING_SESSION"
        and direction_pass
    )
    return {
        "current_event_type": current_type,
        "trial_event_type": recovered_type,
        "classifier_reason": classifier_reason,
        "subject_gate": "PASS" if subject_pass else "FAIL",
        "event_time_relation": timing,
        "direction_signal": direction,
        "direction_compatible": direction_pass,
        "trial_eligible": eligible,
    }
