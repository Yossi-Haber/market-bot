"""Offline tests for LIVE NEWS V3 source boundaries and budgets."""

import logging
import re
from datetime import datetime, timezone

import pytest

from news.live import LiveNewsCoverage, verify_candidates
from news.discovery import DiscoveryCandidate
from news.marketaux import MarketauxDiscovery, parse_raw_articles
from news.official import parse_sec_filings, _rss_candidates
from news.editorial import build_public_stories
from news.schema import NewsCandidate

REFERENCE = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("source", ["marketaux", "gdelt", "google_news", "official"])
def test_live_collection_uses_canonical_discovery_source(source, monkeypatch):
    """Discovery candidates must survive the live filtering path without schema errors."""
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "false")
    monkeypatch.setenv("ENABLE_MARKETAUX", "false")
    monkeypatch.delenv("NEWS_DISABLE_SLOW_FALLBACK", raising=False)
    coverage = LiveNewsCoverage(logging.getLogger("test"), "2026-09-25", force_live=True)
    candidate = DiscoveryCandidate(
        title=f"ABC {source} acquisition announcement",
        url=f"https://example.com/{source}",
        source_domain="example.com",
        published_at="2026-09-25T12:00:00Z",
        language="en",
        source_country="US",
        discovery_source=source,
        url_resolution="resolved",
        retrieval_status="METADATA_ONLY",
    )
    coverage.slow_discovery.search = lambda *args, **kwargs: ([candidate], {}, [])

    stories, budget = coverage.collect(mover_symbols=["ABC"])

    assert stories
    assert budget["verification_candidates"] == (1 if source != "official" else 0)
    assert budget["marketaux_unique_events"] == (1 if source == "marketaux" else 0)


def test_sec_material_event_verified_and_irrelevant_filing_filtered():
    payload = {"cik": "0000000001", "filings": {"recent": {"form": ["8-K", "4"], "filingDate": ["2026-09-25", "2026-09-25"], "primaryDocument": ["event.htm", "ownership.htm"], "accessionNumber": ["0001-26-000001", "0001-26-000002"], "items": [["2.02"], ["4"]]}}}
    stories = parse_sec_filings(payload, "NVDA", "2026-09-25")
    assert len(stories) == 1
    assert stories[0].verification == "VERIFIED"
    assert stories[0].category == "EARNINGS"


def test_official_fed_bls_bea_release_candidates_are_tier_one():
    xml = """<rss><channel><item><title>CPI release</title><pubDate>Fri, 25 Sep 2026 12:30:00 GMT</pubDate><link>https://www.bls.gov/news.release/cpi.htm</link></item></channel></rss>"""
    story = _rss_candidates(xml, "BLS", "bls.gov", "https://www.bls.gov/feed/bls_latest.rss", "MACRO_RELEASE", "2026-09-25")[0]
    assert story.official and story.source_tier == 1 and story.verification == "VERIFIED"


def test_bls_generic_latest_numbers_is_verified_but_not_editorial():
    xml = """<rss><channel><item><title>Major Economic Indicators Latest Numbers</title><pubDate>Fri, 25 Sep 2026 12:30:00 GMT</pubDate><link>https://www.bls.gov/eag/eag.us.htm</link></item></channel></rss>"""
    story = _rss_candidates(xml, "BLS", "bls.gov", "https://www.bls.gov/feed/bls_latest.rss", "MACRO_RELEASE", "2026-09-25")[0]
    assert story.verification == "VERIFIED"
    assert story.editorial_eligible is False
    assert story.content_type == "INDEX_PAGE"


def test_bea_blog_is_not_editorial_but_gdp_release_is():
    blog = _rss_candidates("""<rss><channel><item><title>AI measurement commentary</title><pubDate>Fri, 25 Sep 2026 12:30:00 GMT</pubDate><link>https://www.bea.gov/news/blog/ai-measurement</link></item></channel></rss>""", "BEA", "bea.gov", "https://www.bea.gov/news/blog/feed", "MACRO_RELEASE", "2026-09-25")[0]
    release = _rss_candidates("""<rss><channel><item><title>GDP, 2nd Quarter 2026</title><pubDate>Fri, 25 Sep 2026 12:30:00 GMT</pubDate><link>https://www.bea.gov/news/2026/gdp-second-quarter</link></item></channel></rss>""", "BEA", "bea.gov", "https://www.bea.gov/news/blog/feed", "MACRO_RELEASE", "2026-09-25")[0]
    assert blog.verification == release.verification == "VERIFIED"
    assert blog.content_type == "BLOG" and not blog.editorial_eligible
    assert release.content_type == "RELEASE" and release.editorial_eligible


