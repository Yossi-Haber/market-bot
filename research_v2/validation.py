"""Deterministic source checks; literal reporting is not semantic entailment."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from decimal import Decimal
from urllib.parse import urlsplit

from .contracts import CLAIM_SCHEMA, validate, validate_packet, validate_research
from .evidence import instant, temporal_tag

PUNCTUATION = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-", "−": "-"})


def quotation_span(text, quote):
    """Normalized exact match with offsets into the original source, never metadata."""
    def normalize(value):
        chars, offsets = [], []
        for index, char in enumerate(value):
            for mapped in unicodedata.normalize("NFKC", char).translate(PUNCTUATION):
                if mapped.isspace():
                    if not chars or chars[-1] == " ":
                        continue
                    mapped = " "
                chars.append(mapped)
                offsets.append(index)
        return "".join(chars).rstrip(), offsets
    source, offsets = normalize(text)
    needle, _ = normalize(quote)
    index = source.find(needle) if needle else -1
    if index < 0:
        return None
    start, end = offsets[index], offsets[index + len(needle) - 1] + 1
    return {"start": start, "end": end, "source_excerpt": text[start:end]}


def mentions(text, entity):
    return bool(entity.strip() and re.search(r"(?<!\w)" + re.escape(entity) + r"(?!\w)", text, re.I))


def numeric_tokens(text):
    return {(currency, Decimal(value.replace(",", "")), percent) for currency, value, percent in
            re.findall(r"(?<![\w.$€£+-])([$€£]?)([-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(%?)(?![\w%]|\.\d)", text)}


def source_identity(item, subject, policy):
    provenance = item.get("source_provenance", {})
    kind = provenance.get("kind", "UNKNOWN")
    origin, publisher = provenance.get("origin_url", ""), provenance.get("origin_publisher", "")
    if kind in {"UNKNOWN", "AGGREGATOR"} or not origin or not publisher:
        return None
    host, origin_host = urlsplit(item["original_url"]).hostname, urlsplit(origin).hostname
    if not origin_host or urlsplit(origin).scheme != "https":
        return None
    if kind != "SYNDICATED" and (host != origin_host or publisher.casefold() != item["publisher"].casefold()):
        return None
    # Authority comes from caller-owned policy, never from LLM labels or a filing flag.
    primary = policy.get("primary_sources", {}).get(host, {})
    authoritative = kind == "PRIMARY" and primary.get("publisher") == item["publisher"] and subject in primary.get("subjects", []) and subject in provenance.get("issuer_symbols", [])
    return {"origin": (origin_host.removeprefix("www."), publisher.strip().casefold()), "primary": authoritative}


def validate_claim(claim, packet, events, evidence):
    result = {"claim": claim, "verification_result": "REJECTED", "reasons": [], "source_references": [], "regular_session_eligible": False}
    def reject(reason, status="REJECTED"):
        result["verification_result"] = status
        result["reasons"].append(reason)
        return result
    try:
        validate(claim, CLAIM_SCHEMA)
    except (ValueError, TypeError):
        return reject("MALFORMED_CLAIM")
    if not claim["claim_id"].strip() or claim["event_id"] not in events or not claim["subject"].strip() or not claim["predicate"].strip():
        return reject("CLAIM_IDENTITY_INVALID")
    if not claim["citations"]:
        return reject("CITATION_REQUIRED")
    names = [claim["subject"], *packet["market_context"].get("entity_aliases", {}).get(claim["subject"], [])]
    if not any(mentions(claim["predicate"], name) for name in names):
        return reject("PREDICATE_SUBJECT_MISMATCH")
    valid_items = []
    for citation in claim["citations"]:
        item = evidence.get(citation["evidence_id"])
        if item is None:
            return reject("INVENTED_EVIDENCE_ID")
        if item["evidence_id"] not in events[claim["event_id"]]["evidence_refs"]:
            return reject("CITATION_NOT_LINKED_TO_EVENT")
        span = quotation_span(item["source_text"], citation["quote"])
        if span is None:
            return reject("QUOTE_NOT_IN_SOURCE_TEXT")
        result["source_references"].append({"evidence_id": item["evidence_id"], "publisher": item["publisher"],
            "original_url": item["original_url"], "published_at": item["published_at"], "temporal_tag": item["temporal_tag"], **span})
        quote = span["source_excerpt"]
        before = item["source_text"][:span["start"]].rstrip()
        after = item["source_text"][span["end"]:].lstrip()
        if (before and before[-1] not in ".!?") or (after and quote.rstrip()[-1] not in ".!?"):
            return reject("QUOTE_CONTEXT_INCOMPLETE", "SEMANTIC_REVIEW_REQUIRED")
        if not any(mentions(quote, name) for name in names) or any(not mentions(quote, entity) for entity in claim["entities"]):
            return reject("COMPANY_OR_ENTITY_MISMATCH")
        if numeric_tokens(claim["predicate"]) - numeric_tokens(quote):
            return reject("UNSUPPORTED_NUMERIC_ASSERTION")
        if quotation_span(quote, claim["predicate"]) is None:
            return reject("PREDICATE_ENTAILMENT_UNESTABLISHED", "SEMANTIC_REVIEW_REQUIRED")
        # A small contradiction screen, not a natural-language event classifier.
        paragraph_boundary = item["source_text"].rfind("\n\n", 0, span["start"])
        paragraph_start = paragraph_boundary + 2 if paragraph_boundary >= 0 else 0
        paragraph_end = item["source_text"].find("\n\n", span["end"])
        paragraph = item["source_text"][paragraph_start:paragraph_end if paragraph_end >= 0 else None]
        if re.search(r"\b(may|might|could|would|likely|upcoming|expected|expects|scheduled|will|plans to|preview|forecast|not|false|denied|rumor)\b", paragraph, re.I):
            return reject("PROSPECTIVE_UNCERTAIN_OR_NEGATED_SOURCE", "SEMANTIC_REVIEW_REQUIRED")
        event_type = events[claim["event_id"]]["event_type"].upper()
        if "UPGRADE" in event_type and not re.search(r"\bupgraded?\b", quote, re.I):
            return reject("UPGRADE_NOT_ESTABLISHED")
        if "UPGRADE" in event_type and re.search(r"\b(reiterat\w*|maintain\w*)\b", quote, re.I):
            return reject("REITERATION_IS_NOT_UPGRADE", "SEMANTIC_REVIEW_REQUIRED")
        if event_type in {"EARNINGS", "EARNINGS_RESULTS"} and not re.search(r"\b(?:reported|announced|released)\s+(?:(?:quarterly|annual|financial|q[1-4])\s+)*(?:earnings|results)\b(?!\s+(?:date|call|schedule))", quote, re.I):
            return reject("COMPLETED_EARNINGS_NOT_ESTABLISHED", "SEMANTIC_REVIEW_REQUIRED")
        if re.search(r"\b(because|due to|driven by|led to|caused|prompted)\b", claim["predicate"], re.I):
            return reject("CAUSAL_LANGUAGE_REQUIRES_REVIEW", "SEMANTIC_REVIEW_REQUIRED")
        valid_items.append(item)
    if claim["claim_type"] in {"INTERPRETATION", "CAUSAL_ATTRIBUTION"}:
        return reject("SEMANTIC_OR_CAUSAL_ENTAILMENT_UNESTABLISHED", "SEMANTIC_REVIEW_REQUIRED")
    direct_subject = any(re.match(re.escape(name) + r"\s+(reported|announced|released|acquired|raised|cut|signed|received|completed)\b", claim["predicate"], re.I) for name in names)
    rating_subject = any(re.search(r"\b(upgraded|reiterated|downgraded)\s+" + re.escape(name) + r"(?!\w)", claim["predicate"], re.I) for name in names)
    if claim["claim_type"] == "EVENT_FACT" and not (direct_subject or rating_subject):
        return reject("SUBJECT_ACTION_RELATION_REQUIRES_REVIEW", "SEMANTIC_REVIEW_REQUIRED")
    if claim["claim_type"] == "NUMERIC_FACT":
        if not claim["metric"].strip() or not claim["value"].strip():
            return reject("NUMERIC_CONTEXT_REQUIRED")
        values = numeric_tokens(claim["value"] + claim["unit"])
        def contextual_values(quote):
            match = re.search(r"(?<!\w)" + re.escape(claim["metric"]) + r"\s*(?:of\s+|was\s+|is\s+|:\s*)?([$€£]?[-+]?\d[\d,]*(?:\.\d+)?%?)(?![\w%]|\.\d)", quote, re.I)
            return numeric_tokens(match.group(1)) if match else set()
        if not values or any(values - contextual_values(c["quote"]) for c in claim["citations"]):
            return reject("NUMERIC_CONTEXT_UNSUPPORTED")
    if claim["event_time"]:
        try:
            tag = temporal_tag(claim["event_time"], packet["session"])
            instant(claim["event_time"])
        except (ValueError, TypeError):
            return reject("INVALID_EVENT_TIME")
        if tag == "OUTSIDE_WINDOW":
            return reject("EVENT_TIME_OUTSIDE_WINDOW")
        # Publication does not independently establish event time.
        return reject("EVENT_TIME_REQUIRES_SEMANTIC_REVIEW", "SEMANTIC_REVIEW_REQUIRED")
    result["verification_result"] = "APPROVED_SOURCE_FACT"
    result["regular_session_eligible"] = any(i["temporal_tag"] in {"PREVIOUS_AFTER_HOURS", "PRE_MARKET", "REGULAR_SESSION"} for i in valid_items)
    result["support_scope"] = "LITERAL_SOURCE_REPORT_NOT_SEMANTIC_ENTAILMENT"
    result["event_time_status"] = "UNKNOWN_PUBLICATION_IS_NOT_EVENT_TIME"
    if not result["regular_session_eligible"]:
        result["reasons"].append("POST_CLOSE_OR_UNCERTAIN_TIME")
    return result


def validate_ledger(packet, research, *, source_policy=None):
    validate_packet(packet)
    policy = source_policy or {}
    base = {"ledger_version": 2, "market_date": packet["market_date"], "approved_events": [], "rejected_events": [], "approved_claims": [],
            "rejected_claims": [], "movement_assessments": [], "coverage_limitations": packet["coverage"], "audit_trail": [], "citation_audit": []}
    try:
        if not isinstance(research, dict):
            raise ValueError("RESEARCH_NOT_OBJECT")
        # Validate claims individually so malformed claims get their own audit decision.
        validate_research({k: v for k, v in research.items() if k != "claims"}, packet)
        if "claims" in research and not isinstance(research["claims"], list):
            raise ValueError("CLAIMS_NOT_ARRAY")
    except (ValueError, KeyError, TypeError):
        return {**base, "status": "REJECTED_RESEARCH", "audit_trail": [{"reason": "RESEARCH_CONTRACT_OR_REFERENCE_INVALID"}]}
    evidence = {e["evidence_id"]: e for e in packet["evidence"]}
    events = {e["event_id"]: e for e in research["events"]}
    results = [validate_claim(c, packet, events, evidence) for c in research.get("claims", [])]
    for event in research["events"]:
        for q in event["supporting_quotations"]:
            item = evidence[q["evidence_id"]]
            span = quotation_span(item["source_text"], q["quote"])
            base["citation_audit"].append({"event_id": event["event_id"], "evidence_id": item["evidence_id"],
                "quote_match": span is not None, "publisher": item["publisher"], "published_at": item["published_at"], "span": span})
        bad_quotes = [q for q in event["supporting_quotations"] if quotation_span(evidence[q["evidence_id"]]["source_text"], q["quote"]) is None]
        if bad_quotes:
            for result in results:
                if isinstance(result["claim"], dict) and result["claim"].get("event_id") == event["event_id"]:
                    result.update(verification_result="REJECTED", reasons=["EVENT_QUOTE_NOT_IN_SOURCE_TEXT"])
            results.append({"claim": {"claim_id": event["event_id"] + ":quotations"}, "verification_result": "REJECTED",
                            "reasons": ["EVENT_QUOTE_NOT_IN_SOURCE_TEXT"], "source_references": []})
    ids = [r["claim"].get("claim_id") if isinstance(r["claim"], dict) else None for r in results]
    for result in results:
        if isinstance(result["claim"], dict) and ids.count(result["claim"].get("claim_id")) > 1:
            result.update(verification_result="REJECTED", reasons=["DUPLICATE_CLAIM_ID"])
    states = {}
    for event in research["events"]:
        facts = [r for r in results if r["verification_result"] == "APPROVED_SOURCE_FACT" and r["claim"]["event_id"] == event["event_id"]]
        fact_states = []
        for fact in facts:
            origins, texts, primary = set(), set(), False
            for ref in fact["source_references"]:
                item = evidence[ref["evidence_id"]]
                identity = source_identity(item, fact["claim"]["subject"], policy)
                if identity:
                    digest = hashlib.sha256(re.sub(r"\s+", " ", item["source_text"]).strip().encode()).hexdigest()
                    if digest not in texts:
                        origins.add(identity["origin"])
                        texts.add(digest)
                    primary |= identity["primary"]
            independent = len({o[0] for o in origins}) > 1 and len({o[1] for o in origins}) > 1
            fact_states.append("PRIMARY_CONFIRMED" if primary else "MULTI_SOURCE_REPORTED" if independent else "SINGLE_SOURCE_REPORTED" if origins else "UNVERIFIED")
            fact["event_verification"] = fact_states[-1]
        order = ["UNVERIFIED", "SINGLE_SOURCE_REPORTED", "MULTI_SOURCE_REPORTED", "PRIMARY_CONFIRMED"]
        state = min(fact_states, key=order.index) if fact_states else "UNVERIFIED"
        states[event["event_id"]] = (state, facts)
        destination = "approved_events" if facts and state != "UNVERIFIED" else "rejected_events"
        base[destination].append({"event_id": event["event_id"], "event_verification": state,
            "approved_fact_ids": [r["claim"]["claim_id"] for r in facts], "summary_status": "SEMANTIC_REVIEW_REQUIRED",
            "materiality": "NOT_DETERMINED", "uncertainties": event["unresolved_questions"] + (["SOURCE_INDEPENDENCE_OR_OCCURRENCE_UNESTABLISHED"] if state == "UNVERIFIED" else [])})
        if not facts:
            results.append({"claim": {"claim_id": event["event_id"] + ":summary", "predicate": event["factual_summary"]},
                "verification_result": "SEMANTIC_REVIEW_REQUIRED", "reasons": ["TYPED_SUPPORTED_CLAIM_REQUIRED"], "source_references": []})
    for movement in research["movement_assessments"]:
        eligible = [eid for eid in movement["linked_event_ids"] if states[eid][0] != "UNVERIFIED" and states[eid][1] and
            all(f["regular_session_eligible"] for f in states[eid][1]) and
            any(f["claim"]["subject"] == movement["subject"] for f in states[eid][1])]
        category, reason = "UNEXPLAINED", "NO_VALIDATED_CAUSAL_LINK"
        if movement["attribution"] == "SECTOR_CO_MOVEMENT":
            from .context import peer_context
            context = packet["market_context"]
            signals = peer_context(context.get("securities", []), context.get("peer_membership", {}), context.get("benchmarks", {}))["co_movement"]
            if any(movement["subject"] in s["members"] or movement["subject"] == s["group"] for s in signals):
                category, reason = "SECTOR_CO_MOVEMENT", "QUANTITATIVE_OBSERVATION_ONLY"
        elif eligible and movement["attribution"] in {"SUPPORTED_CATALYST", "PLAUSIBLE_CATALYST"}:
            category, reason = "PLAUSIBLE_CATALYST", "EVENT_REPORTED_CAUSALITY_NOT_INDEPENDENTLY_ESTABLISHED"
        base["movement_assessments"].append({"subject": movement["subject"], "requested_attribution": movement["attribution"],
            "approved_attribution": category, "linked_event_ids": eligible, "reasons": [reason],
            "rationale_status": "SEMANTIC_REVIEW_REQUIRED", "alternatives": movement["alternative_interpretations"],
            "abstention_reason": (movement["abstention_reason"] or reason) if category == "UNEXPLAINED" else ""})
    base["approved_claims"] = [r for r in results if r["verification_result"] == "APPROVED_SOURCE_FACT"]
    base["rejected_claims"] = [r for r in results if r["verification_result"] != "APPROVED_SOURCE_FACT"]
    base["audit_trail"] = [{"claim_id": r["claim"].get("claim_id") if isinstance(r["claim"], dict) else None, "decision": r["verification_result"], "reasons": r["reasons"]} for r in results]
    return {**base, "status": "VALIDATED_WITH_LIMITATIONS", "diagnostics": {"semantic_entailment_proven": False, "live_requests": 0}}
