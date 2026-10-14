import logging
from datetime import datetime, timezone

import pytest
import requests

from news.discovery import DiscoveryCandidate
from news.gdelt import GdeltClient
from news.live import LiveNewsCoverage, _is_relevant_mover_candidate, _rank_targeted_candidates
from debug_news_coverage import validate_subject_gate
from news.performance import NewsBudgetExhausted, NewsPerformance, ProviderCircuitOpen, ProviderRequest
from news.schema import NewsCandidate


def _candidate(provider="marketaux"):
    return NewsCandidate(
        "Company announces earnings",
        "event",
        "2026-09-25T12:00:00+00:00",
        "Reuters",
        "reuters.com",
        2,
        "https://reuters.com/story",
        "EARNINGS",
        ["NVDA"],
        provider,
        False,
    )


def test_marketaux_candidate_prevents_unnecessary_slow_fallback(monkeypatch):
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    coverage = LiveNewsCoverage(logging.getLogger("news-test"), "2026-09-25")
    coverage.official.collect = lambda symbols=None: []
    coverage.marketaux.token = "configured"
    coverage.marketaux.search = lambda *args, **kwargs: [_candidate()]
    coverage.slow_discovery.search = lambda *args, **kwargs: pytest.fail("slow fallback should not run")
    stories, budget = coverage.collect(mover_symbols=["NVDA"])
    assert stories
    assert budget["slow_fallback_requests"] == 0


def test_configured_marketaux_executes_before_slow_fallback(monkeypatch):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "false")
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    monkeypatch.setenv("NEWS_DISABLE_SLOW_FALLBACK", "true")
    monkeypatch.setenv("MARKETAUX_API_TOKEN", "configured")
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": []}

    coverage = LiveNewsCoverage(logging.getLogger("news-test"), "2026-09-25", force_live=True, request_get=lambda *args, **kwargs: Response())
    events = []
    original_search = coverage.marketaux.search
    coverage.marketaux.search = lambda *args, **kwargs: events.append("marketaux") or original_search(*args, **kwargs)
    coverage.slow_discovery.search = lambda *args, **kwargs: events.append("slow") or ([], {}, [])
    _, budget = coverage.collect(mover_symbols=["NVDA"])
    assert events[0] == "marketaux"
    assert budget["marketaux_enabled"] is True
    assert budget["marketaux_requests_attempted"] > 0


def test_marketaux_missing_key_has_explicit_reason(monkeypatch):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "false")
    monkeypatch.delenv("MARKETAUX_API_TOKEN", raising=False)
    coverage = LiveNewsCoverage(logging.getLogger("news-test"), "2026-09-25", force_live=True)
    _, budget = coverage.collect()
    assert budget["marketaux_enabled"] is False
    assert budget["marketaux_disabled_reason"] == "MARKETAUX_API_KEY_MISSING"


def _verified_official(title="Official earnings"):
    item = _candidate(provider="official")
    item.title = title
    item.official = True
    item.source_tier = 1
    item.verification = "VERIFIED"
    item.editorial_eligible = True
    return item


def _target_candidate(symbol="NVDA"):
    return NewsCandidate(
        f"{symbol} announces earnings", "reported revenue", "2026-09-25T12:00:00Z", "Marketaux", "reuters.com", 2,
        "https://reuters.com/story", "EARNINGS", [symbol], "marketaux", False,
    )


def test_coverage_target_met_by_general_discovery_skips_escalation(monkeypatch):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "true")
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    monkeypatch.setenv("NEWS_DISABLE_SLOW_FALLBACK", "true")
    coverage = LiveNewsCoverage(logging.getLogger("news-test"), "2026-09-25", perform_verification=True)
    coverage.marketaux.token = "configured"
    coverage.marketaux.search = lambda *args, **kwargs: []
    coverage.official.collect = lambda symbols=None: [_verified_official("First"), _verified_official("Second")]
    _, budget = coverage.collect(mover_symbols=["A"], mover_details=[{"symbol": "A", "change_pct": 5}])
    assert budget["verified_stories_before_escalation"] == 2
    assert budget["targeted_movers_queried"] == 0
    assert budget["news_coverage_status"] == "SUFFICIENT"


