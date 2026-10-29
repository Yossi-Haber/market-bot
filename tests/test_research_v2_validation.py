"""A2 uses synthetic issuer text, policy, dates and quantities only."""
import copy
import json

import pytest

from research_v2.evidence import build_packet, session_bounds
from research_v2.validation import quotation_span, validate_ledger
from research_v2.replay import replay

TEXT = "ORCL reported earnings and revenue growth of 8%."
POLICY = {"primary_sources": {"issuer.example": {"publisher": "Synthetic Issuer", "subjects": ["ORCL"]}}}


def case(text=TEXT, stamp="2026-10-28T15:00:00Z", day="2026-10-28", kind="PRIMARY", publisher="Synthetic Issuer", url="https://issuer.example/release"):
    row = {"title": "Synthetic results", "body": text, "source": publisher, "url": url, "published_at": stamp,
           "source_provenance": {"kind": kind, "origin_publisher": publisher, "origin_url": url, "issuer_symbols": ["ORCL"]}}
    packet = build_packet(day, [{"provider": "synthetic", "coverage_state": "AVAILABLE", "payload": [row]}])
    eid = packet["evidence"][0]["evidence_id"]
    claim = {"claim_id": "c1", "event_id": "e1", "claim_type": "EVENT_FACT", "subject": "ORCL", "predicate": text,
             "entities": ["ORCL"], "citations": [{"evidence_id": eid, "quote": text}], "event_time": None, "metric": "", "value": "", "unit": ""}
    research = {"research_version": 2, "proposal_only": True, "events": [{"event_id": "e1", "scope": "company", "event_type": "EARNINGS_RESULTS", "securities": ["ORCL"], "sectors": [], "factual_summary": "Unapproved paraphrase", "evidence_refs": [eid], "supporting_quotations": [{"evidence_id": eid, "quote": text}], "event_verification": "PRIMARY_CONFIRMED", "unresolved_questions": []}],
                "claims": [claim], "movement_assessments": [{"subject": "ORCL", "linked_event_ids": ["e1"], "attribution": "SUPPORTED_CATALYST", "rationale": "Unapproved cause", "alternative_interpretations": ["Other events may matter"], "evidence_refs": [eid], "abstention_reason": ""}]}
    return packet, research


def ledger(p, r):
    return validate_ledger(p, r, source_policy=POLICY)


def rejected(p, r, reason):
    result = ledger(p, r)
    assert not result["approved_claims"]
    assert any(reason in row["reasons"] for row in result["rejected_claims"])
    return result


def test_verbatim_primary_event_and_weak_attribution():
    p, r = case()
    result = ledger(p, r)
    assert result["approved_events"][0]["event_verification"] == "PRIMARY_CONFIRMED"
    assert result["approved_claims"][0]["source_references"][0]["source_excerpt"] == TEXT
    assert result["movement_assessments"][0]["approved_attribution"] == "PLAUSIBLE_CATALYST"
    assert result["approved_events"][0]["materiality"] == "NOT_DETERMINED"


def test_normalized_exact_span_and_no_fuzzy_match():
    source = '  ORCL announced “results”\n with growth.'
    span = quotation_span(source, 'ORCL announced "results" with growth.')
    assert source[span["start"]:span["end"]] == span["source_excerpt"]
    assert quotation_span(source, 'ORCL announced excellent results') is None
    assert quotation_span(source, '') is None


def test_invented_claim_citation():
    p, r = case()
    r["claims"][0]["citations"][0]["evidence_id"] = "invented"
    rejected(p, r, "INVENTED_EVIDENCE_ID")


def test_missing_body_and_metadata_quote():
    p, r = case("")
    r["claims"][0]["predicate"] = "ORCL reported earnings"
    r["claims"][0]["citations"][0]["quote"] = p["evidence"][0]["title"]
    r["events"][0]["supporting_quotations"][0]["quote"] = p["evidence"][0]["title"]
    rejected(p, r, "EVENT_QUOTE_NOT_IN_SOURCE_TEXT")


def test_mismatched_company():
    p, r = case()
    r["claims"][0]["subject"] = "MU"
    rejected(p, r, "PREDICATE_SUBJECT_MISMATCH")


def test_cited_source_names_a_different_company():
    p,r=case("MU reported earnings and revenue growth of 8%.")
    r["claims"][0]["predicate"]=TEXT
    rejected(p,r,"COMPANY_OR_ENTITY_MISMATCH")


def test_invented_numeric_assertion():
    p, r = case()
    r["claims"][0]["predicate"] = TEXT.replace("8%", "9%")
    rejected(p, r, "UNSUPPORTED_NUMERIC_ASSERTION")


@pytest.mark.parametrize("value,unit,approved", [("8", "%", True), ("8.0", "%", True), ("8", "", False), ("9", "%", False)])
def test_numeric_metric_and_unit_context(value, unit, approved):
    p, r = case()
    r["claims"][0].update(claim_type="NUMERIC_FACT", metric="revenue growth", value=value, unit=unit)
    assert bool(ledger(p, r)["approved_claims"]) is approved


