from drivers.detector import build_driver_analysis
from drivers.sector_evidence import build_sector_evidence_pool, classify_sector_scope


def _report(sector="ENERGY", move=-2.8, wti=-3.1, direction="down"):
    etf = {"ENERGY": "XLE", "TECHNOLOGY": "XLK"}[sector]
    return {
        "market_date": "2026-09-16",
        "market": {"core": {}},
        "macro": {"WTI": {"change_pct": wti}},
        "sectors": {"all": {etf: {"change_pct": move, "sector_name": sector}}},
        "movers": {"top_gainers": [], "top_losers": []},
    }


def _energy_article(title, summary, published="2026-09-16T16:03:06+00:00", url="https://news.test/story"):
    return {"title": title, "summary": summary, "published_at": published, "source_url": url, "affected_symbols": ["COP"]}


def test_broad_energy_cached_evidence_promotes_without_verifying():
    raw = [_energy_article(
        "Oil Producers Slide as Crude Retreats",
        "Crude prices retreated and the pain landed across exploration and production names as oil prices retreated.",
    )]
    pool = build_sector_evidence_pool(raw, "2026-09-16")
    result = build_driver_analysis({**_report(), "sector_candidate_stories": pool})
    driver = next(item for item in result["sector_drivers"] if item["related_sector"] == "ENERGY")
    assert driver["verification_status"] == "PLAUSIBLE_NOT_VERIFIED"
    assert driver["candidate_stories"]
    assert driver["eligible_for_inheritance"] is True
    assert driver["verified_story"] is None


def test_stock_specific_earnings_does_not_promote_sector():
    article = _energy_article("Occidental Petroleum Q2 Earnings", "Occidental Petroleum reported earnings and raised guidance.")
    assert classify_sector_scope(article)["scope"] == "STOCK_SPECIFIC"
    pool = build_sector_evidence_pool([article], "2026-09-16")
    result = build_driver_analysis({**_report(), "sector_candidate_stories": pool})
    driver = next(item for item in result["sector_drivers"] if item["related_sector"] == "ENERGY")
    assert driver["verification_status"] == "NO_CATALYST_FOUND"


def test_single_stock_recap_is_not_relabelled_sector_evidence():
    article = _energy_article("Occidental Petroleum Stock Slides Wednesday: What's Happening?", "Energy equities experience broad profit-taking alongside a pullback in crude oil prices.")
    assert classify_sector_scope(article)["scope"] == "STOCK_SPECIFIC"


def test_duplicate_symbol_cache_copies_are_one_evidence_item():
    first = {**_energy_article("Oil Producers Slide as Crude Retreats", "Crude prices retreated, pressuring oil producers."), "cache_symbols": ["COP"]}
    second = {**first, "cache_symbols": ["EOG"]}
    pool = build_sector_evidence_pool([first, second], "2026-09-16")
    assert len(pool) == 1
    assert pool[0]["cache_symbols"] == ["COP", "EOG"]


def test_future_or_wrong_direction_evidence_cannot_promote():
    future = _energy_article("Oil Producers Rally as Crude Rises", "Crude prices rose, lifting energy equities.", "2026-09-17T12:00:00-04:00")
    pool = build_sector_evidence_pool([future], "2026-09-16")
    result = build_driver_analysis({**_report(), "sector_candidate_stories": pool})
    driver = next(item for item in result["sector_drivers"] if item["related_sector"] == "ENERGY")
    assert driver["verification_status"] == "NO_CATALYST_FOUND"


def test_semiconductor_theme_evidence_requires_material_sector_move():
    article = {"title": "Chipmakers Lead the Rebound", "summary": "Chipmakers led the rebound as yields retreated, lifting semiconductor stocks.", "published_at": "2026-09-17T13:22:35Z", "source_url": "https://news.test/chips"}
    pool = build_sector_evidence_pool([article], "2026-09-17")
    report = _report("TECHNOLOGY", 2.2, -0.4)
    report["market_date"] = "2026-09-17"
    analysis = build_driver_analysis({**report, "sector_candidate_stories": pool})
    driver = next(item for item in analysis["sector_drivers"] if item["related_sector"] == "TECHNOLOGY")
    assert driver["verification_status"] == "PLAUSIBLE_NOT_VERIFIED"
    assert driver["candidate_stories"]