def test_undated_and_old_official_items_are_not_editorial():
    undated = _rss_candidates("""<rss><channel><item><title>CPI release</title><link>https://www.bls.gov/news.release/cpi.htm</link></item></channel></rss>""", "BLS", "bls.gov", "https://www.bls.gov/feed/bls_latest.rss", "MACRO_RELEASE", "2026-09-25")[0]
    old = _rss_candidates("""<rss><channel><item><title>CPI release</title><pubDate>Fri, 01 Aug 2026 12:30:00 GMT</pubDate><link>https://www.bls.gov/news.release/cpi.htm</link></item></channel></rss>""", "BLS", "bls.gov", "https://www.bls.gov/feed/bls_latest.rss", "MACRO_RELEASE", "2026-09-25")[0]
    assert not undated.editorial_eligible
    assert not old.editorial_eligible


def test_top_story_selector_requires_editorial_eligibility():
    candidates = {"BLS": {"catalyst_status": "CONFIRMED", "evidence_quality": "STRONG", "headline": "Major Economic Indicators Latest Numbers", "catalyst_type": "MACRO_RELEASE", "editorial_eligible": False, "event_datetime": "2026-09-25T12:30:00+00:00", "primary_source": {"source_name": "BLS", "source_tier": 1, "published_at": "2026-09-25T12:30:00+00:00"}}}
    assert build_public_stories(candidates, "2026-09-25", REFERENCE) == []


def test_marketaux_only_remains_unconfirmed_and_matching_official_verifies():
    discovery = NewsCandidate("Fed announces rate decision", None, "2026-09-25T12:30:00Z", "Marketaux", "example.com", 3, "https://example.com/story", "CENTRAL_BANK", ["SPY"], "marketaux", False)
    assert verify_candidates([discovery])[0]["verification"] == "NOT_CONFIRMED"
    official = NewsCandidate("Fed announces rate decision", None, "2026-09-25T12:30:00Z", "Federal Reserve", "federalreserve.gov", 1, "https://federalreserve.gov/story", "CENTRAL_BANK", ["SPY"], "official", True, "VERIFIED")
    merged = verify_candidates([discovery, official])
    assert len(merged) == 1
    assert merged[0]["verification"] == "VERIFIED"


def test_same_day_outranks_stale_and_duplicates_cluster():
    old = NewsCandidate("Company announces acquisition", None, "2026-09-24T12:00:00Z", "Reuters", "reuters.com", 2, "https://reuters.com/old", "M&A", ["ABC"], "discovery", False)
    fresh = NewsCandidate("Company announces acquisition", None, "2026-09-25T12:00:00Z", "SEC", "sec.gov", 1, "https://sec.gov/new", "M&A", ["ABC"], "official", True, "VERIFIED")
    result = verify_candidates([old, fresh])
    assert len(result) == 1 and result[0]["verification"] == "VERIFIED"


def test_marketaux_missing_token_is_disabled_without_request():
    client = MarketauxDiscovery(logging.getLogger("test"), "2026-09-25", token="")
    assert not client.enabled
    assert client.search("market") == []
    assert client.request_count == 0


def test_marketaux_report_date_and_article_quality_filters(tmp_path):
    payload = {"data": [
        {"title": "NVDA announces new partnership", "published_at": "2026-09-25T16:00:00Z", "source": "Reuters", "url": "https://reuters.com/nvda", "entities": [{"symbol": "NVDA"}]},
        {"title": "Week Ahead: gold may rise to new target", "published_at": "2026-09-25T17:00:00Z", "source": "Reuters", "url": "https://reuters.com/forecast", "entities": [{"symbol": "GOLD"}]},
        {"title": "MSFT market commentary", "published_at": "2026-09-26T12:00:00Z", "source": "Reuters", "url": "https://reuters.com/future", "entities": [{"symbol": "MSFT"}]},
        {"title": "AAPL announces product launch", "published_at": "2026-09-25T15:00:00Z", "source": "Unknown Aggregator", "url": "https://stockmarketwatch.com/aapl", "entities": [{"symbol": "AAPL"}]},
    ]}
    class Response:
        def raise_for_status(self):
            return None
        def json(self):
            return payload
    client = MarketauxDiscovery(logging.getLogger("test"), "2026-09-25", token="token", request_get=lambda *args, **kwargs: Response())
    stories = client.search("NVDA")
    assert [story.affected_symbols for story in stories] == [["NVDA"]]
    assert client.diagnostics["marketaux_candidates_total"] == 4
    assert client.diagnostics["marketaux_filtered_future"] == 1
    assert client.diagnostics["marketaux_filtered_forecast"] == 1
    assert client.diagnostics["marketaux_filtered_low_quality"] == 1