def test_coverage_escalation_finds_second_story_and_stops_early(monkeypatch):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "true")
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    monkeypatch.setenv("NEWS_DISABLE_SLOW_FALLBACK", "true")
    coverage = LiveNewsCoverage(logging.getLogger("news-test"), "2026-09-25", perform_verification=True)
    coverage.marketaux.token = "configured"
    target = _target_candidate()
    def search_target(query, query_type, symbols=None):
        if query_type == "TARGETED_MOVER":
            coverage.marketaux.requests_attempted += 1
            return [target]
        return []
    coverage.marketaux.search = search_target
    coverage.official.collect = lambda symbols=None: [_verified_official("First")]
    coverage.verification_acquirer.acquire = lambda candidates: {
        candidates[0]["title"]: ([{"source_type": "OFFICIAL", "source_domain": "sec.gov", "source_name": "SEC EDGAR", "source_url": "https://sec.gov", "title": candidates[0]["title"], "published_at": candidates[0]["published_at"], "affected_symbols": ["NVDA"]}], None)
    }
    stories, budget = coverage.collect(mover_symbols=["NVDA", "A"], mover_details=[{"symbol": "NVDA", "change_pct": 5}, {"symbol": "A", "change_pct": 1}])
    assert budget["verified_stories_before_escalation"] == 1
    assert budget["verified_stories_after_escalation"] == 2
    assert budget["targeted_movers_queried"] == 1
    assert budget["news_coverage_status"] == "SUFFICIENT"
    assert any(item.get("verification") == "VERIFIED" for item in stories)


def test_coverage_escalation_targets_largest_four_movers(monkeypatch):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "false")
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    monkeypatch.setenv("NEWS_DISABLE_SLOW_FALLBACK", "true")
    coverage = LiveNewsCoverage(logging.getLogger("news-test"), "2026-09-25", perform_verification=True)
    coverage.marketaux.token = "configured"
    queried = []
    def search_target(query, query_type, symbols=None):
        if query_type == "TARGETED_MOVER":
            queried.append(symbols[0])
            coverage.marketaux.requests_attempted += 1
        return []
    coverage.marketaux.search = search_target
    coverage.official.collect = lambda symbols=None: []
    details = [{"symbol": symbol, "change_pct": move} for symbol, move in (("A", 1), ("B", 9), ("C", 4), ("D", 7), ("E", 6))]
    _, budget = coverage.collect(mover_symbols=[item["symbol"] for item in details], mover_details=details)
    assert queried == ["B", "D", "E", "C"]
    assert budget["targeted_movers_considered"] == 4
    assert budget["targeted_movers_queried"] == 4
    assert budget["news_coverage_status"] == "INSUFFICIENT"


def test_targeted_escalation_counts_only_real_provider_requests(monkeypatch):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "false")
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    monkeypatch.setenv("NEWS_DISABLE_SLOW_FALLBACK", "true")
    coverage = LiveNewsCoverage(logging.getLogger("news-test"), "2026-09-25", perform_verification=True)
    coverage.marketaux.token = "configured"
    calls = []

    def search_target(query, query_type, symbols=None):
        if query_type == "TARGETED_MOVER":
            calls.append((query, query_type, symbols))
            coverage.marketaux.requests_attempted += 1
        return []

    coverage.marketaux.search = search_target
    coverage.official.collect = lambda symbols=None: []
    details = [{"symbol": symbol, "company_name": f"Company {symbol}", "change_pct": 10 - index} for index, symbol in enumerate(("A", "B", "C", "D"))]
    _, budget = coverage.collect(mover_symbols=[item["symbol"] for item in details], mover_details=details)
    assert len(calls) == 4
    assert budget["targeted_movers_considered"] == 4
    assert budget["targeted_movers_queried"] == 4
    assert budget["targeted_marketaux_requests"] == 4
    assert budget["targeted_cache_hits"] == 0
    assert budget["coverage_escalation_seconds"] > 0
    assert calls[0][0] == ""


