"""Tests for deterministic report composition selectors and readiness."""

import json
from pathlib import Path

import pytest

from report import composer
from report.selectors import build_indices, build_macro, build_market_overview, build_sectors, validate_movers


FIXTURE = Path(__file__).parent / "fixtures" / "report_data.json"


def fixture_data():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def raw_market():
    return {
        "core": {
            symbol: {"symbol": symbol, "display_name": symbol, "close": 100.0, "change": value, "change_pct": value, "verified": True}
            for symbol, value in {"SPY": 0.5, "QQQ": 0.4, "DIA": 0.9, "IWM": 0.1}.items()
        },
        "sectors": {
            "XLK": {"symbol": "XLK", "display_name": "Technology", "close": 100.0, "change": 2.0, "change_pct": 2.0, "verified": True},
            "XLF": {"symbol": "XLF", "display_name": "Financials", "close": 100.0, "change": 1.0, "change_pct": 1.0, "verified": True},
            "XLE": {"symbol": "XLE", "display_name": "Energy", "close": 100.0, "change": -1.0, "change_pct": -1.0, "verified": True},
        },
        "macro": {
            "WTI": {"symbol": "WTI", "close": 96.41, "change": -0.56, "change_pct": -0.57, "freshness": "lagging", "verified": True},
            "US10Y": {"symbol": "US10Y", "close": 5.18, "change": 0.07, "change_pct": 1.37, "change_bps": 7.0, "freshness": "lagging", "verified": True},
        },
    }


def test_core_etfs_are_proxies_and_sector_order_is_deterministic():
    indices, invalid = build_indices(raw_market())
    sectors, sector_invalid = build_sectors(raw_market())
    assert not invalid
    assert not sector_invalid
    assert all(item["representation"] == "ETF_PROXY" for item in indices)
    assert [item["symbol"] for item in sectors["top"]] == ["XLK", "XLF", "XLE"]
    assert [item["symbol"] for item in sectors["bottom"]] == ["XLE", "XLF", "XLK"]


def test_breadth_and_macro_fields_are_preserved():
    indices, _ = build_indices(raw_market())
    sectors, _ = build_sectors(raw_market())
    macro, quality, _ = build_macro(raw_market())
    overview = build_market_overview(indices, sectors["all"])
    assert overview["market_direction"] == "BROADLY_POSITIVE"
    assert sectors["breadth"]["percentage"] == pytest.approx(66.6666666667)
    assert macro["US10Y"]["change_bps"] == 7.0
    assert macro["WTI"]["freshness"] == "lagging"
    assert quality["lagging_count"] == 2


def test_mover_order_is_corrected():
    gainers, losers, warnings, _ = validate_movers({"top_gainers": [{"symbol": "A", "change_pct": 1}, {"symbol": "B", "change_pct": 3}], "top_losers": [{"symbol": "C", "change_pct": -1}, {"symbol": "D", "change_pct": -3}]})
    assert [item["symbol"] for item in gainers] == ["B", "A"]
    assert [item["symbol"] for item in losers] == ["D", "C"]
    assert "GAINER_ORDER_CORRECTED" in warnings
    assert "LOSER_ORDER_CORRECTED" in warnings