def test_future_preview():
    p, r = case("ORCL will report earnings tomorrow.")
    rejected(p, r, "PROSPECTIVE_UNCERTAIN_OR_NEGATED_SOURCE")


def test_rating_reiteration_is_not_upgrade():
    p, r = case("UBS reiterated ORCL at Buy.")
    r["events"][0]["event_type"] = "ANALYST_UPGRADE"
    rejected(p, r, "UPGRADE_NOT_ESTABLISHED")


def test_literal_analyst_upgrade_can_be_reported():
    p, r = case("UBS upgraded ORCL to Buy.")
    r["events"][0]["event_type"] = "ANALYST_UPGRADE"
    assert ledger(p, r)["approved_claims"]


@pytest.mark.parametrize("stamp,expected", [("2026-10-27T21:00:00Z", "PLAUSIBLE_CATALYST"), ("2026-10-28T12:00:00Z", "PLAUSIBLE_CATALYST"), ("2026-10-28T20:00:00Z", "UNEXPLAINED")])
def test_regular_session_timing(stamp, expected):
    p, r = case(stamp=stamp)
    assert ledger(p, r)["movement_assessments"][0]["approved_attribution"] == expected


def test_early_close():
    p, r = case(day="2026-11-27", stamp="2026-11-27T18:01:00Z")
    assert ledger(p, r)["movement_assessments"][0]["approved_attribution"] == "UNEXPLAINED"


def test_holiday_dst_calendar():
    assert session_bounds("2026-11-02")["regular_open_utc"].endswith("14:30:00+00:00")
    assert session_bounds("2026-10-30")["regular_open_utc"].endswith("13:30:00+00:00")
    assert session_bounds("2026-11-27")["previous_session_close_utc"].startswith("2026-11-25")
    with pytest.raises(ValueError, match="NOT_NYSE_SESSION"):
        session_bounds("2026-11-26")


@pytest.mark.parametrize("kind", ["UNKNOWN", "AGGREGATOR"])
def test_unknown_independence_is_unverified(kind):
    p, r = case(kind=kind)
    result = ledger(p, r)
    assert result["rejected_events"][0]["event_verification"] == "UNVERIFIED"
    assert result["movement_assessments"][0]["approved_attribution"] == "UNEXPLAINED"