def test_marketaux_historical_report_is_anchored_to_report_date():
    payload = {"data": [{"title": "S&P 500 session recap", "published_at": "2026-09-25T20:00:00Z", "source": "Reuters", "url": "https://reuters.com/recap", "entities": []}]}
    class Response:
        def raise_for_status(self):
            return None
        def json(self):
            return payload
    client = MarketauxDiscovery(logging.getLogger("test"), "2026-09-25", token="token", request_get=lambda *args, **kwargs: Response())
    assert client.search("market")


def test_query_plan_has_six_factual_groups_and_uses_movers():
    plan = LiveNewsCoverage.default_queries(["NVDA", "MSFT", "AAPL"])
    assert len(plan) == 6
    mover_query = next(item for item in plan if item["query_type"] == "TOP_MOVERS")
    assert mover_query["symbols"] == ["NVDA", "MSFT", "AAPL"]
    assert not any(term in " ".join(item.get("query", "") for item in plan).lower() for term in ("forecast", "price target", "week ahead", "should you buy"))


def test_production_query_groups_use_or_syntax_and_symbols():
    plan = {item["query_type"]: item for item in LiveNewsCoverage.default_queries(["MCHP", "CVS", "PYPL", "PANW", "META"])}
    assert "|" in plan["US_MARKET_SESSION"]["query"]
    assert "|" in plan["RATES_FED"]["query"]
    assert plan["AI_SEMICONDUCTORS"]["symbols"] == ["NVDA", "AMD", "AVGO"]
    assert plan["TOP_MOVERS"]["symbols"] == ["MCHP", "CVS", "PYPL", "PANW", "META"]
    assert "|" in plan["CORPORATE_EVENTS"]["query"]
    assert "|" in plan["MACRO_COMMODITIES"]["query"]
    assert "Nvidia semiconductors AI stocks chips announces launched partnership" not in str(plan)


def test_marketaux_pushes_report_date_window_into_request(tmp_path, monkeypatch):
    monkeypatch.setattr("news.marketaux.CACHE_DIR", tmp_path)
    captured = {}
    class Response:
        def raise_for_status(self):
            return None
        def json(self):
            return {"data": []}
    def request(*args, **kwargs):
        captured.update(kwargs.get("params", {}))
        return Response()
    client = MarketauxDiscovery(logging.getLogger("test"), "2026-09-25", token="token", request_get=request)
    client.search("S&P 500", "US_MARKET_SESSION")
    assert captured["published_after"].startswith("2026-09-25")
    assert "2026-09-26" in captured["published_before"]


def test_marketaux_cache_key_changes_with_query_type_and_refresh_bypasses_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("news.marketaux.CACHE_DIR", tmp_path)
    calls = []
    class Response:
        def raise_for_status(self):
            return None
        def json(self):
            return {"data": []}
    def request(*args, **kwargs):
        calls.append(kwargs)
        return Response()
    client = MarketauxDiscovery(logging.getLogger("test"), "2026-09-25", token="token", request_get=request)
    client.search("same query", "A")
    client.search("same query", "B")
    assert len(calls) == 2
    cached = MarketauxDiscovery(logging.getLogger("test"), "2026-09-25", token="token", request_get=request)
    cached.search("same query", "A")
    assert len(calls) == 2
    refreshed = MarketauxDiscovery(logging.getLogger("test"), "2026-09-25", token="token", request_get=request, refresh_cache=True)
    refreshed.search("same query", "A")
    assert len(calls) == 3


def test_marketaux_targeted_subbudget_remains_after_general_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("news.marketaux.CACHE_DIR", tmp_path)
    calls = []
    class Response:
        def raise_for_status(self):
            return None
        def json(self):
            return {"data": []}
    def request(*args, **kwargs):
        calls.append(kwargs.get("params", {}).copy())
        return Response()
    client = MarketauxDiscovery(logging.getLogger("test"), "2026-09-25", token="token", request_get=request)
    for index in range(client.max_requests):
        client.search(f"general-{index}", f"GENERAL_{index}")
    assert client.request_count == client.max_requests
    client.search('"A" "Company A"', "TARGETED_MOVER", ["A"])
    assert client.targeted_requests_attempted == 1
    assert len(calls) == client.max_requests + 1
    targeted_params = calls[-1]
    assert targeted_params["symbols"] == "A"
    assert targeted_params["filter_entities"] == "true"
    assert targeted_params["must_have_entities"] == "true"
    assert "search" not in targeted_params
    assert " OR " not in str(targeted_params)


