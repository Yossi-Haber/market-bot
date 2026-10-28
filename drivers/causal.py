"""Causal eligibility checks for stock-driver hypotheses.

This is deliberately separate from event clustering and verification.  It answers
only whether a candidate describes a concrete, timely event that could plausibly
explain a move.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import re
from typing import Any
from zoneinfo import ZoneInfo

from market_calendar import get_session
from news.event_language import asserted_event_type, is_future_preview


CAUSAL_EVENT_TYPES = {
    "REGULATORY", "EARNINGS", "EARNINGS_RESULTS", "GUIDANCE", "GUIDANCE_CHANGE", "M_AND_A",
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


def _text(candidate: dict[str, Any]) -> str:
    return " ".join(str(candidate.get(key) or "") for key in ("title", "summary", "canonical_event_title")).lower()


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        raw = str(value).strip()
        if len(raw) <= 10:
            return None  # Day precision cannot prove publication before close.
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=US_MARKET_TZ) if parsed.tzinfo is None else parsed.astimezone(US_MARKET_TZ)
    except (ValueError, TypeError):
        return None


def session_evidence_window(market_date: str) -> dict[str, str]:
    """Previous NYSE close (exclusive) through this close (inclusive). No I/O."""
    session = get_session(market_date)
    if session is None:
        raise ValueError("CATALYST_MARKET_DATE_NOT_A_SESSION")
    previous_day = session.opens_at.date() - timedelta(days=1)
    for _ in range(14):
        previous = get_session(previous_day)
        if previous:
            return {"window_start": previous.closes_at.isoformat(), "session_open": session.opens_at.isoformat(), "session_close": session.closes_at.isoformat()}
        previous_day -= timedelta(days=1)
    raise ValueError("CATALYST_PREVIOUS_SESSION_UNAVAILABLE")


def publication_time_relation(value: Any, market_date: str) -> str:
    """Classify publication within the calendar-defined session evidence window."""
    parsed = _timestamp(value)
    if parsed is None:
        return "UNKNOWN"
    try:
        window = session_evidence_window(market_date)
    except ValueError:
        return "UNKNOWN"
    if parsed > datetime.fromisoformat(window["session_close"]):
        return "FUTURE_EVENT"
    if parsed <= datetime.fromisoformat(window["window_start"]):
        return "BEFORE_EVIDENCE_WINDOW"
    return "BEFORE_OR_DURING_SESSION"


def _same_event_support(candidate: dict[str, Any], article: dict[str, Any]) -> bool:
    """Require identity/action support, not just an earlier cluster timestamp."""
    target = candidate.get("target_symbol")
    if not target or article.get("target_symbol") != target or article.get("target_role") not in {"PRIMARY_SUBJECT", "DIRECT_PRODUCT_EVENT"}:
        return False
    title, earlier = str(candidate.get("title") or ""), str(article.get("title") or "")
    if not earlier or is_future_preview(earlier, str(article.get("summary") or "")):
        return False
    # Same broad event family is not sufficient: retain specific quarter/year,
    # amount/product numbers, actor and action direction when they are stated.
    normalize = lambda text: " ".join(re.findall(r"\w+", text.casefold()))
    numbers = lambda text: set(re.findall(r"\b(?:q[1-4]|\d+(?:\.\d+)?)\b", text.lower()))
    if numbers(title) != numbers(earlier):
        return False
    actions = set(candidate.get("event_actions", [])) & set(article.get("event_actions", []))
    if candidate.get("topic_family") in {None, "", "UNSPECIFIED"} or candidate.get("topic_family") != article.get("topic_family") or not actions:
        return False
    if set(candidate.get("actors", [])) != set(article.get("actors", [])):
        return False
    negative = r"\b(?:cuts?|lower\w*|reduc\w*|downgrad\w*)\b"
    positive = r"\b(?:rais\w*|upgrad\w*)\b"
    if (re.search(negative, title, re.I) and re.search(positive, earlier, re.I)) or (re.search(positive, title, re.I) and re.search(negative, earlier, re.I)):
        return False
    if candidate.get("event_type") in {"EARNINGS", "EARNINGS_RESULTS", "GUIDANCE", "GUIDANCE_CHANGE", "ANALYST_ACTION"} and not asserted_event_type(earlier):
        return False
    left, right = set(normalize(title).split()), set(normalize(earlier).split())
    return len(left & right) / max(1, min(len(left), len(right))) >= 0.6


def timing_metadata(candidate: dict[str, Any], market_date: str) -> dict[str, Any]:
    representative = candidate.get("representative_published_at") or candidate.get("published_at")
    supported = [(representative, candidate.get("representative_title") or candidate.get("title"))] if _timestamp(representative) else []
    for article in candidate.get("cluster_articles", []):
        if not isinstance(article, dict) or not _same_event_support(candidate, article):
            continue
        timestamp = _timestamp(article.get("published_at"))
        original = _timestamp(representative)
        if timestamp and original and abs((original - timestamp).total_seconds()) <= 72 * 3600:
            supported.append((article["published_at"], article.get("title")))
    earliest, supporting_title = min(supported, key=lambda item: _timestamp(item[0])) if supported else (None, None)
    occurrence = candidate.get("event_occurred_at") or candidate.get("event_datetime") or candidate.get("event_date")
    relation = publication_time_relation(earliest, market_date)
    occurrence_relation = publication_time_relation(occurrence, market_date) if occurrence else "UNKNOWN"
    if occurrence:
        if occurrence_relation in {"FUTURE_EVENT", "BEFORE_EVIDENCE_WINDOW"}:
            relation = occurrence_relation
        elif _timestamp(occurrence) is None:
            try:
                event_day = date.fromisoformat(str(occurrence))
                if event_day > date.fromisoformat(market_date):
                    relation = "FUTURE_EVENT"
                elif event_day < datetime.fromisoformat(session_evidence_window(market_date)["window_start"]).date():
                    relation = "BEFORE_EVIDENCE_WINDOW"
            except ValueError:
                relation = "UNKNOWN"
    try:
        window = session_evidence_window(market_date)
    except ValueError:
        window = {}
    return {"event_occurred_at": occurrence, "earliest_supported_publication_at": earliest,
            "supporting_publication_title": supporting_title,
            "representative_published_at": representative, "event_time_relation": relation, **window}


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
    if is_future_preview(str(candidate.get("title") or ""), str(candidate.get("summary") or "")):
        reason = "FUTURE_EVENT_PREVIEW"
        causal_pass = False
    elif commentary and not (event_type in CAUSAL_EVENT_TYPES or actions):
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

    timing = timing_metadata(candidate, market_date)
    relation = timing["event_time_relation"]
    timing_pass = relation == "BEFORE_OR_DURING_SESSION"
    if causal_pass and not timing_pass:
        reason = "TIMING_INELIGIBLE"
    return {
        "subject_gate": "PASS" if subject_pass else "FAIL",
        "causal_gate": "PASS" if causal_pass and subject_pass else "FAIL",
        "causal_reason": reason,
        **timing,
        "causal_eligible": bool(causal_pass and subject_pass and timing_pass),
    }
