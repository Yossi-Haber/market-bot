"""Causal eligibility checks for stock-driver hypotheses.

This is deliberately separate from event clustering and verification.  It answers
only whether a candidate describes a concrete, timely event that could plausibly
explain a move.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo


CAUSAL_EVENT_TYPES = {
    "REGULATORY", "EARNINGS", "EARNINGS_RESULTS", "GUIDANCE", "M_AND_A",
    "CONTRACT", "PARTNERSHIP", "PRODUCT", "ANALYST_ACTION", "LEGAL",
    "MANAGEMENT_CHANGE", "FINANCING", "OPERATIONAL_UPDATE",
}
COMMENTARY_TERMS = (
    "a look at", "gf value", "chartmill", "valuation", "overvalued",
    "undervalued", "price target", "technical analysis", "market recap",
    "price-move recap", "why the stock", "why shares", "what investors",
    "stocks to watch", "portfolio", "מבט על", "שווי הוגן", "ניתוח טכני",
    "סיכום מסחר", "למה המניה", "יעד מחיר", "מניית", "קנייה", "מכירה",
)
REACTION_TERMS = ("rose after", "fell after", "why up", "why down", "עלתה בעקבות העליות", "ירדה בעקבות הירידות")
EVENT_WORDS = ("announced", "reports", "reported", "acquires", "acquisition", "contract", "agreement", "launch", "approved", "delay", "lawsuit", "מכרה", "רכשה", "הודיעה", "דיווחה", "אישור", "חוזה")
US_MARKET_TZ = ZoneInfo("America/New_York")
US_REGULAR_SESSION_CLOSE = time(16, 0)


def _text(candidate: dict[str, Any]) -> str:
    return " ".join(str(candidate.get(key) or "") for key in ("title", "summary", "canonical_event_title")).lower()


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None


def publication_time_relation(value: Any, market_date: str) -> str:
    """Compare an article to the requested day's 16:00 America/New_York close.

    Offset-aware timestamps are converted to New York before classification;
    date-only timestamps are treated as day-level evidence for that date.
    Naive timestamps are interpreted in New York. This keeps UTC evening
    publications that occurred before 16:00 ET on the same session date eligible.
    """
    requested = date.fromisoformat(market_date)
    if not value:
        return "UNKNOWN"
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed_date = date.fromisoformat(raw[:10])
        except ValueError:
            return "UNKNOWN"
        return "BEFORE_OR_DURING_SESSION" if parsed_date <= requested else "FUTURE_EVENT"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=US_MARKET_TZ)
    local = parsed.astimezone(US_MARKET_TZ)
    cutoff = datetime.combine(requested, US_REGULAR_SESSION_CLOSE, tzinfo=US_MARKET_TZ)
    return "BEFORE_OR_DURING_SESSION" if local <= cutoff else "FUTURE_EVENT"


def _article_timestamps(candidate: dict[str, Any]) -> list[Any]:
    values = list(candidate.get("cluster_article_dates", []))
    values.append(candidate.get("published_at"))
    values.extend(article.get("published_at") for article in candidate.get("cluster_articles", []) if isinstance(article, dict))
    return [value for value in values if value]


def evaluate_causal_eligibility(candidate: dict[str, Any], market_date: str) -> dict[str, Any]:
    """Return causal gate, reason, subject gate, and timing for one cluster."""
    event_type = str(candidate.get("event_type") or "OTHER").upper()
    role = str(candidate.get("target_role") or "").upper()
    subject_pass = role in {"PRIMARY_SUBJECT", "DIRECT_PRODUCT_EVENT"} or candidate.get("subject_score", 0) >= 30
    text = _text(candidate)
    actions = {str(action).upper() for action in candidate.get("event_actions", []) if action}
    commentary = any(term in text for term in COMMENTARY_TERMS)
    reaction = any(term in text for term in REACTION_TERMS)
    concrete = event_type in CAUSAL_EVENT_TYPES or bool(actions) or any(word in text for word in EVENT_WORDS)
    if commentary and not (event_type in CAUSAL_EVENT_TYPES or actions):
        reason = "COMMENTARY_ONLY"
        causal_pass = False
    elif reaction and not actions:
        reason = "REACTION_TO_MOVE"
        causal_pass = False
    elif not concrete:
        reason = "NO_CONCRETE_EVENT"
        causal_pass = False
    else:
        reason = "EVENT_SIGNAL_PRESENT"
        causal_pass = True

    requested = _parse_date(market_date)
    # The selected/canonical article timestamp is authoritative.  A prior
    # supporting cluster article cannot make a future selected headline
    # point-in-time eligible.
    timestamps = _article_timestamps(candidate)
    relation = "UNKNOWN"
    if requested and candidate.get("published_at"):
        relation = publication_time_relation(candidate.get("published_at"), market_date)
    elif requested and timestamps:
        relations = [publication_time_relation(value, market_date) for value in timestamps]
        known = [value for value in relations if value != "UNKNOWN"]
        relation = "BEFORE_OR_DURING_SESSION" if "BEFORE_OR_DURING_SESSION" in known else "FUTURE_EVENT" if known else "UNKNOWN"
        if known and all(value == "FUTURE_EVENT" for value in known) and candidate.get("same_event_prior_date"):
            relation = "POST_SESSION_SAME_EVENT"
    timing_pass = relation in {"BEFORE_OR_DURING_SESSION", "POST_SESSION_SAME_EVENT"}
    if causal_pass and not timing_pass:
        reason = "TIMING_INELIGIBLE"
    return {
        "subject_gate": "PASS" if subject_pass else "FAIL",
        "causal_gate": "PASS" if causal_pass and subject_pass else "FAIL",
        "causal_reason": reason,
        "event_time_relation": relation,
        "causal_eligible": bool(causal_pass and subject_pass and timing_pass),
    }
