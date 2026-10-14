"""Offline tests for FMP discovery and press-release evidence."""

import logging

from news.providers.fmp import FMPProvider
from news.providers.strategy import CompanyNewsStrategy
from news.verification_acquisition import LiveEvidenceAcquirer
from news.verification_v2 import verify_candidate


class Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_missing_fmp_key_disables_provider():
    provider = FMPProvider(token="")
    assert provider.enabled is False
    assert provider.status == "DISABLED_NOT_CONFIGURED"
    assert provider.search_company("BA", "2026-09-28", "2026-09-29") == []


def test_fmp_news_normalizes_response():
    provider = FMPProvider(token="secret", request_get=lambda *a, **k: Response([{"symbol": "BA", "title": "Boeing shares rebound", "text": "Boeing update", "url": "https://example/news", "publishedDate": "2026-09-28 12:00:00", "site": "Yahoo"}]))
    result = provider.search_company("BA", "2026-09-28", "2026-09-29")
    assert len(result) == 1
    assert result[0].discovery_source == "fmp"
    assert result[0].content_type == "NEWS"
    assert result[0].provider_article_id
    assert result[0].verification == "NOT_CONFIRMED"


def test_fmp_press_release_normalizes_as_evidence_candidate():
    provider = FMPProvider(token="secret", request_get=lambda *a, **k: Response([{"symbol": "BA", "title": "Boeing raises guidance", "text": "Boeing raises guidance", "url": "https://boeing.example/release", "publishedDate": "2026-09-28 12:00:00", "site": "Boeing IR"}]))
    result = provider.search_press_releases("BA", "2026-09-28", "2026-09-29")
    assert result[0].content_type == "PRESS_RELEASE"
    assert result[0].verification == "NOT_CONFIRMED"
    assert result[0].raw_metadata["provider"] == "FMP"


def test_matching_fmp_release_enters_existing_verifier_without_bypassing_it():
    class FakeFMP:
        enabled = True
        evidence_matches = 0
        no_match = 0

        def search_press_releases(self, symbol, start, end):
            from news.schema import NewsCandidate
            return [NewsCandidate("Boeing raises guidance", "Boeing raises guidance", "2026-09-28T12:00:00+00:00", "Boeing IR", "boeing.example", 3, "https://boeing.example/release", "EARNINGS", [symbol], "fmp", False, "NOT_CONFIRMED", "PRESS_RELEASE", False)]

    candidate = {"title": "Boeing raises guidance", "summary": "Boeing raises guidance", "category": "GUIDANCE", "published_at": "2026-09-28T13:00:00+00:00", "affected_symbols": ["BA"]}
    acquirer = LiveEvidenceAcquirer(logging.getLogger("test"), "2026-09-28", max_requests=0, fmp_provider=FakeFMP())
    evidence = acquirer._official(candidate, __import__("news.verification_v2", fromlist=["VerificationDiagnostics"]).VerificationDiagnostics())
    assert evidence and evidence[0]["provider"] == "FMP"
    result, diagnostics = verify_candidate(candidate, evidence)
    assert result["verification"] == "VERIFIED"
    assert diagnostics.matched_source_type == "OFFICIAL"


def test_unrelated_fmp_release_is_rejected_by_company_match():
    class FakeFMP:
        enabled = True
        evidence_matches = 0
        no_match = 0

        def search_press_releases(self, symbol, start, end):
            from news.schema import NewsCandidate
            return [NewsCandidate("Apple launches product", "Apple product", "2026-09-28T12:00:00+00:00", "Apple IR", "apple.example", 3, "https://apple.example/release", "PRODUCT", ["AAPL"], "fmp", False, "NOT_CONFIRMED", "PRESS_RELEASE", False)]

    candidate = {"title": "Boeing raises guidance", "summary": "Boeing raises guidance", "category": "GUIDANCE", "published_at": "2026-09-28T13:00:00+00:00", "affected_symbols": ["BA"]}
    acquirer = LiveEvidenceAcquirer(logging.getLogger("test"), "2026-09-28", max_requests=0, fmp_provider=FakeFMP())
    evidence = acquirer._official(candidate, __import__("news.verification_v2", fromlist=["VerificationDiagnostics"]).VerificationDiagnostics())
    assert evidence == []


def test_fmp_company_news_is_used_only_after_no_usable_finnhub_candidates():
    class DisabledMarketaux:
        available = False

    class EmptyFinnhub:
        enabled = True
        last_raw_count = 1

        def search_company(self, *args):
            return []

    class FallbackFMP:
        enabled = True

        def search_company(self, *args):
            from news.schema import NewsCandidate
            return [NewsCandidate("Boeing reports contract", None, "2026-09-28T12:00:00+00:00", "FMP", "fmp.example", 3, "https://fmp.example/a", "CONTRACT", ["BA"], "fmp")]

    strategy = CompanyNewsStrategy(DisabledMarketaux(), EmptyFinnhub(), tertiary=FallbackFMP())
    result = strategy.search_company("BA", "2026-09-28", "2026-09-29", "Boeing")
    assert result and result[0].discovery_source == "fmp"
    assert strategy.metrics["fmp_fallback_activated"] is True
