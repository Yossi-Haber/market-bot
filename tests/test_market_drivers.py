"""Offline tests for market-driver hypotheses and provider fallback."""

import logging
import sys
from pathlib import Path

from drivers import build_driver_analysis, detect_driver_hypotheses
from news.providers.finnhub import FinnhubProvider
from news.providers.finnhub import cluster_finnhub_candidates, extract_event_signature, finnhub_subject_relevance, finnhub_subject_role, rank_finnhub_candidates
from news.providers.strategy import CompanyNewsStrategy
from news.schema import NewsCandidate


def _report():
    return {
        "market": {"core": {symbol: {"change_pct": -1.0} for symbol in ("SPY", "QQQ", "DIA", "IWM")}},
        "macro": {"US10Y": {"change_bps": 8}, "VIX": {"change_pct": 12}, "WTI": {"change_pct": 0}},
        "sectors": {"all": [{"symbol": "SMH", "sector_name": "SEMICONDUCTORS", "change_pct": 3.0}, {"symbol": "XLF", "sector_name": "FINANCIALS", "change_pct": -2.0}]},
        "movers": {"top_gainers": [{"symbol": "AMD", "change_pct": 6}], "top_losers": []},
    }


def test_rates_up_market_down_creates_rates_hypothesis():
    ids = {item.driver_id for item in detect_driver_hypotheses(_report())}
    assert "RATES_PRESSURE" in ids


def test_semiconductor_outperformance_and_representatives():
    sector = next(item for item in detect_driver_hypotheses(_report()) if item.driver_id == "SECTOR_SEMICONDUCTORS")
    assert sector.related_symbols[:3] == ["NVDA", "AMD", "AVGO"]


def test_mover_becomes_stock_catalyst_target():
    stock = next(item for item in detect_driver_hypotheses(_report()) if item.driver_id == "STOCK_AMD")
    assert stock.related_symbols == ["AMD"]


def test_unverified_story_cannot_confirm_driver_but_verified_story_can():
    report = _report()
    result = build_driver_analysis({**report, "candidate_stories": [{"title": "AMD event"}]})
    assert all(item["verification_status"] != "CONFIRMED" for item in result["stock_catalysts"])
    verified = {"verification": "VERIFIED", "editorial_eligible": True, "affected_symbols": ["AMD"], "title_he": "AMD reports results"}
    result = build_driver_analysis({**report, "verified_stories": [verified]})
    assert next(item for item in result["stock_catalysts"] if item["driver_id"] == "STOCK_AMD")["verification_status"] == "CONFIRMED"


def test_marketaaux_unavailable_falls_back_to_finnhub_and_normalizes():
    class Primary:
        available = False

        def search_company(self, *args):
            raise AssertionError("disabled primary must not be called")

    def request(*args, **kwargs):
        class Response:
            def raise_for_status(self): pass
            def json(self): return [{"id": "fh-1", "headline": "AMD reports results", "summary": "Results", "url": "https://finnhub.example/a", "source": "Finnhub", "datetime": 1790337600}]
        return Response()

    secondary = FinnhubProvider(token="configured", request_get=request)
    result = CompanyNewsStrategy(Primary(), secondary).search_company("AMD", "2026-09-25", "2026-09-25")
    assert result[0].discovery_source == "finnhub"
    assert result[0].provider_article_id == "fh-1"


def test_finnhub_company_headline_passes_subject_gate():
    item = NewsCandidate("Boeing shares rebound as UBS calls selloff overdone", "Boeing stock recovered", "2026-09-28T12:00:00+00:00", "Yahoo", "", 3, "https://example/ba", "ANALYST_ACTION", ["BA"], "finnhub")
    score, _ = finnhub_subject_relevance(item, "BA", "Boeing")
    assert score >= 30