def add_source(p, r, *, text=TEXT, origin="https://wire.example/article", publisher="Wire", host="copy.example", kind="SYNDICATED", stamp="2026-10-28T16:00:00Z"):
    other, _ = case(text, stamp=stamp, kind=kind, publisher=publisher, url=f"https://{host}/article")
    item = other["evidence"][0]
    item["source_provenance"].update(origin_url=origin, origin_publisher=publisher)
    import hashlib
    item["evidence_id"] = "ev_" + hashlib.sha256(json.dumps({k:v for k,v in item.items() if k != "evidence_id"},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    p["evidence"].append(item)
    r["events"][0]["evidence_refs"].append(item["evidence_id"])
    r["claims"][0]["citations"].append({"evidence_id": item["evidence_id"], "quote": TEXT})


def test_syndication_and_identical_copy_not_independent():
    p, r = case(kind="INDEPENDENT", publisher="Wire", url="https://wire.example/article")
    add_source(p, r)
    assert ledger(p, r)["approved_events"][0]["event_verification"] == "SINGLE_SOURCE_REPORTED"


def test_independent_reporting_requires_origin_and_distinct_text():
    p, r = case(kind="INDEPENDENT", publisher="Wire", url="https://wire.example/article")
    add_source(p, r, text=TEXT + " Additional independent reporting.", origin="https://reporter.example/article", publisher="Reporter", host="reporter.example", kind="INDEPENDENT")
    assert ledger(p, r)["approved_events"][0]["event_verification"] == "MULTI_SOURCE_REPORTED"


def test_same_publisher_on_different_domains_is_not_independent():
    p, r = case(kind="INDEPENDENT", publisher="Wire", url="https://wire.example/article")
    add_source(p, r, text=TEXT + " Extra copy formatting.", origin="https://other.example/article", publisher="Wire", host="other.example", kind="INDEPENDENT")
    assert ledger(p, r)["approved_events"][0]["event_verification"] == "SINGLE_SOURCE_REPORTED"


def test_later_recap_cannot_supply_earlier_missing_facts():
    p, r = case()
    add_source(p, r, text="ORCL acquired Example Corp.", stamp="2026-10-28T21:00:00Z")
    r["claims"][0]["citations"].pop()
    late = copy.deepcopy(r["claims"][0])
    late.update(claim_id="late", predicate="ORCL acquired Example Corp.", citations=[{"evidence_id": p["evidence"][1]["evidence_id"], "quote": "ORCL acquired Example Corp."}])
    r["events"][0]["event_type"] = "OTHER"
    r["claims"].append(late)
    assert ledger(p, r)["movement_assessments"][0]["approved_attribution"] == "UNEXPLAINED"


@pytest.mark.parametrize("kind", ["INTERPRETATION", "CAUSAL_ATTRIBUTION"])
def test_semantic_claims_never_silently_approved(kind):
    p, r = case()
    r["claims"][0]["claim_type"] = kind
    result = rejected(p, r, "SEMANTIC_OR_CAUSAL_ENTAILMENT_UNESTABLISHED")
    assert result["rejected_claims"][0]["verification_result"] == "SEMANTIC_REVIEW_REQUIRED"


@pytest.mark.parametrize("malformed", [{}, None, {"claim_id": "bad"}])
def test_malformed_claims_are_audited(malformed):
    p, r = case()
    r["claims"] = [malformed]
    rejected(p, r, "MALFORMED_CLAIM")


def test_opaque_a1_summary_is_not_promoted():
    p, r = case()
    del r["claims"]
    assert not ledger(p, r)["approved_claims"]


def test_peer_co_movement_needs_no_news_and_no_cause():
    p, r = case()
    r.update(events=[], claims=[])
    r["movement_assessments"][0].update(subject="MU", linked_event_ids=[], evidence_refs=[], attribution="SECTOR_CO_MOVEMENT")
    p["market_context"] = {"securities": [{"symbol": s, "daily_return": v} for s,v in [("MU",5.86),("AMAT",5.07),("LRCX",5.39)]], "peer_membership": {"semi": ["MU","AMAT","LRCX"]}}
    result = ledger(p, r)
    assert result["movement_assessments"][0]["approved_attribution"] == "SECTOR_CO_MOVEMENT"
    p["market_context"] = {}
    assert ledger(p,r)["movement_assessments"][0]["approved_attribution"] == "UNEXPLAINED"


def test_explicit_abstention_and_policy_fail_closed():
    p, r = case()
    r["movement_assessments"][0].update(attribution="UNEXPLAINED", abstention_reason="Insufficient causal evidence")
    result = validate_ledger(p,r)
    assert result["approved_events"][0]["event_verification"] == "SINGLE_SOURCE_REPORTED"
    assert result["movement_assessments"][0]["approved_attribution"] == "UNEXPLAINED"


def test_offline_replay_exports_audit(tmp_path):
    p, r = case()
    for name, value in [("packet.json",p),("response.json",r),("policy.json",POLICY)]:
        (tmp_path/name).write_text(json.dumps(value))
    replay(tmp_path/'packet.json',tmp_path/'response.json',tmp_path/'out',tmp_path/'policy.json')
    output=json.loads((tmp_path/'out'/'validated_research_ledger.json').read_text())
    assert output['approved_events'][0]['event_verification']=='PRIMARY_CONFIRMED'
    assert (tmp_path/'out'/'validation_audit_trail.json').exists()


def test_matching_quote_does_not_prove_a_paraphrased_claim():
    p, r = case()
    r["claims"][0]["predicate"] = "ORCL earnings caused its shares to rise."
    rejected(p,r,"PREDICATE_ENTAILMENT_UNESTABLISHED")


def test_fabricated_quote_and_unrelated_metadata():
    p,r=case()
    r["claims"][0]["citations"][0]["quote"]="ORCL invented quote"
    rejected(p,r,"QUOTE_NOT_IN_SOURCE_TEXT")


def test_earnings_date_announcement_is_not_completed_results():
    p,r=case("ORCL announced earnings date for October 30.")
    rejected(p,r,"COMPLETED_EARNINGS_NOT_ESTABLISHED")


def test_numeric_field_cannot_borrow_another_metrics_value():
    p,r=case()
    r["claims"][0].update(claim_type="NUMERIC_FACT",metric="earnings",value="8",unit="%")
    rejected(p,r,"NUMERIC_CONTEXT_UNSUPPORTED")


def test_duplicate_claim_ids_are_all_blocked():
    p,r=case()
    r["claims"].append(copy.deepcopy(r["claims"][0]))
    rejected(p,r,"DUPLICATE_CLAIM_ID")


def test_unsupported_event_time_and_later_source_do_not_authorize_attribution():
    p,r=case()
    r["claims"][0]["event_time"]="2026-10-28T14:00:00Z"
    rejected(p,r,"EVENT_TIME_REQUIRES_SEMANTIC_REVIEW")


def test_quote_cannot_cut_out_negation_context():
    p,r=case("It is false that ORCL reported earnings.")
    r["claims"][0].update(predicate="ORCL reported earnings.",citations=[{"evidence_id":p["evidence"][0]["evidence_id"],"quote":"ORCL reported earnings."}])
    rejected(p,r,"QUOTE_CONTEXT_INCOMPLETE")


def test_quoted_sentence_cannot_ignore_paragraph_denial():
    p,r=case("The next sentence is false. ORCL reported earnings.")
    r["claims"][0].update(predicate="ORCL reported earnings.",citations=[{"evidence_id":p["evidence"][0]["evidence_id"],"quote":"ORCL reported earnings."}])
    rejected(p,r,"PROSPECTIVE_UNCERTAIN_OR_NEGATED_SOURCE")