def test_general_marketaux_uses_short_timeout_but_targeted_keeps_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr("news.marketaux.CACHE_DIR", tmp_path)
    captured = []
    class Response:
        status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            return {"data": []}
    def request(*args, **kwargs):
        captured.append(kwargs["timeout"])
        return Response()
    client = MarketauxDiscovery(logging.getLogger("test"), "2026-09-25", token="token", request_get=request)
    client.search("market", "GENERAL")
    client.search("", "TARGETED_MOVER", ["DASH"])
    assert captured == [(5, 6), (5, 15)]


def test_targeted_marketaux_keeps_company_event_outside_general_category_filter(tmp_path, monkeypatch):
    monkeypatch.setattr("news.marketaux.CACHE_DIR", tmp_path)
    class Response:
        def raise_for_status(self):
            return None
        def json(self):
            return {"data": [{
                "title": "DoorDash analyst discusses quarterly outlook",
                "description": "DoorDash company event discussion",
                "published_at": "2026-09-25T15:00:00Z",
                "source": "Reuters",
                "url": "https://reuters.com/doordash",
                "entities": [{"symbol": "DASH", "name": "DoorDash"}],
            }]}
    client = MarketauxDiscovery(logging.getLogger("test"), "2026-09-25", token="token", request_get=lambda *args, **kwargs: Response())
    stories = client.search('"DASH" OR "DoorDash" OR analyst', "TARGETED_MOVER", ["DASH"])
    assert len(stories) == 1
    assert stories[0].affected_symbols == ["DASH"]


def test_targeted_marketaux_rejects_article_without_target_entity(tmp_path, monkeypatch):
    monkeypatch.setattr("news.marketaux.CACHE_DIR", tmp_path)
    class Response:
        status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            return {"meta": {"found": 1, "returned": 1}, "data": [{
                "title": "DoorDash announces a contract",
                "published_at": "2026-09-25T15:00:00Z",
                "source": "Reuters",
                "url": "https://reuters.com/doordash-contract",
                "entities": [{"symbol": "UBER", "name": "Uber"}],
            }]}
    client = MarketauxDiscovery(logging.getLogger("test"), "2026-09-25", token="token", request_get=lambda *args, **kwargs: Response())
    assert client.search("", "TARGETED_MOVER", ["DASH"]) == []
    diagnostic = client.query_diagnostics[-1]
    assert diagnostic["entity_missing"] == 1
    assert diagnostic["raw_candidates"] == 1


def test_marketaux_json_error_is_not_reported_as_zero_results(tmp_path, monkeypatch):
    monkeypatch.setattr("news.marketaux.CACHE_DIR", tmp_path)
    class Response:
        status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            return {"error": {"code": "invalid_parameter", "message": "invalid symbols"}}
    client = MarketauxDiscovery(logging.getLogger("test"), "2026-09-25", token="token", request_get=lambda *args, **kwargs: Response())
    assert client.search("", "TARGETED_MOVER", ["DASH"]) == []
    diagnostic = client.query_diagnostics[-1]
    assert diagnostic["error_code"] == "invalid_parameter"
    assert diagnostic["raw_candidates"] == 0
    assert diagnostic["status"] == 200


def test_marketaux_request_failure_has_sanitized_diagnostics_and_counters():
    captured = {}
    class Response:
        status_code = 400
        def raise_for_status(self):
            error = __import__("requests").HTTPError("bad request")
            error.response = self
            raise error
        def json(self):
            return {"error": {"code": "malformed_parameters", "message": "The published_before parameter(s) are incorrectly formatted."}}
    def request(*args, **kwargs):
        captured.update(kwargs.get("params", {}))
        return Response()
    client = MarketauxDiscovery(logging.getLogger("test"), "2026-09-25", token="secret-token", request_get=request)
    assert client.search("Fed", "FED_RATES") == []
    assert client.requests_attempted == 1 and client.requests_succeeded == 0 and client.requests_failed == 1
    diagnostic = client.query_diagnostics[0]
    assert diagnostic["error_code"] == "malformed_parameters"
    assert "secret-token" not in str(diagnostic)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", captured["published_after"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", captured["published_before"])
    assert "Z" not in captured["published_before"] and "+00:00" not in captured["published_before"]