def test_company_headline_matching_preserves_retained_strict_name_contract():
    canonical = NewsCandidate("Tesla Inc. shares rise after launch", None, None, "Yahoo", "", 3,
                              "https://example.test/story", "OTHER")
    role, score, _ = finnhub_subject_role(canonical, "TSLA", "Tesla Inc.")
    assert role == "PRIMARY_SUBJECT"
    assert score >= 30

    shortened_brand = NewsCandidate("Tesla shares rise after launch", None, None, "Yahoo", "", 3,
                                    "https://example.test/short-brand", "OTHER")
    assert finnhub_subject_role(shortened_brand, "TSLA", "Tesla Inc.")[1] == 0

    tagged_only = NewsCandidate("Markets rally as investors weigh rates", None, None, "Yahoo", "", 3,
                                "https://example.test/market", "OTHER", ["TSLA"], "finnhub")
    assert finnhub_subject_role(tagged_only, "TSLA", "Tesla Inc.")[1] == 0


def test_finnhub_broad_tagged_articles_fail_subject_gate():
    for title in ("Stock Market Today: Dow Jones stocks move", "The Great AI Dilemma"):
        item = NewsCandidate(title, "Broad market commentary", "2026-09-28T12:00:00+00:00", "Yahoo", "", 3, "https://example/story", "OTHER", ["BA"], "finnhub")
        score, _ = finnhub_subject_relevance(item, "BA", "Boeing")
        assert score == 0


def test_finnhub_symbol_metadata_alone_is_insufficient():
    item = NewsCandidate("Markets rally on rate hopes", "Broad indices gained", "2026-09-28T12:00:00+00:00", "Yahoo", "", 3, "https://example/story", "OTHER", ["BA"], "finnhub")
    assert finnhub_subject_relevance(item, "BA", "Boeing")[0] == 0


def test_finnhub_timestamp_is_canonical_utc_iso():
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return [{"id": "1", "headline": "Boeing reports results", "url": "https://example/1", "datetime": 1790337600}]
    item = FinnhubProvider(token="configured", request_get=lambda *a, **k: Response()).search_company("BA", "2026-09-28", "2026-09-29")[0]
    assert item.published_at.endswith("+00:00")


def test_finnhub_event_story_outranks_generic_market_story():
    candidates = [
        NewsCandidate("Stock Market Today: Dow Jones stocks move", None, None, "Yahoo", "", 3, "https://example/generic", "OTHER", ["BA"], "finnhub"),
        NewsCandidate("Boeing shares rebound as UBS calls selloff overdone", None, None, "Yahoo", "", 3, "https://example/event", "ANALYST_ACTION", ["BA"], "finnhub"),
    ]
    ranked = rank_finnhub_candidates(candidates, "BA", "Boeing")
    assert ranked[0]["headline"].startswith("Boeing")
    assert ranked[0]["subject_gate"] == "PASS"


def test_ftai_aircraft_purchase_is_secondary_boeing_mention():
    item = NewsCandidate("FTAI Acquires 27 Boeing 737-700s to Expand Aviation Businesses", None, None, "Yahoo", "", 3, "https://example/ftai", "M_AND_A", ["BA"], "finnhub")
    role, score, _ = finnhub_subject_role(item, "BA", "Boeing")
    assert role == "SECONDARY_MENTION"
    assert score < 30


def test_faa_max_certification_delay_is_direct_product_event():
    item = NewsCandidate("FAA Administrator Says Software Issue Will Delay Max 10 Certification", None, None, "Yahoo", "", 3, "https://example/faa", "REGULATORY", ["BA"], "finnhub")
    role, score, _ = finnhub_subject_role(item, "BA", "Boeing")
    assert role == "DIRECT_PRODUCT_EVENT"
    assert score >= 30


def test_multiple_max_articles_form_one_cluster_with_independent_sources():
    items = [
        NewsCandidate("FAA delays MAX 10 certification over software issue", None, "2026-09-28T10:00:00+00:00", "Reuters", "", 3, "https://reuters.example/a", "REGULATORY", ["BA"], "finnhub"),
        NewsCandidate("FAA Administrator says software issue will delay MAX 10 certification", None, "2026-09-28T11:00:00+00:00", "Yahoo", "", 3, "https://yahoo.example/b", "REGULATORY", ["BA"], "finnhub"),
        NewsCandidate("MAX 10 certification delayed by FAA software issue", None, "2026-09-28T12:00:00+00:00", "WSJ", "", 3, "https://wsj.example/c", "REGULATORY", ["BA"], "finnhub"),
    ]
    clusters = cluster_finnhub_candidates(items, "BA", "Boeing")
    assert len(clusters) == 1
    assert clusters[0]["article_count"] == 3
    assert clusters[0]["independent_source_count"] == 3


