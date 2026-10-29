"""Versioned strict JSON contracts shared by providers and offline replay."""
from .evidence import COVERAGE_STATES, instant, normalized_url, session_bounds, temporal_tag
import hashlib
import json


def obj(fields):
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


def array(items):
    return {"type": "array", "items": items}


TEXT = {"type": "string"}
STRINGS = array(TEXT)
MAPPING = {"type": "object"}
NULL_TEXT = {"type": ["string", "null"]}
EVIDENCE_SCHEMA = obj({k: TEXT for k in ("evidence_id", "source_provider", "publisher", "original_url", "normalized_url", "title", "source_text", "source_category", "acquisition_method")} | {
    "published_at": TEXT, "publication_timestamp_original": TEXT, "retrieved_at": NULL_TEXT, "symbols": STRINGS,
    "publication_precision": {"enum": ["TIMESTAMP", "DATE_ONLY"]},
    "content_completeness": {"enum": ["FULL_TEXT", "EXCERPT", "TITLE_ONLY"]},
    "temporal_tag": {"enum": ["PREVIOUS_AFTER_HOURS", "PRE_MARKET", "REGULAR_SESSION", "POST_CLOSE", "TIME_UNCERTAIN"]},
    "provider_limitations": STRINGS, "filing_metadata": MAPPING})
PACKET_SCHEMA = obj({"packet_version": {"const": 2}, "market_date": TEXT,
                     "session": obj({k: TEXT for k in ("timezone", "regular_open_utc", "regular_close_utc", "previous_session_close_utc")}),
                     "market_context": MAPPING, "movers": array(MAPPING), "peer_groups": array(MAPPING),
                     "evidence": array(EVIDENCE_SCHEMA), "coverage": array(obj({"provider": TEXT, "state": {"enum": sorted(COVERAGE_STATES)}, "limitations": STRINGS,
                         **{k: {"type": "integer", "minimum": 0} for k in ("accepted", "excluded", "budget_excluded", "incomplete")}})), "diagnostics": MAPPING})
QUOTE_SCHEMA = obj({"evidence_id": TEXT, "quote": TEXT})
EVENT_SCHEMA = obj({"event_id": TEXT, "scope": {"enum": ["company", "sector", "macro", "geopolitical", "other"]},
                    "event_type": TEXT, "securities": STRINGS, "sectors": STRINGS, "factual_summary": TEXT,
                    "evidence_refs": STRINGS, "supporting_quotations": array(QUOTE_SCHEMA),
                    "event_verification": {"enum": ["PRIMARY_CONFIRMED", "MULTI_SOURCE_REPORTED", "SINGLE_SOURCE_REPORTED", "UNVERIFIED"]},
                    "unresolved_questions": STRINGS})
ASSESSMENT_SCHEMA = obj({"subject": TEXT, "linked_event_ids": STRINGS,
                         "attribution": {"enum": ["SUPPORTED_CATALYST", "PLAUSIBLE_CATALYST", "SECTOR_CO_MOVEMENT", "UNEXPLAINED"]},
                         "rationale": TEXT, "alternative_interpretations": STRINGS, "evidence_refs": STRINGS, "abstention_reason": TEXT})
RESEARCH_SCHEMA = obj({"research_version": {"const": 2}, "proposal_only": {"const": True}, "events": array(EVENT_SCHEMA), "movement_assessments": array(ASSESSMENT_SCHEMA)})


def validate(value, schema, path="root"):
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        raise ValueError(f"SCHEMA_CONST:{path}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"SCHEMA_ENUM:{path}")
    types = {"object": dict, "array": list, "string": str, "integer": int, "null": type(None)}
    expected = schema.get("type")
    if expected and type(value) not in [types[t] for t in (expected if isinstance(expected, list) else [expected])]:
        raise ValueError(f"SCHEMA_TYPE:{path}")
    if "minimum" in schema and value < schema["minimum"]:
        raise ValueError(f"SCHEMA_MINIMUM:{path}")
    if isinstance(value, dict) and "properties" in schema:
        if not set(schema["required"]) <= value.keys() or (schema.get("additionalProperties") is False and value.keys() - schema["properties"].keys()):
            raise ValueError(f"SCHEMA_FIELDS:{path}")
        for key, child in schema["properties"].items():
            validate(value[key], child, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, child in enumerate(value):
            validate(child, schema["items"], f"{path}[{i}]")


def validate_packet(packet):
    json.dumps(packet, allow_nan=False)
    validate(packet, PACKET_SCHEMA)
    if packet["session"] != session_bounds(packet["market_date"]):
        raise ValueError("SESSION_BOUNDS_INVALID")
    ids = set()
    for item in packet["evidence"]:
        if not item["evidence_id"] or item["evidence_id"] in ids:
            raise ValueError("EVIDENCE_ID_INVALID")
        ids.add(item["evidence_id"])
        content = {k: v for k, v in item.items() if k != "evidence_id"}
        expected_id = "ev_" + hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if item["evidence_id"] != expected_id:
            raise ValueError("EVIDENCE_CONTENT_CHANGED")
        if not item["publisher"].strip() or not item["title"].strip() or not item["source_provider"].strip():
            raise ValueError("SOURCE_IDENTITY_MISSING")
        tag = temporal_tag(item["published_at"], packet["session"])
        if item["publication_precision"] == "DATE_ONLY" and tag != "OUTSIDE_WINDOW":
            tag = "TIME_UNCERTAIN"
        if normalized_url(item["original_url"]) != item["normalized_url"] or tag != item["temporal_tag"]:
            raise ValueError("EVIDENCE_PROVENANCE_INVALID")
        if item["retrieved_at"]:
            instant(item["retrieved_at"])
    return packet


def validate_research(result, packet):
    validate(result, RESEARCH_SCHEMA)
    evidence_ids = {item["evidence_id"] for item in packet["evidence"]}
    event_ids = [event["event_id"] for event in result["events"]]
    if len(set(event_ids)) != len(event_ids) or any(not eid for eid in event_ids):
        raise ValueError("EVENT_ID_INVALID")
    for event in result["events"]:
        if not event["evidence_refs"] or not event["supporting_quotations"] or not event["factual_summary"].strip():
            raise ValueError("EVENT_SUPPORT_REQUIRED")
        refs = event["evidence_refs"] + [q["evidence_id"] for q in event["supporting_quotations"]]
        if set(refs) - evidence_ids:
            raise ValueError("UNKNOWN_EVIDENCE_REFERENCE")
        if any(not q["quote"].strip() or q["evidence_id"] not in event["evidence_refs"] for q in event["supporting_quotations"]):
            raise ValueError("QUOTE_REFERENCE_INVALID")
    for assessment in result["movement_assessments"]:
        if set(assessment["linked_event_ids"]) - set(event_ids) or set(assessment["evidence_refs"]) - evidence_ids:
            raise ValueError("UNKNOWN_RESEARCH_REFERENCE")
        if assessment["attribution"] == "UNEXPLAINED" and not assessment["abstention_reason"].strip():
            raise ValueError("ABSTENTION_REASON_REQUIRED")
    return result
