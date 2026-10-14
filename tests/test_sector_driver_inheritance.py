"""Deterministic association and causal-inheritance regression tests."""

from __future__ import annotations

import json
from pathlib import Path

from drivers.sector_driver import inherit_stock_explanations, resolve_stock_sector_association


CONFIG = Path(__file__).parents[1] / "drivers" / "config" / "sector_mappings.json"


def _stock(symbol: str, move: float, *, candidate: bool = False) -> dict:
    stories = ([{"affected_symbols": [symbol], "causal_eligible": True, "verification": "VERIFIED"}] if candidate else [])
    return {"driver_id": f"STOCK_{symbol}", "related_symbols": [symbol], "direction": "up" if move > 0 else "down", "movement_evidence": [{"value": move}], "verification_status": "NO_CATALYST_FOUND", "candidate_stories": stories}


def _sector(sector: str, direction: str, title: str, *, status: str = "PLAUSIBLE_NOT_VERIFIED", strength: float = 2.5, eligible: bool | None = None) -> dict:
    item = {"driver_id": f"SECTOR_{sector}", "driver_type": "SECTOR_DRIVER", "scope": sector, "related_sector": sector, "direction": direction, "strength": strength, "verification_status": status, "candidate_stories": [{"title": title, "causal_eligible": True, "causal_gate": "PASS", "event_time_relation": "BEFORE_OR_DURING_SESSION"}]}
    if eligible is not None:
        item["eligible_for_inheritance"] = eligible
    return item


def test_direct_sector_mapping_inherits_aligned_causal_driver():
    result = inherit_stock_explanations([_stock("COP", -4)], [_sector("ENERGY", "down", "Oil producers slide as crude retreats")], {}, {})
    assert result[0]["overall_explanation_status"] == "SECTOR_EXPLAINED"
    assert result[0]["sector_association_status"] == "DIRECT_SECTOR_MATCH"


def test_opposite_stock_and_sector_direction_cannot_inherit():
    result = inherit_stock_explanations([_stock("COP", 4)], [_sector("ENERGY", "down", "Oil producers slide as crude retreats")], {}, {})
    assert result[0]["inherited_sector_driver"] is None
    assert result[0]["sector_inheritance_rejection_reason"] == "DIRECTION_CONFLICT"


def test_hypothesis_only_sector_driver_cannot_inherit():
    result = inherit_stock_explanations([_stock("COP", -4)], [_sector("ENERGY", "down", "Oil producers slide", status="NO_CATALYST_FOUND", eligible=False)], {}, {})
    assert result[0]["inherited_sector_driver"] is None
    assert result[0]["sector_inheritance_rejection_reason"] in {"HYPOTHESIS_ONLY", "NO_CAUSAL_SECTOR_EVIDENCE"}


def test_stock_specific_explanation_outranks_sector_explanation():
    stock = _stock("COP", -4, candidate=True)
    stock["verification_status"] = "CONFIRMED"
    stock["candidate_stories"][0]["title"] = "Occidental announces a specific acquisition"
    result = inherit_stock_explanations([stock], [_sector("ENERGY", "down", "Oil producers slide as crude retreats")], {}, {})
    assert result[0]["overall_explanation_status"] == "STOCK_SPECIFIC"


def test_supported_semiconductor_theme_can_inherit_but_unrelated_theme_cannot():
    technology_driver = _sector("TECHNOLOGY", "up", "AI chip demand lifts semiconductor sector")
    qcom = inherit_stock_explanations([_stock("QCOM", 7)], [technology_driver], {}, {})[0]
    assert qcom["overall_explanation_status"] == "SECTOR_EXPLAINED"
    assert qcom["sector_association_status"] == "SUPPORTED_THEME_MATCH"
    unsupported = resolve_stock_sector_association("ABNB", {}, technology_driver, json.loads(CONFIG.read_text(encoding="utf-8")))
    assert unsupported["association_status"] == "NO_MATCH"


def test_sector_inheritance_requires_direction_and_causal_sector_terms():
    no_sector_context = _sector("ENERGY", "down", "Occidental Q2 earnings recap")
    result = inherit_stock_explanations([_stock("COP", -4)], [no_sector_context], {}, {})[0]
    assert result["inherited_sector_driver"] is None
    assert result["sector_inheritance_rejection_reason"] == "EVIDENCE_NOT_SECTOR_SCOPED"


def test_sector_explanation_remains_multifactor_with_existing_market_semantics():
    market = {"primary_driver": {"hypothesis_id": "BROAD", "scope": "US_MARKET", "direction": "down", "verification_status": "PLAUSIBLE_NOT_VERIFIED", "candidate_stories": [{"title": "Broad risk event"}]}}
    result = inherit_stock_explanations([_stock("COP", -4)], [_sector("ENERGY", "down", "Oil producers slide as crude retreats")], market, {})[0]
    assert result["overall_explanation_status"] == "MULTI_FACTOR"


def test_sector_scoped_market_hypothesis_does_not_leak_to_unrelated_stocks():
    market = {"primary_driver": {"hypothesis_id": "OIL", "scope": "SECTOR", "direction": "up", "related_symbols": ["XLE"], "verification_status": "PLAUSIBLE_NOT_VERIFIED", "candidate_stories": [{"title": "Oil rally"}]}}
    result = inherit_stock_explanations([_stock("TMO", 4)], [], market, {})[0]
    assert result["overall_explanation_status"] == "UNEXPLAINED"
    assert result["inherited_market_driver"] is None


def test_sector_scoped_market_hypothesis_requires_member_scope_and_direction():
    market = {"primary_driver": {"hypothesis_id": "OIL", "scope": "SECTOR", "direction": "up", "related_symbols": ["XLE"], "verification_status": "PLAUSIBLE_NOT_VERIFIED", "candidate_stories": [{"title": "Oil rally"}]}}
    in_scope = inherit_stock_explanations([_stock("XOM", 2)], [], market, {})[0]
    conflict = inherit_stock_explanations([_stock("COP", -2)], [], market, {})[0]
    assert in_scope["overall_explanation_status"] == "MARKET_EXPLAINED"
    assert conflict["overall_explanation_status"] == "UNEXPLAINED"
    assert conflict["inheritance_rejection_reason"] == "MARKET_INHERITANCE_DIRECTION_CONFLICT"


def test_ambiguous_sector_association_is_rejected_deterministically():
    mapping = {"sectors": {"TECHNOLOGY": ["ABC"], "SEMICONDUCTORS": ["ABC"]}, "sector_aliases": {"XLK": "TECHNOLOGY", "SMH": "SEMICONDUCTORS"}}
    result = resolve_stock_sector_association("ABC", {}, {"related_symbols": ["XLK", "SMH"]}, mapping)
    assert result["association_status"] == "AMBIGUOUS"