def test_secondary_cluster_does_not_pass_boeing_catalyst_gate():
    item = NewsCandidate("FTAI Acquires 27 Boeing 737-700s", None, None, "Yahoo", "", 3, "https://example/ftai", "M_AND_A", ["BA"], "finnhub")
    cluster = cluster_finnhub_candidates([item], "BA", "Boeing")[0]
    assert cluster["target_role"] == "SECONDARY_MENTION"
    assert cluster["subject_score"] < 30


def test_secondary_ftai_article_does_not_join_primary_max_cluster():
    primary = NewsCandidate("FAA delays MAX 10 certification over software issue", None, "2026-09-28T10:00:00+00:00", "Reuters", "", 3, "https://example/faa", "REGULATORY", ["BA"], "finnhub")
    secondary = NewsCandidate("FTAI Acquires 27 Boeing 737-700s", None, "2026-09-28T11:00:00+00:00", "Yahoo", "", 3, "https://example/ftai", "M_AND_A", ["BA"], "finnhub")
    clusters = cluster_finnhub_candidates([primary, secondary], "BA", "Boeing")
    assert len(clusters) == 2
    assert all(cluster["article_count"] == 1 for cluster in clusters)


def test_shared_737_product_without_action_overlap_does_not_merge():
    primary = NewsCandidate("Boeing 737 MAX software issue delays FAA certification", None, "2026-09-28T10:00:00+00:00", "Reuters", "", 3, "https://example/faa", "REGULATORY", ["BA"], "finnhub")
    order = NewsCandidate("Boeing 737 order expands airline fleet", None, "2026-09-28T11:00:00+00:00", "WSJ", "", 3, "https://example/order", "OTHER", ["BA"], "finnhub")
    assert len(cluster_finnhub_candidates([primary, order], "BA", "Boeing")) == 2


def test_short_actor_aliases_use_token_boundaries():
    unrelated = NewsCandidate("Large aerospace demand improves", None, None, "Yahoo", "", 3, "https://example/large", "OTHER", ["BA"], "finnhub")
    assert "GE" not in extract_event_signature(unrelated, "BA", "Boeing")["actors"]
    valid = NewsCandidate("GE Aerospace reviews engine deliveries", None, None, "Yahoo", "", 3, "https://example/ge", "OTHER", ["BA"], "finnhub")
    assert "GE" in extract_event_signature(valid, "BA", "Boeing")["actors"]


def test_ubs_analyst_story_does_not_gain_earnings_action():
    item = NewsCandidate("Boeing shares rebound as UBS calls selloff overdone", "Analyst commentary", None, "Yahoo", "", 3, "https://example/ubs", "ANALYST_ACTION", ["BA"], "finnhub")
    signature = extract_event_signature(item, "BA", "Boeing")
    assert signature["event_actions"] == ["ANALYST_UPGRADE"]


def test_duplicate_articles_are_removed_across_provider_results():
    item = NewsCandidate("Same story", None, "2026-09-25", "Reuters", "reuters.com", 2, "https://example/story", "OTHER", ["AMD"], "marketaux")
    strategy = CompanyNewsStrategy(object(), object())
    assert len(strategy._dedupe([item, item])) == 1


def test_llm_driver_payload_preserves_certainty_state():
    from llm.input_builder import build_llm_payload
    payload = build_llm_payload({"publication_readiness": {"status": "READY", "reasons": []}, "market_date": "2026-09-25", "main_market_driver": {"driver_id": "RISK_OFF", "verification_status": "PLAUSIBLE_NOT_VERIFIED", "scope": "US_MARKET"}})
    assert payload["driver_state"]["plausible_driver"]["driver_id"] == "RISK_OFF"
    assert payload["driver_state"]["verified_driver"] is None


def test_offline_driver_harness_reads_only_local_report(monkeypatch):
    import debug_market_drivers
    fixture = Path(__file__).parent / "fixtures" / "market_driver_report_20260928.json"
    monkeypatch.setattr(sys, "argv", ["debug_market_drivers.py", "--market-date", "2026-09-28", "--offline", "--report-path", str(fixture)])
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("offline harness used network")))
    assert debug_market_drivers.main() == 0