def test_marketaux_usage_limit_disables_client_after_http_402(tmp_path, monkeypatch):
    """A usage-limit response hard-stops only this client/run and skips later HTTP calls."""
    monkeypatch.setattr("news.marketaux.CACHE_DIR", tmp_path)
    calls = []

    class Response:
        status_code = 402

        def raise_for_status(self):
            error = __import__("requests").HTTPError("payment required")
            error.response = self
            raise error

        def json(self):
            return {"error": {"code": "usage_limit_reached", "message": "usage limit reached"}}

    def request(*args, **kwargs):
        calls.append(kwargs)
        return Response()

    client = MarketauxDiscovery(logging.getLogger("test"), "2026-09-25", token="secret", request_get=request, refresh_cache=True)
    assert client.search("first", "GENERAL") == []
    assert client.usage_limit_reached is True
    assert client.available_for_run is False
    assert client.requests_attempted == 1
    assert client.search("second", "GENERAL") == []
    assert len(calls) == 1
    assert client.requests_skipped_usage_limit == 1
    assert client.query_diagnostics[-1]["error_code"] == "MARKETAUX_USAGE_LIMIT_REACHED"


def test_marketaux_usage_limit_skips_targeted_escalation_and_preserves_fallback(monkeypatch):
    """General 402 disables targeted calls while the existing slow fallback remains available."""
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "false")
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    monkeypatch.setenv("MARKETAUX_API_TOKEN", "configured")
    monkeypatch.delenv("NEWS_DISABLE_SLOW_FALLBACK", raising=False)
    calls = []

    class Response:
        status_code = 402

        def raise_for_status(self):
            error = __import__("requests").HTTPError("payment required")
            error.response = self
            raise error

        def json(self):
            return {"error": {"code": "usage_limit_reached", "message": "usage limit reached"}}

    def request(*args, **kwargs):
        calls.append(kwargs)
        return Response()

    coverage = LiveNewsCoverage(logging.getLogger("test"), "2026-09-25", request_get=request, perform_verification=True, force_live=True)
    coverage.official.collect = lambda symbols=None: []
    slow_calls = []
    coverage.slow_discovery.search = lambda *args, **kwargs: (slow_calls.append(args) or ([], {}, []))
    _, budget = coverage.collect(
        mover_symbols=["DASH", "QCOM", "BA", "META"],
        mover_details=[{"symbol": symbol, "change_pct": 5 - index} for index, symbol in enumerate(("DASH", "QCOM", "BA", "META"))],
    )
    assert len(calls) == 1
    assert budget["marketaux_usage_limit_reached"] is True
    assert budget["marketaux_requests_attempted"] == 1
    assert budget["marketaux_requests_skipped_usage_limit"] >= 5
    assert budget["targeted_marketaux_requests"] == 0
    assert budget["targeted_marketaux_skipped_usage_limit"] == 4
    assert budget["coverage_escalation_skipped_reason"] == "MARKETAUX_USAGE_LIMIT_REACHED"
    assert slow_calls


def test_marketaux_usage_limit_does_not_block_existing_cache(tmp_path, monkeypatch):
    """The hard stop applies to live requests, not valid normal-mode cache reads."""
    monkeypatch.setattr("news.marketaux.CACHE_DIR", tmp_path)
    client = MarketauxDiscovery(logging.getLogger("test"), "2026-09-25", token="secret", request_get=lambda *a, **k: pytest.fail("live request must not run"))
    query = "cached query"
    import hashlib
    digest = hashlib.sha256(f"{query}|".encode("utf-8")).hexdigest()[:16]
    cache_path = tmp_path / "2026-09-25" / f"general_{digest}.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text('{"data": []}', encoding="utf-8")
    client._mark_usage_limit_reached()
    assert client.search(query, "GENERAL") == []
    assert client.cache_hits == 1
    assert client.requests_skipped_usage_limit == 0


def test_marketaux_usage_limit_is_scoped_to_one_program_run(tmp_path, monkeypatch):
    """A new provider instance starts enabled again for the next independent run."""
    monkeypatch.setattr("news.marketaux.CACHE_DIR", tmp_path)
    first = MarketauxDiscovery(logging.getLogger("test"), "2026-09-25", token="secret")
    first._mark_usage_limit_reached()
    second = MarketauxDiscovery(logging.getLogger("test"), "2026-09-25", token="secret")
    assert first.available_for_run is False
    assert second.available_for_run is True
    assert second.usage_limit_reached is False


