from drivers.cross_asset import build_cross_asset_hypotheses, market_driver_causal_gate
from drivers.detector import build_driver_analysis
from drivers.market_driver_discovery import build_market_driver_queries
from drivers.market_regime import detect_market_regimes
from drivers.sector_driver import build_sector_driver_analysis


def _report(*, equities=(-1.4, -2.2, -0.8, -1.6), us10y=14, vix=12, wti=0, xle=1.0):
    return {
        "market_date": "2026-09-28",
        "market": {"core": {symbol: {"change_pct": move} for symbol, move in zip(("SPY", "QQQ", "DIA", "IWM"), equities)}},
        "macro": {"US10Y": {"change_bps": us10y}, "VIX": {"change_pct": vix}, "WTI": {"change_pct": wti}, "DXY": {"change_pct": 0.6}},
        "sectors": {"all": [{"symbol": symbol, "change_pct": move} for symbol, move in {"XLK": -2.8, "XLRE": -2.5, "XLU": -2.2, "XLE": xle, "XLF": -1.0, "XLI": -1.0, "XLB": -1.0, "XLV": -1.0, "XLY": -1.0, "XLP": -1.0, "XLC": -1.0}.items()]},
        "movers": {"top_gainers": [], "top_losers": []},
    }


def test_regime_and_cross_asset_hypothesis_are_deterministic():
    report = _report()
    regime = detect_market_regimes(report)
    assert {"BROAD_RISK_OFF", "RISING_YIELDS", "TECH_SELL_OFF", "VOLATILITY_SPIKE"}.issubset(regime["regimes"])
    ids = {item["hypothesis_id"] for item in build_cross_asset_hypotheses(report, regime)}
    assert "RATE_PRESSURE_ON_GROWTH" in ids


def test_yields_up_but_equities_rally_does_not_force_bearish_rate_hypothesis():
    report = _report(equities=(1.0, 1.4, 0.9, 1.2), us10y=14, vix=0)
    assert "RATE_PRESSURE_ON_GROWTH" not in {item["hypothesis_id"] for item in build_cross_asset_hypotheses(report)}


def test_oil_relationship_conflict_does_not_create_energy_hypothesis():
    report = _report(wti=4.0, xle=-2.0)
    assert "OIL_RALLY_SUPPORTING_ENERGY" not in {item["hypothesis_id"] for item in build_cross_asset_hypotheses(report)}


def test_sector_driver_and_stock_inheritance_are_explicit():
    report = _report()
    report["movers"]["top_losers"] = [{"symbol": "META", "change_pct": -4.8}]
    analysis = build_driver_analysis(report)
    assert analysis["market_driver_analysis"]["primary_driver"]
    assert analysis["sector_driver_analysis"]
    meta = next(item for item in analysis["stock_explanations"] if item["symbol"] == "META")
    assert meta["stock_specific_catalyst_status"] == "NOT_FOUND"
    assert meta["overall_explanation_status"] == "UNEXPLAINED"


def test_market_causal_gate_rejects_future_and_generic_commentary():
    hypothesis = {"supporting_market_data": {"signals": []}}
    future = market_driver_causal_gate({"published_at": "2026-09-29", "title": "Stocks fell as yields jumped"}, hypothesis, "2026-09-28")
    generic = market_driver_causal_gate({"published_at": "2026-09-28", "title": "Why stocks could fall this year"}, hypothesis, "2026-09-28")
    assert future["causal_reason"] == "FUTURE_EVENT"
    assert generic["causal_reason"] == "COMMENTARY_ONLY"


def test_discovery_generates_bounded_date_scoped_queries_without_network():
    queries = build_market_driver_queries({"hypothesis_id": "RATE_PRESSURE_ON_GROWTH"}, "2026-09-28")
    assert queries and all(item["market_date"] == "2026-09-28" for item in queries)


def test_market_driver_payload_keeps_hypothesis_separate_from_verified_cause():
    report = _report()
    analysis = build_driver_analysis(report)
    primary = analysis["market_driver_analysis"]["primary_driver"]
    assert primary["status"] == "HYPOTHESIS"
    assert primary["verification_status"] == "NO_MARKET_DRIVER_FOUND"
    assert primary["hypothesis_status"] == "HYPOTHESIS_ONLY"


def test_market_pattern_without_eligible_evidence_cannot_be_promoted_or_inherited():
    report = _report()
    report["movers"]["top_losers"] = [{"symbol": "META", "change_pct": -4.8}]
    analysis = build_driver_analysis(report)
    primary = analysis["market_driver_analysis"]["primary_driver"]
    assert primary["verification_status"] == "NO_MARKET_DRIVER_FOUND"
    assert primary["hypothesis_status"] == "HYPOTHESIS_ONLY"
    meta = next(item for item in analysis["stock_explanations"] if item["symbol"] == "META")
    assert meta["inherited_market_driver"] is None


def test_subject_or_causal_gate_failure_cannot_promote_market_hypothesis():
    report = _report()
    candidate = {
        "published_at": "2026-09-28",
        "title": "Stocks fell as volatility jumped",
        "causal_eligible": True,
        "subject_gate": "FAIL",
        "causal_gate": "PASS",
    }
    analysis = build_driver_analysis({**report, "market_candidate_stories": [candidate]})
    assert analysis["market_driver_analysis"]["primary_driver"]["verification_status"] == "NO_MARKET_DRIVER_FOUND"


def test_eligible_market_evidence_can_promote_but_direction_conflict_blocks_inheritance():
    report = _report()
    candidate = {
        "published_at": "2026-09-28",
        "title": "Stocks fell as volatility jumped",
        "causal_eligible": True,
        "subject_gate": "PASS",
        "causal_gate": "PASS",
    }
    report["movers"]["top_gainers"] = [{"symbol": "PANW", "change_pct": 4.63}]
    analysis = build_driver_analysis({**report, "market_candidate_stories": [candidate]})
    primary = analysis["market_driver_analysis"]["primary_driver"]
    assert primary["verification_status"] == "PLAUSIBLE_NOT_VERIFIED"
    panw = next(item for item in analysis["stock_explanations"] if item["symbol"] == "PANW")
    assert panw["inherited_market_driver"] is None
    assert panw["inheritance_rejection_reason"] == "MARKET_INHERITANCE_DIRECTION_CONFLICT"


def test_aligned_market_evidence_can_be_inherited():
    report = _report()
    report["movers"]["top_losers"] = [{"symbol": "PANW", "change_pct": -4.63}]
    candidate = {
        "published_at": "2026-09-28",
        "title": "Stocks fell as volatility jumped",
        "causal_eligible": True,
        "subject_gate": "PASS",
        "causal_gate": "PASS",
    }
    analysis = build_driver_analysis({**report, "market_candidate_stories": [candidate]})
    panw = next(item for item in analysis["stock_explanations"] if item["symbol"] == "PANW")
    assert panw["inherited_market_driver"] is not None


def test_sector_below_materiality_and_direction_conflict_are_not_inheritable():
    report = _report(wti=4.0, xle=0.37)
    analysis = build_driver_analysis(report)
    assert any("SECTOR_DRIVER_BELOW_MATERIALITY_THRESHOLD" in item["warnings"] for item in analysis["sector_driver_analysis"])
    assert all(item["eligible_for_inheritance"] is False for item in analysis["sector_driver_analysis"])

    conflict = _report(wti=4.0, xle=-2.0)
    conflict_analysis = build_sector_driver_analysis(conflict)
    assert any("SECTOR_DRIVER_DIRECTION_CONFLICT" in item["warnings"] for item in conflict_analysis)
