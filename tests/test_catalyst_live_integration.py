import logging
import json

import debug_catalyst_integration as debug
from news.catalyst import build_catalyst_snapshot_from_live_news


def _movers():
    return {
        "market_date": "2026-09-25",
        "top_gainers": [{"symbol": symbol, "company_name": f"{symbol} Corp", "change_pct": 1.0} for symbol in ("A", "B", "C", "D", "E")],
        "top_losers": [{"symbol": symbol, "company_name": f"{symbol} Corp", "change_pct": -1.0} for symbol in ("F", "G", "H", "I", "J")],
    }


def _live(stories):
    return {
        "market_date": "2026-09-25",
        "request_budget": {"total_news_seconds": 7.2, "official_requests": 1, "marketaux_requests_attempted": 2},
        "stories": stories,
    }


def test_live_news_evidence_maps_to_existing_catalyst_contract_without_network():
    story = {
        "title_he": "A העלתה תחזית",
        "summary_he": "החברה עדכנה את התחזית.",
        "category": "EARNINGS",
        "published_at": "2026-09-25T15:00:00Z",
        "affected_symbols": ["A"],
        "verification": "VERIFIED",
        "editorial_eligible": True,
        "primary_source_name": "SEC EDGAR",
        "primary_source_domain": "sec.gov",
        "primary_source_url": "https://sec.gov/filing",
        "primary_source_type": "OFFICIAL",
        "supporting_sources_count": 1,
    }
    snapshot = build_catalyst_snapshot_from_live_news(_movers(), _live([story]), logger=logging.getLogger("test"))

    assert snapshot["catalysts"]["A"]["catalyst_status"] == "CONFIRMED"
    assert snapshot["catalysts"]["A"]["primary_source"]["source_name"] == "SEC EDGAR"
    assert snapshot["catalysts"]["B"]["catalyst_status"] == "NO_CONFIRMED_CATALYST"
    assert snapshot["request_statistics"]["catalyst_network_requests"] == 0
    assert snapshot["request_statistics"]["live_news_seconds"] == 7.2
    assert set(snapshot["catalysts"]) == set("ABCDEFGHIJ")


def test_zero_verified_live_catalysts_do_not_trigger_gap_fill():
    snapshot = build_catalyst_snapshot_from_live_news(_movers(), _live([]), logger=logging.getLogger("test"))

    assert all(item["catalyst_status"] == "NO_CONFIRMED_CATALYST" for item in snapshot["catalysts"].values())
    assert snapshot["summary"]["NO_CONFIRMED_CATALYST"] == 10
    assert snapshot["request_statistics"]["catalyst_gap_fill_seconds"] == 0.0
    assert snapshot["request_statistics"]["catalyst_network_requests"] == 0


def test_pathological_legacy_provider_is_not_constructed(monkeypatch):
    class ShouldNeverConstruct:
        def __init__(self, *args, **kwargs):
            raise AssertionError("legacy catalyst network provider was constructed")

    monkeypatch.setattr("news.catalyst.CatalystEngine", ShouldNeverConstruct)
    snapshot = build_catalyst_snapshot_from_live_news(_movers(), _live([]))
    assert snapshot["request_statistics"]["catalyst_network_requests"] == 0


def test_debug_movers_date_match_passes(tmp_path):
    path = tmp_path / "movers_snapshot.json"
    path.write_text(json.dumps(_movers()), encoding="utf-8")
    loaded = debug.load_movers_snapshot_for_date(path, "2026-09-25")
    assert loaded["market_date"] == "2026-09-25"


def test_debug_movers_date_mismatch_fails_explicitly(tmp_path):
    payload = _movers()
    payload["market_date"] = "2026-09-28"
    path = tmp_path / "movers_snapshot.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        debug.load_movers_snapshot_for_date(path, "2026-09-25")
    except RuntimeError as exc:
        assert str(exc) == "DEBUG_MOVERS_DATE_MISMATCH\nrequested=2026-09-25\nsnapshot=2026-09-28"
    else:
        raise AssertionError("expected explicit movers date mismatch")


def test_debug_date_consistency_requires_all_three_payloads(capsys):
    assert debug.validate_date_consistency("2026-09-25", _movers(), _live([]), {"market_date": "2026-09-25"})
    assert "PASS" in capsys.readouterr().out
    live = _live([])
    live["market_date"] = "2026-09-28"
    assert not debug.validate_date_consistency("2026-09-25", _movers(), live, {"market_date": "2026-09-25"})
    assert "FAIL" in capsys.readouterr().out
