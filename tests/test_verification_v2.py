"""Offline tests for bounded Verification V2 matching and materiality."""

from news.verification_v2 import is_material_event, public_story, verify_candidate


def candidate(title="Company announces acquisition", category="M&A", published="2026-09-25T12:00:00Z"):
    return {"title": title, "summary": "event", "published_at": published, "category": category, "affected_symbols": ["NVDA"], "source_domain": "reuters.com", "discovery_source": "marketaux", "verification": "NOT_CONFIRMED", "editorial_eligible": False}


def evidence(source_type, title="Company announces acquisition", domain="sec.gov", published="2026-09-25T12:30:00Z", symbols=None, conflict=False):
    return {"source_type": source_type, "title": title, "source_domain": domain, "published_at": published, "affected_symbols": symbols or ["NVDA"], "conflict": conflict}


def test_marketaux_plus_official_ir_or_sec_match_is_verified_and_eligible():
    result, diagnostics = verify_candidate(candidate(), [evidence("OFFICIAL", domain="company.com")])
    assert result["verification"] == "VERIFIED" and result["editorial_eligible"]
    assert diagnostics.matched_source_type == "OFFICIAL"


def test_marketaux_plus_reuters_confirmation_is_verified():
    result, diagnostics = verify_candidate(candidate(), [evidence("TIER2", domain="reuters.com")])
    assert result["verification"] == "VERIFIED"
    assert diagnostics.matched_source_type == "TIER2"


def test_marketaux_only_and_ticker_only_remain_unconfirmed():
    result, _ = verify_candidate(candidate(), [])
    assert result["verification"] == "NOT_CONFIRMED"
    ticker_only = evidence("TIER2", title="Completely unrelated headline", symbols=["NVDA"])
    result, _ = verify_candidate(candidate(), [ticker_only])
    assert result["verification"] == "NOT_CONFIRMED"


def test_conflict_is_explicitly_rejected():
    result, diagnostics = verify_candidate(candidate(), [evidence("OFFICIAL", conflict=True)])
    assert result["verification"] == "REJECTED_CONFLICT"
    assert diagnostics.reason == "conflicting_evidence"


def test_stale_match_and_non_material_event_are_not_eligible():
    stale = evidence("OFFICIAL", published="2026-09-20T12:30:00Z")
    result, _ = verify_candidate(candidate(), [stale])
    assert result["verification"] == "NOT_CONFIRMED"
    generic = candidate("Market commentary and outlook", "OTHER")
    result, _ = verify_candidate(generic, [evidence("OFFICIAL", title="Market commentary and outlook")])
    assert result["verification"] == "VERIFIED" and not result["editorial_eligible"]


def test_public_story_excludes_verification_diagnostics():
    result, _ = verify_candidate(candidate(), [evidence("TIER2")])
    result["attempted_sources"] = ["OFFICIAL"]
    public = public_story(result)
    assert "attempted_sources" not in public
    assert public["verification"] == "VERIFIED"


def test_materiality_terms_are_deterministic():
    assert is_material_event(candidate("Company reports earnings and guidance"))
    assert not is_material_event(candidate("Generic market commentary", "OTHER"))


def test_klxe_verified_public_source_contract_preserves_discovery():
    item = candidate("KLXE Raises Q3 Revenue Guidance", "EARNINGS")
    item.update({"source_name": "GuruFocus", "source_domain": "gurufocus.com", "source_url": "https://gurufocus.com/klxe"})
    sec = {
        "source_type": "OFFICIAL", "source_name": "SEC EDGAR", "source_domain": "sec.gov",
        "source_url": "https://www.sec.gov/Archives/edgar/data/123/filing.htm",
        "title": "SEC 8-K filing for KLXE", "summary": "7.01", "filing_items": "7.01",
        "exhibit_text": "EX-99.1 Updates 2026 Third Quarter Guidance for KLXE",
        "sec_exhibit_match": True, "published_at": "2026-09-25T12:30:00Z", "affected_symbols": ["NVDA"],
    }
    result, _ = verify_candidate(item, [sec])
    public = public_story(result)
    assert public["verification"] == "VERIFIED"
    assert public["editorial_eligible"] is True
    assert public["category"] == "EARNINGS"
    assert public["primary_source_domain"] == "sec.gov"
    assert public["primary_source_type"] == "OFFICIAL"
    assert public["supporting_sources_count"] == 1
    assert result["discovery_source_domain"] == "gurufocus.com"
    assert "source_name" not in public


def test_duplicate_sec_pages_do_not_inflate_supporting_sources():
    item = candidate("Company announces acquisition")
    first = evidence("OFFICIAL", title="Company announces acquisition", domain="sec.gov")
    second = {**first, "source_url": "https://www.sec.gov/exhibit/99-1", "exhibit_text": "EX-99.1 acquisition"}
    result, _ = verify_candidate(item, [first, second])
    assert result["supporting_sources_count"] == 1
