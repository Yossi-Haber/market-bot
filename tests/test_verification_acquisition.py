import logging
import json

import pytest

from news.live import LiveNewsCoverage
from news.marketaux import infer_category
from news.verification_acquisition import LiveEvidenceAcquirer
from news.verification_acquisition import resolve_verification_route, _safe_cache_name
from news.verification_v2 import verify_candidate


def _candidate(title="Company announces acquisition", symbol="MCHP", domain="marketaux.example"):
    return {
        "title": title,
        "published_at": "2026-09-25T12:00:00Z",
        "category": "M&A",
        "affected_symbols": [symbol],
        "source_domain": domain,
        "source_url": f"https://{domain}/story",
    }


class Response:
    def __init__(self, text="", payload=None, content=None):
        self.text = text
        self._payload = payload
        self.content = content if content is not None else text.encode()

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_live_acquirer_official_ir_metadata_verifies(tmp_path, monkeypatch):
    monkeypatch.setattr("news.verification_acquisition.CACHE_DIR", tmp_path)
    html = '<html><head><title>Company announces acquisition</title><meta property="article:published_time" content="2026-09-25T12:05:00Z"></head></html>'
    acquirer = LiveEvidenceAcquirer(logging.getLogger("test"), "2026-09-25", request_get=lambda *a, **k: Response(html))
    evidence, diagnostics = acquirer.acquire([_candidate()])["Company announces acquisition"]
    result, _ = verify_candidate(_candidate(), evidence)
    assert result["verification"] == "VERIFIED"
    assert diagnostics.attempted_sources
    assert acquirer.request_count == 1


def test_live_acquirer_tier2_direct_metadata_verifies(tmp_path, monkeypatch):
    monkeypatch.setattr("news.verification_acquisition.CACHE_DIR", tmp_path)
    rss = b"<rss><channel><item><title>Company announces acquisition</title><link>https://reuters.com/story</link><pubDate>Fri, 25 Sep 2026 12:05:00 GMT</pubDate></item></channel></rss>"

    def request(url, **kwargs):
        if "google.com" in url:
            return Response(content=rss)
        return Response('<html><title>Company announces acquisition</title></html>')

    candidate = _candidate(symbol="UNKNOWN", domain="gurufocus.com")
    acquirer = LiveEvidenceAcquirer(logging.getLogger("test"), "2026-09-25", request_get=request)
    evidence, _ = acquirer.acquire([candidate])[candidate["title"]]
    result, _ = verify_candidate(candidate, evidence)
    assert result["verification"] == "VERIFIED"
    assert evidence[0]["source_domain"] == "reuters.com"
    assert acquirer.request_count <= 10


def test_same_tier2_source_is_not_independent(tmp_path, monkeypatch):
    monkeypatch.setattr("news.verification_acquisition.CACHE_DIR", tmp_path)
    rss = b"<rss><channel><item><title>Company announces acquisition</title><link>https://reuters.com/story</link><pubDate>Fri, 25 Sep 2026 12:05:00 GMT</pubDate></item></channel></rss>"
    candidate = _candidate(symbol="UNKNOWN", domain="reuters.com")
    acquirer = LiveEvidenceAcquirer(logging.getLogger("test"), "2026-09-25", request_get=lambda *a, **k: Response(content=rss))
    evidence, _ = acquirer.acquire([candidate])[candidate["title"]]
    assert evidence == []