def patched_composer(monkeypatch, tmp_path, dates):
    monkeypatch.setattr(composer, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(composer, "build_indices", lambda _: ([{"symbol": "SPY", "close": 100, "change": 1, "change_pct": 1, "representation": "ETF_PROXY"}] * 4, []))
    monkeypatch.setattr(composer, "build_sectors", lambda _: ({"all": [{"symbol": "XLK", "change_pct": 1}], "top": [], "bottom": [], "valid_count": 11, "breadth": {"fraction": 1, "percentage": 100}}, []))
    monkeypatch.setattr(composer, "build_macro", lambda _: ({}, {"current_count": 0, "lagging_count": 0, "stale_count": 0}, []))
    monkeypatch.setattr(composer, "validate_movers", lambda _: ([], [], [], []))
    for filename, date in zip(("market_snapshot.json", "movers_snapshot.json", "catalysts_snapshot.json"), dates):
        payload = {"market_date": date, "market": {}, "catalysts": {}, "summary": {}}
        (tmp_path / filename).write_text(json.dumps(payload), encoding="utf-8")


def test_matching_dates_compose_successfully(monkeypatch, tmp_path):
    patched_composer(monkeypatch, tmp_path, ["2026-09-25"] * 3)
    assert composer.compose_daily_report() == tmp_path / "daily_report_data.json"


def test_market_driver_snapshot_is_source_of_truth(monkeypatch, tmp_path):
    patched_composer(monkeypatch, tmp_path, ["2026-09-25"] * 3)
    snapshot = {
        "market_date": "2026-09-25",
        "market_drivers_source_of_truth": True,
        "legacy_catalyst_used_for_driver": False,
        "driver_analysis": {
            "main_market_driver": None,
            "sector_drivers": [],
            "stock_catalysts": [{"driver_id": "STOCK_BA", "driver_type": "STOCK_CATALYST", "scope": "STOCK", "direction": "down", "strength": 6.9, "related_symbols": ["BA"], "movement_evidence": [{"value": -6.9}], "verification_status": "PLAUSIBLE_NOT_VERIFIED", "confidence_reason": "Not independently confirmed."}],
            "metrics": {"market_driver_hypotheses": 0, "sector_driver_hypotheses": 0, "stock_catalyst_targets": 1},
        },
        "metrics": {"fmp_requests": 0},
    }
    (tmp_path / "market_driver_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    composer.compose_daily_report()
    report = json.loads((tmp_path / "daily_report_data.json").read_text(encoding="utf-8"))
    assert report["stock_catalysts"][0]["driver_id"] == "STOCK_BA"
    assert report["stock_catalysts"][0]["verification_status"] == "PLAUSIBLE_NOT_VERIFIED"
    assert report["legacy_catalyst_used_for_driver"] is False


def test_mismatched_dates_raise_market_date_mismatch(monkeypatch, tmp_path):
    patched_composer(monkeypatch, tmp_path, ["2026-09-25", "2026-09-24", "2026-09-25"])
    with pytest.raises(composer.ReportCompositionError, match="MARKET_DATE_MISMATCH"):
        composer.compose_daily_report()


def test_catalyst_selection_and_lead_visibility_rules():
    catalysts = {
        "SAFE": {"catalyst_status": "CONFIRMED", "evidence_quality": "STRONG", "editorial_eligible": True, "catalyst_type": "FILING", "headline": "Verified", "event_datetime": "2026-09-25T12:00:00+00:00", "primary_source": {"source_name": "SEC", "source_tier": 1, "publisher_domain": "sec.gov", "published_at": "2026-09-25T12:00:00+00:00"}},
        "POSSIBLE": {"catalyst_status": "POSSIBLE", "evidence_quality": "STRONG", "editorial_eligible": False, "catalyst_type": "OTHER", "headline": "Possible"},
        "BAD": {"catalyst_status": "NOT_CONFIRMED", "evidence_quality": "MODERATE", "catalyst_type": "OTHER", "headline": "Unconfirmed"},
    }
    stories = composer.select_verified_stories(catalysts)
    leads = composer.select_unconfirmed_leads(catalysts)
    assert {item["symbol"] for item in stories} == {"SAFE"}
    assert all(item["symbol"] not in {"POSSIBLE", "BAD"} for item in stories)
    assert leads[0]["display_in_public_report"] is False


def test_publication_readiness_states():
    sectors = {"valid_count": 11}
    indices = [{"symbol": str(index)} for index in range(4)]
    assert composer.publication_readiness(indices, sectors, [], {"2026-09-25"}, []) ["status"] == "READY"
    assert composer.publication_readiness(indices, sectors, [], {"2026-09-25"}, ["MACRO_DATA_LAGGING"])["status"] == "READY_WITH_WARNINGS"
    assert composer.publication_readiness(indices[:3], sectors, [], {"2026-09-25"}, [])["status"] == "NOT_READY"


def test_unexplained_notice_selects_large_unverified_same_industry_moves_without_causal_claim():
    movers = {"top_gainers": [], "top_losers": [{"symbol": "T", "company_name": "AT&T Inc.", "change_pct": -10.856454}, {"symbol": "VZ", "company_name": "Verizon Communications", "change_pct": -10.140237}]}
    drivers = [{"related_symbols": ["T"], "verification_status": "PLAUSIBLE_NOT_VERIFIED"}, {"related_symbols": ["VZ"], "verification_status": "PLAUSIBLE_NOT_VERIFIED"}]
    notice, = composer.build_unexplained_move_notices(movers, drivers, [])
    assert notice["status"] == "UNEXPLAINED"
    assert notice["symbols"] == ["T", "VZ"]
    assert notice["industry"] == "COMMUNICATION_SERVICES"
    assert "10.86%" in notice["message_he"] and "10.14%" in notice["message_he"]
    assert "לא הצליחה לאמת את הגורם" in notice["message_he"]
    assert "Starlink" not in notice["message_he"]


def test_unexplained_notice_excludes_verified_symbols_and_requires_threshold():
    movers = {"top_gainers": [{"symbol": "T", "company_name": "AT&T", "change_pct": 11}], "top_losers": [{"symbol": "VZ", "company_name": "Verizon", "change_pct": -9.99}]}
    stories = [{"verification": "VERIFIED", "affected_symbols": ["T"]}]
    assert composer.build_unexplained_move_notices(movers, [], stories) == []