def test_marketaux_unique_events_is_zero_when_only_official_candidates_exist(monkeypatch):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "true")
    monkeypatch.setenv("ENABLE_MARKETAUX", "false")
    coverage = LiveNewsCoverage(logging.getLogger("test"), "2026-09-25")
    coverage.official.collect = lambda symbols=None: [NewsCandidate("Official event", None, "2026-09-25T12:00:00Z", "SEC", "sec.gov", 1, "https://sec.gov", "COMPANY", ["ABC"], "official", True, "VERIFIED", "FILING", True)]
    stories, budget = coverage.collect()
    assert stories and budget["marketaux_unique_events"] == 0


def test_marketaux_unique_events_counts_deduplicated_discovery_candidates(monkeypatch):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "false")
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    coverage = LiveNewsCoverage(logging.getLogger("test"), "2026-09-25")
    coverage.marketaux.token = "configured"
    candidates = [NewsCandidate(f"Company announces event {index}", None, "2026-09-25T12:00:00Z", "Reuters", "reuters.com", 2, f"https://reuters.com/{index}", "COMPANY", [f"SYM{index}"], "marketaux", False, "NOT_CONFIRMED", "OTHER", False, "NEWS_EVENT", 5.0) for index in range(3)]
    coverage.marketaux.search = lambda *args, **kwargs: candidates
    stories, budget = coverage.collect()
    assert len(stories) == 3
    assert budget["marketaux_unique_events"] == 3


def test_refresh_marketaux_does_not_repeat_official_collection(monkeypatch):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "true")
    monkeypatch.setenv("ENABLE_MARKETAUX", "false")
    coverage = LiveNewsCoverage(logging.getLogger("test"), "2026-09-25", refresh_marketaux=True)
    calls = []
    coverage.official.collect = lambda symbols=None: calls.append(symbols) or []
    coverage.collect(mover_symbols=["NVDA"])
    assert calls == [None]


def test_raw_marketaux_fixture_meta_and_parsing_are_preserved():
    payload = {"meta": {"found": 3, "returned": 3, "limit": 3}, "data": [{"title": "Article 1"}, {"title": "Article 2"}, {"title": "Article 3"}]}
    assert len(parse_raw_articles(payload)) == 3
    assert payload["meta"]["found"] == 3 and payload["meta"]["returned"] == 3


def test_diagnostic_probe_uses_at_most_five_safe_requests(tmp_path, monkeypatch):
    monkeypatch.setattr("news.marketaux.CACHE_DIR", tmp_path)
    calls = []
    class Response:
        def raise_for_status(self):
            return None
        def json(self):
            return {"meta": {"found": 3, "returned": 3, "limit": 3}, "data": [{"title": "One"}, {"title": "Two"}, {"title": "Three"}]}
    def request(*args, **kwargs):
        calls.append(kwargs["params"])
        return Response()
    client = MarketauxDiscovery(logging.getLogger("test"), "2026-09-25", token="secret-token", request_get=request)
    probes = client.diagnose({"search": "Nvidia"})
    assert len(probes) == 5 and len(calls) == 5
    assert all("api_token" not in probe["params"] for probe in probes)
    assert all(probe["found"] == 3 and probe["returned"] == 3 and probe["parsed_count"] == 3 for probe in probes)
    assert all("secret-token" not in str(probe) for probe in probes)


def test_diagnostic_zero_meta_is_distinct_from_parser_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("news.marketaux.CACHE_DIR", tmp_path)
    class Response:
        def raise_for_status(self):
            return None
        def json(self):
            return {"meta": {"found": 0, "returned": 0, "limit": 3}, "data": []}
    client = MarketauxDiscovery(logging.getLogger("test"), "2026-09-25", token="token", request_get=lambda *args, **kwargs: Response())
    result = client.diagnose({"search": "Nvidia"})[0]
    assert result["found"] == result["returned"] == result["data_length"] == result["parsed_count"] == 0


def test_provider_failure_does_not_fail_coverage(monkeypatch, tmp_path):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "true")
    monkeypatch.setenv("ENABLE_MARKETAUX", "false")
    monkeypatch.setattr("news.official.CACHE_DIR", tmp_path)
    def fail(*args, **kwargs):
        raise OSError("offline")
    stories, budget = LiveNewsCoverage(logging.getLogger("test"), "2026-09-25", request_get=fail).collect()
    assert stories == []
    assert budget["news_requests_total"] == budget["official_requests"]