def test_targeted_cache_hit_is_explicit_and_not_counted_as_provider_query(monkeypatch):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "false")
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    monkeypatch.setenv("NEWS_DISABLE_SLOW_FALLBACK", "true")
    coverage = LiveNewsCoverage(logging.getLogger("news-test"), "2026-09-25", perform_verification=True)
    coverage.marketaux.token = "configured"

    def cached_search(query, query_type, symbols=None):
        coverage.marketaux.cache_hits += 1
        return []

    coverage.marketaux.search = cached_search
    coverage.official.collect = lambda symbols=None: []
    _, budget = coverage.collect(mover_symbols=["A", "B"], mover_details=[{"symbol": "A", "change_pct": 2}, {"symbol": "B", "change_pct": 1}])
    assert budget["targeted_movers_considered"] == 2
    assert budget["targeted_movers_queried"] == 0
    assert budget["targeted_cache_hits"] == 2
    assert budget["targeted_marketaux_requests"] == 0


def test_targeted_escalation_records_missing_token_skip(monkeypatch):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "false")
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    monkeypatch.delenv("MARKETAUX_API_TOKEN", raising=False)
    coverage = LiveNewsCoverage(logging.getLogger("news-test"), "2026-09-25", perform_verification=True)
    coverage.official.collect = lambda symbols=None: []
    _, budget = coverage.collect(mover_symbols=["A"], mover_details=[{"symbol": "A", "change_pct": 2}])
    assert budget["targeted_movers_considered"] == 1
    assert budget["coverage_escalation_skipped_reason"] == "MARKETAUX_API_KEY_MISSING"
    assert budget["targeted_marketaux_requests"] == 0


def test_targeted_escalation_records_budget_skip(monkeypatch):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "false")
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    monkeypatch.setenv("MARKETAUX_API_TOKEN", "configured")
    coverage = LiveNewsCoverage(logging.getLogger("news-test"), "2026-09-25", perform_verification=True, budget_seconds=0)
    coverage.official.collect = lambda symbols=None: []
    _, budget = coverage.collect(mover_symbols=["A"], mover_details=[{"symbol": "A", "change_pct": 2}])
    assert budget["coverage_escalation_skipped_reason"] == "NEWS_BUDGET_EXHAUSTED"
    assert budget["targeted_movers_queried"] == 0


def test_marketaux_metadata_alone_does_not_meet_coverage_target(monkeypatch):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "false")
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    monkeypatch.setenv("NEWS_DISABLE_SLOW_FALLBACK", "true")
    coverage = LiveNewsCoverage(logging.getLogger("news-test"), "2026-09-25", perform_verification=True)
    coverage.marketaux.token = "configured"
    coverage.marketaux.search = lambda query, query_type, symbols=None: [_target_candidate(symbols[0])] if query_type == "TARGETED_MOVER" else []
    coverage.official.collect = lambda symbols=None: []
    coverage.verification_acquirer.acquire = lambda candidates: {candidates[0]["title"]: ([], None)}
    _, budget = coverage.collect(mover_symbols=["NVDA"], mover_details=[{"symbol": "NVDA", "change_pct": 5}])
    assert budget["verified_stories_after_escalation"] == 0
    assert budget["news_coverage_status"] == "INSUFFICIENT"


