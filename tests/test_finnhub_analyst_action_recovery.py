from drivers.causal import evaluate_causal_eligibility
from drivers.runtime import _candidate_from_cluster, _select_cluster
from drivers.sector_driver import inherit_stock_explanations
from news.providers.finnhub import (
    analyst_action_direction_compatible,
    cluster_finnhub_candidates,
    parse_analyst_action,
    rank_finnhub_candidates,
)
from news.schema import NewsCandidate


def article(title, summary, symbol, published="2026-09-25T11:38:18+00:00"):
    return NewsCandidate(
        title=title,
        summary=summary,
        published_at=published,
        source_name="Benzinga",
        source_domain="benzinga.com",
        source_tier=3,
        source_url="https://example.test/article",
        category="OTHER",
        affected_symbols=[symbol],
        discovery_source="finnhub",
        raw_metadata={},
    )


def test_cached_humana_upgrade_recovers_direct_subject_and_parses_fields():
    item = article(
        "Barclays Upgrades Humana to Overweight, Raises Price Target to $515",
        "Barclays analyst Andrew Mok upgrades Humana (NYSE:HUM) from Equal-Weight "
        "to Overweight and raises the price target from $407 to $515.",
        "HUM",
    )
    parsed = parse_analyst_action(item, "HUM", "Humana Inc.")
    ranked = rank_finnhub_candidates([item], "HUM", "Humana Inc.")[0]

    assert parsed == {
        "analyst_firm": "Barclays",
        "action_type": "UPGRADE",
        "rating_from": "EQUAL-WEIGHT",
        "rating_to": "OVERWEIGHT",
        "price_target_from": 407.0,
        "price_target_to": 515.0,
        "target_symbol": "HUM",
        "subject_confidence": "DIRECT_TARGET",
        "action_direction": "POSITIVE",
    }
    assert ranked["target_role"] == "PRIMARY_SUBJECT"
    assert ranked["subject_relevance_score"] >= 30
    assert ranked["event_type"] == "ANALYST_ACTION"


def test_panw_maintained_buy_with_target_raise_recovers_suffix_variant():
    item = article(
        "BTIG Maintains Buy on Palo Alto Networks, Raises Price Target to $425",
        "BTIG analyst Gray Powell maintains Palo Alto Networks (NASDAQ:PANW) with a "
        "Buy and raises the price target from $404 to $425.",
        "PANW",
        "2026-09-28T06:16:14+00:00",
    )
    parsed = parse_analyst_action(item, "PANW", "Palo Alto Networks, Inc.")

    assert parsed["subject_confidence"] == "DIRECT_TARGET"
    assert parsed["target_symbol"] == "PANW"
    assert parsed["analyst_firm"] == "BTIG"
    assert parsed["action_type"] == "REITERATION_WITH_TARGET_CHANGE"
    assert parsed["price_target_from"] == 404.0
    assert parsed["price_target_to"] == 425.0


def test_direct_downgrade_is_parsed_and_directional():
    item = article(
        "Morgan Stanley Downgrades Example Holdings to Underweight",
        "Morgan Stanley analyst downgrades Example Holdings from Equal-Weight to Underweight.",
        "EXH",
    )
    parsed = parse_analyst_action(item, "EXH", "Example Holdings")

    assert parsed["action_type"] == "DOWNGRADE"
    assert parsed["action_direction"] == "NEGATIVE"
    assert parsed["subject_confidence"] == "DIRECT_TARGET"
    assert analyst_action_direction_compatible(parsed, -4.0)
    assert not analyst_action_direction_compatible(parsed, 4.0)


def test_price_target_raise_with_clear_target_is_eligible():
    item = article(
        "BTIG Raises Example Holdings Price Target to $125",
        "BTIG analyst raises the price target from $110 to $125 for Example Holdings.",
        "EXH",
    )
    parsed = parse_analyst_action(item, "EXH", "Example Holdings")
    assert parsed["action_type"] == "PRICE_TARGET_RAISE"
    assert parsed["price_target_from"] == 110.0
    assert parsed["price_target_to"] == 125.0
    assert parsed["subject_confidence"] == "DIRECT_TARGET"


def test_single_announced_target_is_extracted_without_inventing_prior_target():
    item = article(
        "BNP Paribas Upgrades Synopsys to Neutral, Announces $420 Price Target",
        "BNP Paribas upgraded Synopsys from Underperform to Neutral and announced a $420 target.",
        "SNPS",
        "2026-09-24T14:28:04+00:00",
    )
    parsed = parse_analyst_action(item, "SNPS", "Synopsys, Inc.")
    assert parsed["rating_from"] == "UNDERPERFORM"
    assert parsed["rating_to"] == "NEUTRAL"
    assert parsed["price_target_from"] is None
    assert parsed["price_target_to"] == 420.0


def test_roundup_consensus_and_generic_opinion_remain_ineligible():
    item = article(
        "This 3M Analyst Is No Longer Bearish; Here Are Top 5 Upgrades For Friday",
        "Analysts upgraded First Financial, Synopsys and Humana.",
        "HUM",
    )
    parsed = parse_analyst_action(item, "HUM", "Humana Inc.")
    assert parsed["subject_confidence"] == "INCIDENTAL"
    assert parsed["target_symbol"] is None

    consensus = article(
        "Analysts Raise Palo Alto Networks Stock Price Targets - What's the Best PANW Play?",
        "Analysts have hiked price targets for Palo Alto Networks.",
        "PANW",
        "2026-09-28T12:45:02+00:00",
    )
    assert parse_analyst_action(consensus, "PANW", "Palo Alto Networks, Inc.")["subject_confidence"] != "DIRECT_TARGET"


