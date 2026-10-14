from drivers.market_driver_discovery import (
    MAX_QUERY_PHASES,
    build_market_driver_queries,
    build_market_discovery_result,
    deduplicate_market_stories,
    filter_market_candidate,
    normalize_market_story,
    load_market_discovery_cache,
    load_market_discovery_cache_state,
    save_market_discovery_cache,
)


HYPOTHESIS = {"hypothesis_id": "RISING_YIELDS", "direction": "down"}


def test_stock_specific_article_cannot_support_market_driver():
    story = normalize_market_story({"title": "PANW cybersecurity market report", "affected_symbols": ["PANW"], "published_at": "2026-09-28"}, market_date="2026-09-28")
    result = filter_market_candidate(story, HYPOTHESIS)
    assert result["market_eligible"] is False
    assert result["rejection_reason"] == "LOW_MARKET_SUBJECT_RELEVANCE"


def test_explicit_yields_story_scores_as_market_relevant():
    story = normalize_market_story({"title": "Nasdaq falls as Treasury yields jump", "published_at": "2026-09-28"}, market_date="2026-09-28")
    assert story["causal_link_type"] == "EXPLICIT_AS"
    result = filter_market_candidate(story, HYPOTHESIS)
    assert result["market_subject_score"] >= 30
    assert result["hypothesis_match_score"] >= 30


def test_separate_facts_without_causal_link_score_lower():
    linked = normalize_market_story({"title": "Nasdaq falls as Treasury yields jump", "published_at": "2026-09-28"}, market_date="2026-09-28")
    separate = normalize_market_story({"title": "Treasury yields and stocks in focus", "published_at": "2026-09-28"}, market_date="2026-09-28")
    linked_result = filter_market_candidate(linked, HYPOTHESIS)
    separate_result = filter_market_candidate(separate, HYPOTHESIS)
    assert separate["causal_link_type"] == "NO_EXPLICIT_LINK"
    assert separate_result["hypothesis_match_score"] < linked_result["hypothesis_match_score"]


def test_commentary_and_future_story_are_rejected():
    commentary = normalize_market_story({"title": "What investors should know about Treasury yields", "published_at": "2026-09-28"}, market_date="2026-09-28")
    future = normalize_market_story({"title": "Nasdaq falls as Treasury yields jump", "published_at": "2026-09-29"}, market_date="2026-09-28")
    assert filter_market_candidate(commentary, HYPOTHESIS)["rejection_reason"] == "COMMENTARY_ONLY"
    assert filter_market_candidate(future, HYPOTHESIS)["rejection_reason"] == "FUTURE_EVENT"


def test_syndicated_copies_dedupe_but_keep_independent_source_count():
    stories = [
        normalize_market_story({"title": "Nasdaq falls as Treasury yields jump", "source_name": "Reuters", "published_at": "2026-09-28"}, market_date="2026-09-28"),
        normalize_market_story({"title": "Nasdaq falls as Treasury yields jump", "source_name": "AP", "published_at": "2026-09-28"}, market_date="2026-09-28"),
    ]
    clusters = deduplicate_market_stories(stories, "2026-09-28")
    assert len(clusters) == 1
    assert clusters[0]["independent_source_count"] == 2
    assert clusters[0]["syndicated_count"] == 2


def test_marketaux_query_plan_is_bounded_and_has_fallback_phase():
    primary = build_market_driver_queries({"hypothesis_id": "RISK_OFF_VOLATILITY_SHOCK"}, "2026-09-28")
    fallback = build_market_driver_queries({"hypothesis_id": "RISK_OFF_VOLATILITY_SHOCK"}, "2026-09-28", phase=2)
    assert primary and fallback
    assert all("2026-09-28" in item["query"] for item in primary + fallback)
    assert MAX_QUERY_PHASES == 2


def test_discovery_result_persists_rejection_diagnostics():
    result = build_market_discovery_result(HYPOTHESIS, [{"title": "PANW cybersecurity market report", "affected_symbols": ["PANW"], "published_at": "2026-09-28"}], "2026-09-28")
    assert result["candidates_found"] == 1
    assert result["metrics"]["eligible_count"] == 0
    assert result["rejected_candidates"][0]["rejection_reason"] == "LOW_MARKET_SUBJECT_RELEVANCE"


def test_shared_cache_loader_states_and_hit_candidates(tmp_path):
    assert load_market_discovery_cache_state(tmp_path, "2026-09-28")["state"] == "MISS"
    payload = {"market_date": "2026-09-28", "offline_ready": True, "discovery_completed": True, "point_in_time_status": "PASS", "hypotheses": [{"hypothesis_id": "RISK_OFF_VOLATILITY_SHOCK", "market_eligible_candidates": [{"title": "Reuters broad market story"}]}], "provider_metrics": {}}
    save_market_discovery_cache(tmp_path, "2026-09-28", payload)
    state = load_market_discovery_cache_state(tmp_path, "2026-09-28")
    assert state["state"] == "HIT"
    assert state["eligible_candidates"] == [{"title": "Reuters broad market story"}]
    assert load_market_discovery_cache(tmp_path, "2026-09-28")["market_date"] == "2026-09-28"


def test_shared_cache_loader_rejects_schema_date_and_integrity_failures(tmp_path):
    base = {"market_date": "2026-09-27", "offline_ready": True, "discovery_completed": True, "point_in_time_status": "PASS", "hypotheses": [], "provider_metrics": {}}
    save_market_discovery_cache(tmp_path, "2026-09-28", base)
    assert load_market_discovery_cache_state(tmp_path, "2026-09-28")["state"] == "DATE_MISMATCH"
    bad = {"market_date": "2026-09-28", "offline_ready": True, "discovery_completed": True, "point_in_time_status": "FAIL", "hypotheses": [], "provider_metrics": {}}
    save_market_discovery_cache(tmp_path, "2026-09-28", bad)
    assert load_market_discovery_cache_state(tmp_path, "2026-09-28")["state"] == "INTEGRITY_FAIL"
    (tmp_path / "market_driver_news" / "2026-09-28.json").write_text("[]", encoding="utf-8")
    assert load_market_discovery_cache_state(tmp_path, "2026-09-28")["state"] == "INVALID_SCHEMA"