def test_targeted_candidate_reaches_existing_verifier(monkeypatch):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "false")
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    monkeypatch.setenv("NEWS_DISABLE_SLOW_FALLBACK", "true")
    coverage = LiveNewsCoverage(logging.getLogger("news-test"), "2026-09-25", perform_verification=True)
    coverage.marketaux.token = "configured"
    candidate = _target_candidate("DASH")
    candidate.title = "DoorDash reports quarterly revenue"
    coverage.marketaux.search = lambda query, query_type, symbols=None: (setattr(coverage.marketaux, "requests_attempted", coverage.marketaux.requests_attempted + 1) or [candidate]) if query_type == "TARGETED_MOVER" else []
    coverage.official.collect = lambda symbols=None: []
    def acquire(items):
        coverage.verification_acquirer.request_count += 1
        return {items[0]["title"]: ([], None)}
    coverage.verification_acquirer.acquire = acquire
    _, budget = coverage.collect(mover_symbols=["DASH"], mover_details=[{"symbol": "DASH", "company_name": "DoorDash Inc.", "change_pct": 5}])
    assert budget["targeted_diagnostics"][0]["after_relevance_filter"] == 1
    assert budget["targeted_diagnostics"][0]["verification_candidates"] == 1
    assert budget["targeted_verification_requests"] >= 1


def test_targeted_acquisition_attempts_are_preserved(monkeypatch):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "false")
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    monkeypatch.setenv("NEWS_DISABLE_SLOW_FALLBACK", "true")
    coverage = LiveNewsCoverage(logging.getLogger("news-test"), "2026-09-25", perform_verification=True)
    coverage.marketaux.token = "configured"
    candidate = _target_candidate("NVDA")
    coverage.marketaux.search = lambda *args, **kwargs: (setattr(coverage.marketaux, "requests_attempted", coverage.marketaux.requests_attempted + 1) or [candidate])
    coverage.official.collect = lambda symbols=None: []
    def acquire(items):
        coverage.verification_acquirer.request_count += 1
        diagnostics = type("D", (), {"attempted_sources": [{"type": "TIER2_SEARCH", "status": "CHECKED"}]})()
        return {items[0]["title"]: ([], diagnostics)}
    coverage.verification_acquirer.acquire = acquire
    _, budget = coverage.collect(mover_symbols=["NVDA"], mover_details=[{"symbol": "NVDA", "change_pct": 5}])
    assert budget["targeted_verification_requests"] == 1
    assert budget["targeted_verification_sources_attempted"] == 1
    assert budget["targeted_tier2_requests"] == 1
    assert budget["targeted_verification_no_source_attempt"] == 0
    assert budget["verification_diagnostics"][-1]["attempted_sources"]


def test_targeted_missing_schema_is_explicit(monkeypatch):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "false")
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    monkeypatch.setenv("NEWS_DISABLE_SLOW_FALLBACK", "true")
    coverage = LiveNewsCoverage(logging.getLogger("news-test"), "2026-09-25", perform_verification=True)
    coverage.marketaux.token = "configured"
    candidate = _target_candidate("NVDA")
    candidate.source_url = ""
    coverage.marketaux.search = lambda query, query_type, symbols=None: [candidate] if query_type == "TARGETED_MOVER" else []
    coverage.official.collect = lambda symbols=None: []
    called = []
    coverage.verification_acquirer.acquire = lambda items: called.append(items) or {}
    _, budget = coverage.collect(mover_symbols=["NVDA"], mover_details=[{"symbol": "NVDA", "change_pct": 5}])
    assert not called
    assert budget["targeted_verification_schema_failures"] == 1
    assert budget["targeted_verification_no_source_attempt"] == 1