def test_energy_move_without_cached_causal_article_does_not_promote():
    analysis = build_driver_analysis(_report())
    driver = next(item for item in analysis["sector_drivers"] if item["related_sector"] == "ENERGY")
    assert driver["verification_status"] == "NO_CATALYST_FOUND"
    assert driver["candidate_stories"] == []


def test_energy_causal_evidence_still_requires_material_wti_move():
    article = _energy_article("Oil Producers Slide as Crude Retreats", "Oil producers slid as crude prices retreated.")
    pool = build_sector_evidence_pool([article], "2026-09-16")
    analysis = build_driver_analysis({**_report(wti=-0.5), "sector_candidate_stories": pool})
    driver = next(item for item in analysis["sector_drivers"] if item["related_sector"] == "ENERGY")
    assert driver["verification_status"] == "NO_CATALYST_FOUND"


def test_point_in_time_future_evidence_is_not_eligible():
    article = _energy_article("Oil Producers Slide as Crude Retreats", "Oil producers slid as crude prices retreated.", "2026-09-16T17:00:01-04:00")
    pool = build_sector_evidence_pool([article], "2026-09-16")
    assert pool[0]["event_time_relation"] == "FUTURE_EVENT"
    assert pool[0]["sector_causal_eligible"] is False


def test_integrity_failure_blocks_sector_evidence_promotion():
    article = _energy_article("Oil Producers Slide as Crude Retreats", "Oil producers slid as crude prices retreated.")
    pool = build_sector_evidence_pool([article], "2026-09-16")
    analysis = build_driver_analysis({**_report(), "integrity_status": "FAIL", "sector_candidate_stories": pool})
    driver = next(item for item in analysis["sector_drivers"] if item["related_sector"] == "ENERGY")
    assert driver["verification_status"] == "NO_CATALYST_FOUND"


def test_sector_scope_taxonomy_is_deterministic_and_distinguishes_breadth():
    assert classify_sector_scope({"title": "Oil producers fall as crude retreats"})["scope"] == "COMMODITY_WITH_SECTOR_IMPACT"
    assert classify_sector_scope({"title": "Chipmakers led the rebound as yields retreat"})["scope"] == "MACRO_WITH_SECTOR_IMPACT"
    assert classify_sector_scope({"title": "Semiconductor stocks surge across the sector"})["scope"] == "THEME_WIDE"
    assert classify_sector_scope({"title": "Energy equities decline across the sector"})["scope"] == "SECTOR_WIDE"
    assert classify_sector_scope({"title": "Occidental Q2 earnings", "summary": "Occidental reported results.", "affected_symbols": ["OXY"]})["scope"] == "STOCK_SPECIFIC"
    assert classify_sector_scope({"title": "Rain in Boston"})["scope"] == "UNRELATED"


def test_syndicated_duplicate_does_not_inflate_source_count():
    article = _energy_article("Oil Producers Slide as Crude Retreats", "Oil producers slid as crude prices retreated.")
    copy = {**article, "source_name": "Syndicator", "source_url": article["source_url"], "cache_symbols": ["EOG"]}
    first = {**article, "source_name": "Yahoo", "cache_symbols": ["COP"]}
    pool = build_sector_evidence_pool([first, copy], "2026-09-16")
    assert len(pool) == 1
    assert pool[0]["article_count"] == 1
    assert pool[0]["cache_symbols"] == ["COP", "EOG"]


def test_sector_pool_does_not_change_stock_candidate_classification():
    report = _report()
    report["movers"] = {"top_gainers": [], "top_losers": [{"symbol": "OXY", "change_pct": -4.0}]}
    stock_story = {"title": "OXY earnings recap", "affected_symbols": ["OXY"]}
    bare = build_driver_analysis({**report, "candidate_stories": [stock_story]})
    sector = build_driver_analysis({**report, "candidate_stories": [stock_story], "sector_candidate_stories": build_sector_evidence_pool([_energy_article("Oil Producers Slide as Crude Retreats", "Oil producers slid as crude prices retreated.")], "2026-09-16")})
    assert [item["verification_status"] for item in bare["stock_catalysts"]] == [item["verification_status"] for item in sector["stock_catalysts"]]
