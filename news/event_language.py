"""Shared deterministic checks for asserted events versus upcoming previews."""

from __future__ import annotations

import re


_UPCOMING = re.compile(
    r"\b(?:what to expect|earnings preview|preview of|ahead of (?:its |the )?(?:quarterly |earnings |financial )?results|"
    r"(?:will|scheduled to|set to|expected to|plans to) (?:report|release|announce)|"
    r"(?:earnings|results).{0,35}(?:tomorrow|next week)|(?:tomorrow|next week).{0,35}(?:earnings|results)|"
    r"(?:will|may|might|could|expected to).{0,35}(?:earnings|results|guidance|acquisition|acquire|downgrade|upgrade))\b", re.I)
_FUTURE_VERB = re.compile(r"\b(?:will|may|might|could|would|expected to|plans to|set to|scheduled to)\s+(?:\w+\s+){0,2}$", re.I)
_ACTIONS = (
    ("GUIDANCE_CHANGE", re.compile(r"\b(?:cuts?|cut|lowers?|lowered|raises?|raised|reduces?|reduced|revises?|revised)\b.{0,35}\b(?:guidance|outlook|forecast)\b", re.I)),
    ("EARNINGS_RESULTS", re.compile(r"\b(?:reports|reported|posts|posted|releases|released|announces|announced)\b.{0,40}\b(?:earnings|results|profit|revenue)\b", re.I)),
    ("ANALYST_ACTION", re.compile(r"\b(?:downgrades|downgraded|upgrades|upgraded)\b", re.I)),
    ("M_AND_A", re.compile(r"\b(?:acquires|acquired|announces|announced|completes|completed)\b.{0,40}\b(?:acquisition|merger|takeover)\b|\b(?:acquires|acquired)\s+[A-Z]", re.I)),
)


def asserted_event_type(title: str) -> str | None:
    """Recognize a completed/present action; questions and modal verbs are not facts."""
    if "?" in title:
        return None
    for event_type, pattern in _ACTIONS:
        match = pattern.search(title)
        if match and not _FUTURE_VERB.search(title[:match.start()]):
            # "reports earnings tomorrow" is an upcoming release, but a real
            # guidance cut ahead of tomorrow's earnings is an asserted action.
            if event_type == "EARNINGS_RESULTS" and re.search(r"\b(?:tomorrow|next week|next month)\b", title, re.I):
                continue
            return event_type
    return None


def is_future_preview(title: str, summary: str = "") -> bool:
    """An asserted headline action can coexist with preview context in its summary."""
    if asserted_event_type(title):
        return False
    if _UPCOMING.search(title):
        return True
    # A vague headline is not made concrete by generic summary event keywords.
    return bool(_UPCOMING.search(summary) and not asserted_event_type(summary))