def test_targeted_no_ticker_full_company_name_survives_relevance(monkeypatch):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "false")
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    monkeypatch.setenv("NEWS_DISABLE_SLOW_FALLBACK", "true")
    coverage = LiveNewsCoverage(logging.getLogger("news-test"), "2026-09-25", perform_verification=True)
    coverage.marketaux.token = "configured"
    candidate = _target_candidate("DASH")
    candidate.title = "DoorDash announces a new contract"
    candidate.affected_symbols = []
    coverage.marketaux.search = lambda *args, **kwargs: (setattr(coverage.marketaux, "requests_attempted", coverage.marketaux.requests_attempted + 1) or [candidate])
    coverage.official.collect = lambda symbols=None: []
    coverage.verification_acquirer.acquire = lambda items: {items[0]["title"]: ([], None)}
    _, budget = coverage.collect(mover_symbols=["DASH"], mover_details=[{"symbol": "DASH", "company_name": "DoorDash Inc.", "change_pct": 5}])
    assert budget["targeted_diagnostics"][0]["after_relevance_filter"] == 1


def test_targeted_event_story_ranks_above_valuation_analysis():
    weak = _target_candidate("DASH")
    weak.title = "A Look at DoorDash After Decline - GF Value"
    strong = _target_candidate("DASH")
    strong.title = "DoorDash cuts quarterly revenue guidance"
    ranked = _rank_targeted_candidates([weak, strong], "DASH", ["DoorDash Inc."])
    assert ranked[0]["candidate"] is strong
    assert ranked[0]["event_type"] == "GUIDANCE_CHANGE"
    assert ranked[1]["analysis_penalty"] > 0


def test_targeted_broad_industry_story_is_penalized():
    broad = _target_candidate("META")
    broad.title = "Global AI industry needs $6 trillion to justify data centres: Bain"
    specific = _target_candidate("META")
    specific.title = "Meta announces new data center investment"
    ranked = _rank_targeted_candidates([broad, specific], "META", ["Meta Platforms Inc."])
    broad_rank = next(item for item in ranked if item["candidate"] is broad)
    specific_rank = next(item for item in ranked if item["candidate"] is specific)
    assert broad_rank["subject_mismatch_penalty"] > 0
    assert specific_rank["candidate_selection_score"] > broad_rank["candidate_selection_score"]


def test_targeted_subject_gate_rejects_entity_tagged_mongodb_and_bain_stories(monkeypatch):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "false")
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    monkeypatch.setenv("NEWS_DISABLE_SLOW_FALLBACK", "true")
    coverage = LiveNewsCoverage(logging.getLogger("news-test"), "2026-09-25", perform_verification=True)
    coverage.marketaux.token = "configured"
    mongo = _target_candidate("META")
    mongo.title = "MongoDB CEO exit raises questions"
    bain = _target_candidate("META")
    bain.title = "Global AI industry needs $6 trillion: Bain"
    mongo.affected_symbols = ["META"]
    bain.affected_symbols = ["META"]
    coverage.marketaux.search = lambda query, query_type, symbols=None: [mongo, bain] if query_type == "TARGETED_MOVER" else []
    coverage.official.collect = lambda symbols=None: []
    called = []
    coverage.verification_acquirer.acquire = lambda items: called.append(items) or {}
    _, budget = coverage.collect(mover_symbols=["META"], mover_details=[{"symbol": "META", "company_name": "Meta Platforms Inc.", "change_pct": 5}])
    assert not called
    assert budget["targeted_subject_gate_rejected"] == 2
    assert budget["targeted_candidates_selected"] == 0
    assert all(item["subject_gate"] == "FAIL" for item in budget["targeted_diagnostics"][0]["subject_gate_results"])


def test_targeted_subject_gate_accepts_genuine_company_headline():
    candidate = _target_candidate("META")
    candidate.title = "Meta announces new data center investment"
    from news.live import _targeted_subject_relevance
    score, reason = _targeted_subject_relevance(candidate, "META", ["Meta Platforms Inc."])
    assert score >= 30
    assert reason == "COMPANY_IN_HEADLINE"