def test_acquisition_budget_is_capped_at_ten_requests(tmp_path, monkeypatch):
    monkeypatch.setattr("news.verification_acquisition.CACHE_DIR", tmp_path)
    acquirer = LiveEvidenceAcquirer(logging.getLogger("test"), "2026-09-25", request_get=lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    candidates = [_candidate(title=f"Company announces acquisition {index}", symbol="UNKNOWN") for index in range(5)]
    acquirer.acquire(candidates)
    assert acquirer.request_count <= 10


def test_live_mode_invokes_acquisition_but_default_mode_does_not(monkeypatch):
    monkeypatch.setenv("ENABLE_OFFICIAL_NEWS", "false")
    monkeypatch.setenv("ENABLE_MARKETAUX", "true")
    coverage = LiveNewsCoverage(logging.getLogger("test"), "2026-09-25", perform_verification=True)
    coverage.marketaux.token = "configured"
    candidate = _candidate()
    from news.schema import NewsCandidate
    story = NewsCandidate(candidate["title"], None, candidate["published_at"], "Marketaux", candidate["source_domain"], 3, candidate["source_url"], "M&A", ["MCHP"], "marketaux")
    coverage.marketaux.search = lambda *a, **k: [story]
    called = []
    coverage.verification_acquirer.acquire = lambda items: called.append(items) or {story.title: ([], type("D", (), {"attempted_sources": ["OFFICIAL_IR:investor.microchip.com"]})())}
    coverage.collect()
    assert called


def test_marketaux_category_inference_is_explicit():
    assert infer_category("Company agrees to acquire rival") == "M&A"
    assert infer_category("Company raises guidance after earnings") == "EARNINGS"


def test_klxe_sec_exhibit_guidance_match(tmp_path, monkeypatch):
    monkeypatch.setattr("news.verification_acquisition.CACHE_DIR", tmp_path)
    submission = {"filings": {"recent": {"form": ["8-K"], "filingDate": ["2026-09-25"], "items": [["7.01"]], "primaryDocument": ["klxe.htm"], "accessionNumber": ["0000000-26-000001"]}}}

    def request(url, **kwargs):
        if url.endswith("klxe.htm"):
            return Response("<html>EX-99.1 Updates 2026 Third Quarter Guidance. KLXE raises Q3 revenue guidance.</html>")
        return Response(payload=submission)

    candidate = _candidate("KLXE Raises Q3 Revenue Guidance", "KLXE")
    candidate["sec_cik"] = "1234567890"
    acquirer = LiveEvidenceAcquirer(logging.getLogger("test"), "2026-09-25", request_get=request)
    evidence, _ = acquirer.acquire([candidate])[candidate["title"]]
    result, diagnostics = verify_candidate(candidate, evidence)
    assert result["verification"] == "VERIFIED"
    assert diagnostics.reason == "official_sec_exhibit_match"
    assert evidence[0]["filing_form"] == "8-K"
    assert "EX-99.1" in evidence[0]["exhibit_titles"]


def test_basf_evonik_official_discovery_without_ticker(tmp_path, monkeypatch):
    monkeypatch.setattr("news.verification_acquisition.CACHE_DIR", tmp_path / "evidence")
    monkeypatch.setattr("news.verification_acquisition.OFFICIAL_DISCOVERY_CACHE_DIR", tmp_path / "domains")
    rss = b"<rss><channel><item><title>BASF newsroom</title><link>https://www.basf.com/news/takeover</link><pubDate>Fri, 25 Sep 2026 12:05:00 GMT</pubDate></item></channel></rss>"

    def request(url, **kwargs):
        if "news.google.com" in url:
            return Response(content=rss)
        return Response("<html><title>BASF confirms exploratory talks regarding potential takeover of Evonik</title></html>")

    candidate = _candidate("BASF Proposes Acquisition of Evonik", symbol="", domain="marketaux.example")
    candidate["affected_symbols"] = []
    acquirer = LiveEvidenceAcquirer(logging.getLogger("test"), "2026-09-25", request_get=request)
    evidence, _ = acquirer.acquire([candidate])[candidate["title"]]
    result, diagnostics = verify_candidate(candidate, evidence)
    assert result["verification"] == "VERIFIED"
    assert diagnostics.reason == "official_ir_match"
    assert evidence[0]["source_domain"] == "basf.com"


def test_rocket_lab_iridium_merger_sec_match(tmp_path, monkeypatch):
    monkeypatch.setattr("news.verification_acquisition.CACHE_DIR", tmp_path)
    submission = {"filings": {"recent": {"form": ["8-K"], "filingDate": ["2026-09-25"], "items": [["8.01"]], "primaryDocument": ["merger.htm"], "accessionNumber": ["0000000-26-000002"]}}}

    def request(url, **kwargs):
        if url.endswith("merger.htm"):
            return Response("<html>EX-99.1 Merger agreement and shareholder approval process for Rocket Lab and Iridium.</html>")
        return Response(payload=submission)

    candidate = _candidate("Rocket Lab Advances Merger With Iridium", "RKLB")
    candidate["affected_symbols"] = ["RKLB", "IRDM"]
    candidate["entity_names"] = ["Rocket Lab", "Iridium"]
    candidate["sec_cik"] = "1234567890"
    acquirer = LiveEvidenceAcquirer(logging.getLogger("test"), "2026-09-25", request_get=request)
    evidence, _ = acquirer.acquire([candidate])[candidate["title"]]
    result, _ = verify_candidate(candidate, evidence)
    assert result["verification"] == "VERIFIED"


def test_guidance_precedes_m_and_a_category():
    assert infer_category("KLXE raises Q3 revenue guidance after acquisition update") == "EARNINGS"


def test_event_routes_are_centralized_and_event_aware():
    assert resolve_verification_route({"event_type": "REGULATORY", "title": "FAA delays certification"})[0] == "REGULATOR_OFFICIAL"
    assert resolve_verification_route({"event_type": "ANALYST_ACTION", "title": "UBS upgrades shares"}) == ["TIER2_SEARCH"]
    assert resolve_verification_route({"event_type": "EARNINGS", "title": "Company reports earnings"}) == ["COMPANY_IR", "SEC_EDGAR", "TIER2_SEARCH"]


def test_offline_regulator_route_is_cache_only_and_reports_miss(tmp_path, monkeypatch):
    monkeypatch.setattr("news.verification_acquisition.CACHE_DIR", tmp_path)
    candidate = _candidate("FAA regulatory review delays Boeing certification", "BA")
    candidate["event_type"] = "REGULATORY"
    acquirer = LiveEvidenceAcquirer(logging.getLogger("test"), "2026-09-25", cache_only=True, request_get=lambda *a, **k: pytest.fail("offline regulator route attempted network"))
    evidence, diagnostics = acquirer.acquire([candidate])[candidate["title"]]
    assert evidence == []
    assert diagnostics.attempted_sources[0]["type"].startswith("REGULATOR_OFFICIAL")
    assert diagnostics.attempted_sources[0]["status"] == "CACHE_MISS"
    assert acquirer.request_count == 0


def test_cached_regulator_evidence_can_confirm_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr("news.verification_acquisition.CACHE_DIR", tmp_path)
    candidate = _candidate("FAA regulatory review delays Boeing certification", "BA")
    candidate["event_type"] = "REGULATORY"
    cache = tmp_path / "2026-09-25" / _safe_cache_name(candidate, "regulator_FAA_faa.gov")
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps([{"source_type": "OFFICIAL", "source_name": "FAA Official", "source_domain": "faa.gov", "title": candidate["title"], "published_at": candidate["published_at"], "affected_symbols": ["BA"], "source_url": "https://www.faa.gov/notice"}]), encoding="utf-8")
    acquirer = LiveEvidenceAcquirer(logging.getLogger("test"), "2026-09-25", cache_only=True)
    evidence, diagnostics = acquirer.acquire([candidate])[candidate["title"]]
    result, _ = verify_candidate(candidate, evidence)
    assert diagnostics.attempted_sources[0]["status"] == "CACHE_HIT"
    assert result["verification"] == "VERIFIED"
