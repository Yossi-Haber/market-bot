from drivers.causal import evaluate_causal_eligibility
from drivers.detector import classify_driver
from drivers.model import Driver


def candidate(**overrides):
    value = {
        "title": "FAA delays Boeing 737 Max 10 certification over software glitch",
        "summary": "FAA review of the certification timeline.",
        "event_type": "REGULATORY",
        "event_actions": ["SOFTWARE_GLITCH", "CERTIFICATION_DELAY", "REGULATORY_REVIEW"],
        "target_role": "PRIMARY_SUBJECT",
        "subject_score": 80,
        "published_at": "2026-09-28T12:00:00+00:00",
        "cluster_article_dates": ["2026-09-28T12:00:00+00:00"],
    }
    value.update(overrides)
    return value


def test_regulatory_cluster_is_causally_eligible():
    result = evaluate_causal_eligibility(candidate(), "2026-09-28")
    assert result["causal_gate"] == "PASS"
    assert result["causal_reason"] == "EVENT_SIGNAL_PRESENT"
    assert result["event_time_relation"] == "BEFORE_OR_DURING_SESSION"


def test_commentary_only_candidate_is_not_plausible():
    result = evaluate_causal_eligibility(
        candidate(
            title="A Look at DoorDash: GF Value, valuation and whether DASH is a buy",
            summary="Post-move valuation commentary.",
            event_type="OTHER",
            event_actions=[],
            target_role="PRIMARY_SUBJECT",
        ),
        "2026-09-28",
    )
    assert result["causal_gate"] == "FAIL"
    assert result["causal_reason"] == "COMMENTARY_ONLY"


def test_other_with_concrete_event_action_passes():
    result = evaluate_causal_eligibility(candidate(event_type="OTHER", event_actions=["CONTRACT"]), "2026-09-28")
    assert result["causal_gate"] == "PASS"


def test_future_selected_headline_is_rejected_even_with_prior_cluster_article():
    future = evaluate_causal_eligibility(candidate(published_at="2026-09-29T10:00:00+00:00", cluster_article_dates=["2026-09-29T10:00:00+00:00"]), "2026-09-28")
    assert future["event_time_relation"] == "FUTURE_EVENT"
    assert future["causal_eligible"] is False
    prior_cluster = evaluate_causal_eligibility(candidate(published_at="2026-09-29T10:00:00+00:00", cluster_article_dates=["2026-09-28T12:00:00+00:00", "2026-09-29T10:00:00+00:00"]), "2026-09-28")
    assert prior_cluster["event_time_relation"] == "FUTURE_EVENT"
    assert prior_cluster["causal_eligible"] is False


def test_secondary_subject_does_not_pass_subject_gate():
    result = evaluate_causal_eligibility(candidate(target_role="SECONDARY_MENTION", subject_score=10), "2026-09-28")
    assert result["subject_gate"] == "FAIL"
    assert result["causal_gate"] == "FAIL"


def test_no_causal_candidate_produces_no_catalyst_found():
    driver = Driver("STOCK_DASH", "STOCK_CATALYST", "STOCK", "down", 5.0, ["DASH"])
    result = classify_driver(driver, [], []).as_dict()
    assert result["verification_status"] == "NO_CATALYST_FOUND"