def test_operational_update_outranks_generic_other_when_subject_matches():
    factual = _target_candidate("BA")
    factual.title = "Boeing reports 737 software issue"
    commentary = _target_candidate("BA")
    commentary.title = "Boeing space comeback could be a big story"
    ranked = _rank_targeted_candidates([commentary, factual], "BA", ["Boeing Company"])
    assert ranked[0]["candidate"] is factual
    assert ranked[0]["event_type"] == "OPERATIONAL_UPDATE"


def test_targeted_second_candidate_is_attempted_after_rank_one_not_confirmed(monkeypatch):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "false")
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    monkeypatch.setenv("NEWS_DISABLE_SLOW_FALLBACK", "true")
    coverage = LiveNewsCoverage(logging.getLogger("news-test"), "2026-09-25", perform_verification=True)
    coverage.marketaux.token = "configured"
    weak = _target_candidate("BA")
    weak.title = "Boeing announces executive change"
    strong = _target_candidate("BA")
    strong.title = "Boeing wins major aircraft order"
    coverage.marketaux.search = lambda query, query_type, symbols=None: ((setattr(coverage.marketaux, "requests_attempted", coverage.marketaux.requests_attempted + 1) or [weak, strong]) if query_type == "TARGETED_MOVER" else [])
    coverage.official.collect = lambda symbols=None: []
    calls = []
    def acquire(items):
        calls.append(items[0]["title"])
        coverage.verification_acquirer.request_count += 1
        diagnostics = type("D", (), {"attempted_sources": [{"type": "TIER2_SEARCH", "status": "CHECKED"}]})()
        if items[0]["title"] == strong.title:
            return {items[0]["title"]: ([], diagnostics)}
        evidence = [{"source_type": "OFFICIAL", "source_domain": "boeing.com", "source_name": "Boeing IR", "source_url": "https://boeing.com/order", "title": items[0]["title"], "published_at": items[0]["published_at"], "affected_symbols": ["BA"]}]
        return {items[0]["title"]: (evidence, diagnostics)}
    coverage.verification_acquirer.acquire = acquire
    stories, budget = coverage.collect(mover_symbols=["BA"], mover_details=[{"symbol": "BA", "company_name": "Boeing Company", "change_pct": 8}])
    assert calls == [strong.title, weak.title]
    assert budget["targeted_second_candidates_attempted"] == 1
    assert budget["targeted_verification_requests"] == 2
    assert budget["targeted_verified_from_rank2"] == 1
    assert any(item.get("verification") == "VERIFIED" for item in stories)


def test_targeted_empty_results_have_explicit_diagnostics(monkeypatch):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "false")
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    monkeypatch.setenv("NEWS_DISABLE_SLOW_FALLBACK", "true")
    coverage = LiveNewsCoverage(logging.getLogger("news-test"), "2026-09-25", perform_verification=True)
    coverage.marketaux.token = "configured"
    coverage.marketaux.search = lambda *args, **kwargs: []
    coverage.official.collect = lambda symbols=None: []
    _, budget = coverage.collect(mover_symbols=["DASH"], mover_details=[{"symbol": "DASH", "company_name": "DoorDash Inc.", "change_pct": 5}])
    assert budget["targeted_verification_requests"] == 0
    assert budget["targeted_diagnostics"][0]["verification_candidates"] == 0
    assert budget["targeted_diagnostics"][0]["rejection_reasons"] == {}


def test_empty_marketaux_can_activate_capped_slow_fallback(monkeypatch):
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    monkeypatch.delenv("NEWS_DISABLE_SLOW_FALLBACK", raising=False)
    coverage = LiveNewsCoverage(logging.getLogger("news-test"), "2026-09-25")
    coverage.official.collect = lambda symbols=None: []
    coverage.marketaux.token = "configured"
    coverage.marketaux.search = lambda *args, **kwargs: []
    calls = []
    coverage.slow_discovery.search = lambda *args, **kwargs: calls.append(args[0]) or ([], {}, [])
    _, budget = coverage.collect(mover_symbols=["A", "B", "C", "D"])
    assert calls == ["A", "B", "C"]
    assert budget["slow_fallback_movers_attempted"] == 3
    assert budget["slow_fallback_provider_requests"] == 0


