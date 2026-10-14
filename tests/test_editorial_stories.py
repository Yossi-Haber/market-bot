"""Offline tests for the News V2 editorial boundary."""

from datetime import datetime, timezone

from news.editorial import build_public_stories, cluster_stories
from visual.builder_v2 import build_view_model_v2


MARKET_DATE = "2026-09-25"
REFERENCE = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)


def verified(symbol, title, category="COMPANY", published_at="2026-09-25T14:32:00+00:00", source="Reuters", tier=2):
    return {symbol: {"catalyst_status": "CONFIRMED", "evidence_quality": "STRONG", "editorial_eligible": True, "catalyst_type": category, "headline": title, "event_datetime": published_at, "primary_source": {"source_name": source, "source_tier": tier, "publisher_domain": source.lower().replace(" ", "") + ".com", "published_at": published_at}}}


def test_raw_macro_moves_cannot_become_top_stories():
    catalysts = {
        "VIX": {"catalyst_status": "NOT_CONFIRMED", "headline": "VIX fell 5.11%"},
        "GOLD": {"catalyst_status": "NOT_CONFIRMED", "headline": "Gold rose 0.27%"},
    }
    assert build_public_stories(catalysts, MARKET_DATE, REFERENCE) == []


def test_verified_company_and_official_macro_release_are_public_stories():
    catalysts = verified("NVDA", "NVIDIA announced a new product", "TECH_PRODUCT")
    catalysts.update(verified("CPI", "BLS released the monthly inflation report", "MACRO_RELEASE", source="BLS", tier=1))
    stories = build_public_stories(catalysts, MARKET_DATE, REFERENCE)
    assert [item["category"] for item in stories] == ["TECH_PRODUCT", "MACRO_RELEASE"]
    assert stories[0]["verification"] == "VERIFIED"
    assert stories[0]["source_name"] == "Reuters"
    assert stories[0]["freshness"]["same_market_day"] is True


def test_same_day_story_outranks_older_equivalent_and_duplicates_cluster():
    candidates = verified("NVDA", "NVIDIA announces new chip", "TECH_PRODUCT", "2026-09-24T14:32:00+00:00")
    candidates.update(verified("AMD", "AMD announced guidance", "GUIDANCE", "2026-09-25T14:32:00+00:00"))
    stories = build_public_stories(candidates, MARKET_DATE, REFERENCE)
    assert stories[0]["affected_symbols"] == ["AMD"]
    duplicate = [
        {"title": "Company announces acquisition of Example", "category": "M&A", "published_at": "2026-09-25T12:00:00Z", "source_domain": "reuters.com"},
        {"title": "Company announced acquisition of Example", "category": "M&A", "published_at": "2026-09-25T12:10:00Z", "source_domain": "apnews.com"},
    ]
    assert len(cluster_stories(duplicate)) == 1
    assert cluster_stories(duplicate)[0]["supporting_sources_count"] == 2


def test_unconfirmed_and_zero_news_are_safe_and_driver_fallback_remains():
    catalysts = {"NVDA": {"catalyst_status": "NOT_CONFIRMED", "headline": "Unconfirmed announcement"}}
    assert build_public_stories(catalysts, MARKET_DATE, REFERENCE) == []
    data = {"market_date": MARKET_DATE, "market_overview": {"market_direction": "MIXED"}, "indices": [], "sectors": {"top": [], "bottom": [], "positive_count": 0, "valid_count": 0}, "macro": {}, "movers": {}, "verified_stories": [], "publication_readiness": {"status": "READY"}, "data_quality": {"warnings": []}}
    model = build_view_model_v2(data, "", {"selected_source": "DETERMINISTIC"})
    assert model["stories"] == []
    assert model["drivers"] == []
