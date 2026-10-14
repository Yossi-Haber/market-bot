from evaluation.concrete_event_classification_trial_v1 import (
    classify_concrete_event,
    evaluate_trial_candidate,
)


def cand(title, *, summary="", role="PRIMARY_SUBJECT", published="2026-09-10T14:13:03+00:00", symbol="ELV"):
    return {
        "title": title, "summary": summary, "target_role": role,
        "subject_score": 48 if role == "PRIMARY_SUBJECT" else 0,
        "affected_symbols": [symbol], "target_symbol": symbol,
        "published_at": published,
    }


def test_a_same_day_explicit_guidance_reaffirmation_is_concrete():
    assert classify_concrete_event("Elevance reaffirms FY2026 adjusted EPS guidance")[0] == "GUIDANCE"


def test_b_guidance_raise_lower_and_reaffirm_verbs_classify():
    for title in ("Company raises EPS guidance", "Company lowers EPS outlook", "Company reaffirms earnings guidance"):
        assert classify_concrete_event(title)[0] == "GUIDANCE"


def test_c_material_operational_management_disclosure_classifies():
    event, _ = classify_concrete_event(
        "Management disclosed a manufacturing constraint", "The company reported a production delay."
    )
    assert event == "OPERATIONAL_UPDATE"


def test_d_generic_commentary_remains_other():
    assert classify_concrete_event("Should You Buy Elevance Health Stock For The Margin It Lost?")[0] == "OTHER"


def test_e_post_earnings_recap_remains_other():
    assert classify_concrete_event("Post-earnings recap: investors weigh the quarter")[0] == "OTHER"


def test_f_valuation_article_remains_other():
    assert classify_concrete_event("Is AMAT undervalued after its earnings?")[0] == "OTHER"


def test_g_stock_move_recap_remains_other():
    assert classify_concrete_event("Why the stock moved higher today")[0] == "OTHER"


def test_h_direct_subject_is_required_for_eligibility():
    item = cand("Elevance shares move higher after FY26 EPS reaffirmation", role="INCIDENTAL_MENTION")
    assert not evaluate_trial_candidate(item, "2026-09-10", 4.9, "ELV")["trial_eligible"]


def test_i_timing_safety_preserved():
    item = cand("Elevance shares move higher after FY26 EPS reaffirmation", published="2026-09-11T12:00:00+00:00")
    assert not evaluate_trial_candidate(item, "2026-09-10", 4.9, "ELV")["trial_eligible"]


def test_j_future_events_never_eligible():
    item = cand("Elevance shares move higher after FY26 EPS reaffirmation", published="2026-09-11T12:00:00+00:00")
    result = evaluate_trial_candidate(item, "2026-09-10", 4.9, "ELV")
    assert result["event_time_relation"] == "FUTURE_EVENT"
    assert not result["trial_eligible"]


def test_k_cross_symbol_candidate_cannot_become_eligible():
    item = cand("Elevance shares move higher after FY26 EPS reaffirmation", symbol="UNH")
    assert not evaluate_trial_candidate(item, "2026-09-10", 4.9, "ELV")["trial_eligible"]


def test_l_subject_matching_inputs_are_not_rewritten():
    item = cand("Elevance shares move higher after FY26 EPS reaffirmation")
    before = (item["target_role"], item["subject_score"], list(item["affected_symbols"]))
    evaluate_trial_candidate(item, "2026-09-10", 4.9, "ELV")
    assert (item["target_role"], item["subject_score"], item["affected_symbols"]) == before


def test_m_amat_negative_control_stays_ineligible():
    item = cand("Applied Materials: When Beating Wall Street Numbers Isn't Enough", role="INCIDENTAL_MENTION", symbol="AMAT")
    result = evaluate_trial_candidate(item, "2026-08-17", 5.55, "AMAT")
    assert result["trial_event_type"] == "OTHER"
    assert result["classifier_reason"] == "COMMENTARY_OR_NO_EXPLICIT_EVENT"
    assert not result["trial_eligible"]


def test_elv_explicit_same_day_up_move_passes_only_existing_gates():
    item = cand("Elevance Health Stock is Moving Higher After FY26 EPS Reaffirmation")
    result = evaluate_trial_candidate(item, "2026-09-10", 4.948, "ELV")
    assert result["trial_event_type"] == "GUIDANCE"
    assert result["subject_gate"] == "PASS"
    assert result["event_time_relation"] == "BEFORE_OR_DURING_SESSION"
    assert result["direction_compatible"] is True
    assert result["trial_eligible"] is True