def test_peer_or_unrelated_ticker_mention_is_not_direct_target():
    peer = article(
        "UnitedHealth Shares Rise as Humana Faces Medicare Rating Risk",
        "Analysts discuss the insurer sector; no new Humana rating action was announced.",
        "HUM",
    )
    assert parse_analyst_action(peer, "HUM", "Humana Inc.")["subject_confidence"] == "INCIDENTAL"

    wrong_symbol = article(
        "Barclays Upgrades Humana to Overweight",
        "Barclays upgrades Humana from Equal-Weight to Overweight.",
        "CVS",
    )
    assert parse_analyst_action(wrong_symbol, "HUM", "Humana Inc.")["subject_confidence"] == "INCIDENTAL"


def test_ambiguous_target_abstains_and_does_not_infer_missing_values():
    item = article(
        "Humana Shares Move After Analyst Commentary",
        "Analysts discussed Humana valuation without a new rating or price-target action.",
        "HUM",
    )
    parsed = parse_analyst_action(item, "HUM", "Humana Inc.")
    assert parsed["subject_confidence"] == "AMBIGUOUS"
    assert parsed["action_type"] is None
    assert parsed["analyst_firm"] is None
    assert parsed["price_target_to"] is None


def test_future_action_and_direction_conflict_cannot_be_selected():
    future = article(
        "BTIG Maintains Buy on Palo Alto Networks, Raises Price Target to $425",
        "BTIG analyst raises target from $404 to $425.",
        "PANW",
        "2026-10-03T06:16:14+00:00",
    )
    timely = article(
        "BTIG Maintains Buy on Palo Alto Networks, Raises Price Target to $425",
        "BTIG analyst Gray Powell maintains Buy and raises target from $404 to $425.",
        "PANW",
        "2026-09-28T06:16:14+00:00",
    )
    clusters = cluster_finnhub_candidates([future, timely], "PANW", "Palo Alto Networks, Inc.")
    selected = _select_cluster(clusters, "PANW", "2026-09-28", 4.6)
    assert selected is not None
    # The canonical article timestamp remains authoritative: a cluster whose
    # selected item is after close cannot borrow the earlier member's time.
    future_candidate = _candidate_from_cluster(clusters[0], "PANW")
    timing = evaluate_causal_eligibility(future_candidate, "2026-09-28")
    assert timing["causal_eligible"] is False
    assert timing["causal_reason"] == "TIMING_INELIGIBLE"
    timely_cluster = cluster_finnhub_candidates([timely], "PANW", "Palo Alto Networks, Inc.")[0]
    assert _select_cluster([timely_cluster], "PANW", "2026-09-28", 4.6) is timely_cluster
    assert not analyst_action_direction_compatible(selected["analyst_action"], -4.6)


def test_direct_action_cluster_beats_incidental_high_ranked_cluster():
    incidental = article(
        "Virtualization Security Market Report Benchmarks Palo Alto Networks",
        "A broad industry market report.",
        "PANW",
        "2026-09-28T09:27:00+00:00",
    )
    action = article(
        "BTIG Maintains Buy on Palo Alto Networks, Raises Price Target to $425",
        "BTIG analyst Gray Powell maintains Buy and raises target from $404 to $425.",
        "PANW",
        "2026-09-28T06:16:14+00:00",
    )
    clusters = cluster_finnhub_candidates([incidental, action], "PANW", "Palo Alto Networks, Inc.")
    assert clusters[0]["canonical_event_title"] == action.title
    selected = _select_cluster(clusters, "PANW", "2026-09-28", 4.6)
    assert selected["canonical_event_title"] == action.title


def test_stock_specific_analyst_action_outranks_market_and_sector_inheritance():
    item = article(
        "BTIG Maintains Buy on Palo Alto Networks, Raises Price Target to $425",
        "BTIG analyst Gray Powell maintains Buy and raises target from $404 to $425.",
        "PANW",
        "2026-09-28T06:16:14+00:00",
    )
    parsed = parse_analyst_action(item, "PANW", "Palo Alto Networks, Inc.")
    candidate = {
        "title": item.title,
        "affected_symbols": ["PANW"],
        "causal_eligible": True,
        "verification": "NOT_CONFIRMED",
        "analyst_action": parsed,
    }
    stock = {
        "driver_id": "STOCK_PANW",
        "related_symbols": ["PANW"],
        "movement_evidence": [{"value": 4.629878}],
        "verification_status": "PLAUSIBLE_NOT_VERIFIED",
        "candidate_stories": [candidate],
    }
    sector = {
        "driver_id": "SECTOR_TECHNOLOGY",
        "scope": "TECHNOLOGY",
        "related_sector": "TECHNOLOGY",
        "direction": "up",
        "strength": 2.0,
        "verification_status": "PLAUSIBLE_NOT_VERIFIED",
        "candidate_stories": [{"title": "Technology stocks rise", "causal_eligible": True, "causal_gate": "PASS"}],
    }
    market = {
        "primary_driver": {
            "hypothesis_id": "BROAD", "scope": "US_MARKET", "direction": "down",
            "verification_status": "PLAUSIBLE_NOT_VERIFIED",
            "candidate_stories": [{"title": "Market declines"}],
        }
    }
    result = inherit_stock_explanations([stock], [sector], market, {})[0]
    assert result["overall_explanation_status"] == "STOCK_SPECIFIC"
    assert result["inherited_market_driver"] is None
    assert result["inherited_sector_driver"] is None