def test_incidental_mover_story_is_filtered_but_corporate_story_is_preserved():
    junk = _candidate()
    junk.title = "CVS shoplifting incident at local store"
    junk.affected_symbols = ["CVS"]
    corporate = _candidate()
    corporate.title = "CVS faces corporate litigation"
    corporate.affected_symbols = ["CVS"]
    assert not _is_relevant_mover_candidate(junk, ["CVS"])
    assert _is_relevant_mover_candidate(corporate, ["CVS"])


def test_early_stop_with_verified_official_story(monkeypatch):
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    coverage = LiveNewsCoverage(logging.getLogger("news-test"), "2026-09-25")
    official = _candidate(provider="official")
    official.official = True
    official.verification = "VERIFIED"
    official.editorial_eligible = True
    coverage.official.collect = lambda symbols=None: [official]
    coverage.marketaux.token = "configured"
    coverage.marketaux.search = lambda *args, **kwargs: pytest.fail("Marketaux should be skipped after early stop")
    _, budget = coverage.collect(mover_symbols=["NVDA"])
    assert budget["early_stop"] is True


def test_provider_circuit_opens_after_two_network_failures():
    clock = iter([0.0, 0.0, 0.0, 0.0])
    performance = NewsPerformance(logging.getLogger("news-test"), clock=lambda: next(clock))
    request = ProviderRequest(performance, "gdelt", lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectionError("offline")))
    with pytest.raises(requests.ConnectionError):
        request("url")
    with pytest.raises(requests.ConnectionError):
        request("url")
    with pytest.raises(ProviderCircuitOpen):
        request("url")
    assert performance.circuit_breakers_opened == 1


def test_budget_stops_new_requests_without_blocking():
    performance = NewsPerformance(logging.getLogger("news-test"), budget_seconds=0, clock=lambda: 1.0)
    request = ProviderRequest(performance, "marketaux", lambda *args, **kwargs: pytest.fail("request should not run"))
    with pytest.raises(NewsBudgetExhausted):
        request("url")


def test_provider_request_clamps_timeout_to_external_deadline():
    clock = [100.0]
    performance = NewsPerformance(logging.getLogger("news-test"), clock=lambda: clock[0])
    captured = {}
    request = ProviderRequest(performance, "marketaux", lambda *args, **kwargs: captured.update(kwargs) or object())
    request.set_deadline(104.0)
    request("url", timeout=(5, 15))
    assert captured["timeout"] == (4.0, 4.0)


def test_debug_subject_gate_allows_rejected_zero_score_candidate():
    budget = {"targeted_diagnostics": [{"candidate_ranking": [{"subject_relevance_score": 0, "selected_for_verification": False}]}]}
    passed, violations = validate_subject_gate(budget)
    assert passed and not violations


def test_debug_subject_gate_rejects_selected_zero_score_candidate():
    budget = {"targeted_diagnostics": [{"candidate_ranking": [{"subject_relevance_score": 0, "selected_for_verification": True}]}]}
    passed, violations = validate_subject_gate(budget)
    assert not passed and violations


def test_debug_subject_gate_accepts_selected_threshold_candidate():
    budget = {"targeted_diagnostics": [{"candidate_ranking": [{"subject_relevance_score": 30, "selected_for_verification": True}]}]}
    passed, violations = validate_subject_gate(budget)
    assert passed and not violations


def test_gdelt_timeout_is_bounded_and_classified():
    def hanging(*args, **kwargs):
        raise requests.ReadTimeout("simulated hang")

    client = GdeltClient(logging.getLogger("news-test"), datetime.now(timezone.utc), datetime.now(timezone.utc), request_get=hanging)
    result = client.search("market", 3)
    assert result["status"] == "failed"
    assert result["error_code"] == "GDELT_TIMEOUT"
