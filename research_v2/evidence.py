"""Mechanical acquisition of recorded provider facts, without V1 event filters."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from market_calendar import NEW_YORK, get_session

COVERAGE_STATES = {"AVAILABLE", "NO_EVIDENCE", "UNAVAILABLE", "QUOTA_EXHAUSTED", "INCOMPLETE_CONTENT", "HYGIENE_EXCLUDED", "BUDGET_NOT_COLLECTED"}


def instant(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("TIMESTAMP_TIMEZONE_REQUIRED")
    return parsed.astimezone(timezone.utc)


def session_bounds(day):
    session = get_session(day)
    if session is None:
        raise ValueError("NOT_NYSE_SESSION")
    previous_day = datetime.fromisoformat(day).date() - timedelta(days=1)
    while get_session(previous_day) is None:
        previous_day -= timedelta(days=1)
    previous = get_session(previous_day)
    return {"timezone": "America/New_York", "regular_open_utc": session.opens_at.astimezone(timezone.utc).isoformat(),
            "regular_close_utc": session.closes_at.astimezone(timezone.utc).isoformat(),
            "previous_session_close_utc": previous.closes_at.astimezone(timezone.utc).isoformat()}


def temporal_tag(published, session):
    moment = instant(published)
    previous, opened, closed = (instant(session[k]) for k in ("previous_session_close_utc", "regular_open_utc", "regular_close_utc"))
    if moment < previous:
        return "OUTSIDE_WINDOW"
    if moment >= closed:
        return "POST_CLOSE" if moment.astimezone(NEW_YORK).date() == closed.astimezone(NEW_YORK).date() else "OUTSIDE_WINDOW"
    if moment >= opened:
        return "REGULAR_SESSION"
    return "PRE_MARKET" if moment.astimezone(NEW_YORK).date() == opened.astimezone(NEW_YORK).date() else "PREVIOUS_AFTER_HOURS"


def normalized_url(value):
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("INVALID_SOURCE_URL")
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")]
    if any(k.lower() in {"token", "api_key", "apikey", "key"} for k, _ in query):
        raise ValueError("CREDENTIAL_URL_REJECTED")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(sorted(query)), ""))


def recorded_items(provider, payload):
    """Read real raw response shapes, not provider-filtered event decisions."""
    rows = payload.get("data", []) if provider == "marketaux" and isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("RECORDED_PROVIDER_SHAPE_INVALID")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("RECORDED_ARTICLE_INVALID")
        published = row.get("published_at", row.get("datetime"))
        if isinstance(published, (int, float)):
            published = datetime.fromtimestamp(published, timezone.utc).isoformat()
        yield {
            "title": row.get("title", row.get("headline", "")),
            "original_url": row.get("source_url", row.get("url", "")),
            "publisher": row.get("source_name", row.get("source", row.get("source_domain", ""))),
            "published_at": published,
            "publication_timestamp_original": str(row.get("published_at", row.get("datetime", ""))),
            "publication_precision": "DATE_ONLY" if row.get("filing_form") and str(published).endswith("T00:00:00+00:00") else "TIMESTAMP",
            "retrieved_at": row.get("retrieved_at"),
            "symbols": row.get("affected_symbols", [e["symbol"] for e in row.get("entities", []) if e.get("symbol")]),
            "source_text": row.get("body") or row.get("text") or row.get("exhibit_text") or row.get("summary") or row.get("description") or "",
            "content_completeness": row.get("content_completeness") or ("FULL_TEXT" if row.get("body") else "EXCERPT" if (row.get("text") or row.get("exhibit_text") or row.get("summary") or row.get("description")) else "TITLE_ONLY"),
            "source_category": row.get("source_category", "FILING" if (row.get("filing_type") or row.get("filing_form")) else "NEWS"),
            "provider_limitations": row.get("provider_limitations", []) + (["V1_FILING_DATE_PLACEHOLDER_NOT_ACCEPTANCE_TIMESTAMP"] if row.get("filing_form") and str(published).endswith("T00:00:00+00:00") else []),
            "filing_metadata": {k: row[k] for k in ("filing_type", "filing_form", "filing_items", "sec_items", "exhibit_titles", "exhibit_text", "filing_excerpts") if k in row},
            **({"source_provenance": row["source_provenance"]} if "source_provenance" in row else {}),
        }


def build_packet(market_date, recordings, *, market_context=None, movers=None, peer_groups=None, max_items=100):
    """Recordings have provider, payload, coverage_state, limitations and optional spam URLs.

    No requests, classification, causal filtering, or production cache writes occur here.
    Budgets cap evidence items; they never authorize provider requests.
    """
    if type(max_items) is not int or max_items < 1:
        raise ValueError("EVIDENCE_BUDGET_INVALID")
    session = session_bounds(market_date)
    evidence, coverage, rejected, seen = [], [], {}, set()
    for recording in recordings:
        state = recording["coverage_state"]
        if state not in COVERAGE_STATES:
            raise ValueError("COVERAGE_STATE_INVALID")
        counts = {"accepted": 0, "excluded": 0, "budget_excluded": 0, "incomplete": 0}
        for item in recorded_items(recording["provider"], recording.get("payload", [])):
            try:
                if not item["title"].strip() or not item["publisher"].strip():
                    raise ValueError("SOURCE_IDENTITY_MISSING")
                url = normalized_url(item["original_url"])
                tag = temporal_tag(item["published_at"], session)
                if item["retrieved_at"]:
                    instant(item["retrieved_at"])
                if tag == "OUTSIDE_WINDOW":
                    raise ValueError("OUTSIDE_WINDOW")
                if item["publication_precision"] == "DATE_ONLY":
                    tag = "TIME_UNCERTAIN"
                if url in recording.get("spam_urls", []):
                    raise ValueError("SPAM_SUPPRESSED")
                key = (url, item["published_at"], item["title"])
                if key in seen:
                    raise ValueError("DUPLICATE")
                seen.add(key)
                if len(evidence) >= max_items:
                    counts["budget_excluded"] += 1
                    continue
                item.update(source_provider=recording["provider"], acquisition_method="RECORDED_PROVIDER_RESPONSE", normalized_url=url, temporal_tag=tag)
                item["evidence_id"] = "ev_" + hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                evidence.append(item)
                counts["accepted"] += 1
                counts["incomplete"] += item["content_completeness"] != "FULL_TEXT"
            except (ValueError, TypeError) as exc:
                reason = str(exc) if isinstance(exc, ValueError) else "INVALID_TIMESTAMP_OR_FIELD"
                rejected[reason] = rejected.get(reason, 0) + 1
                counts["excluded"] += 1
        coverage.append({"provider": recording["provider"], "state": state, "limitations": recording.get("limitations", []), **counts})
    return {"packet_version": 2, "market_date": market_date, "session": session, "market_context": market_context or {},
            "movers": movers or [], "peer_groups": peer_groups or [], "evidence": evidence, "coverage": coverage,
            "diagnostics": {"hygiene_rejections": rejected, "live_requests": 0, "origin": "RECORDED_INPUTS"}}
