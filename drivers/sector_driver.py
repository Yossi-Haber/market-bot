"""Sector-driver priors and inheritance, separate from causal verification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .market_regime import detect_market_regimes
from .market_regime import load_thresholds

MAPPINGS_PATH = Path(__file__).with_name("config") / "cross_asset_sector_mappings.json"
LEGACY_MAPPING_PATH = Path(__file__).with_name("config") / "sector_mappings.json"


def build_sector_driver_analysis(report_data: dict[str, Any], market_analysis: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    regime = detect_market_regimes(report_data)
    market_analysis = market_analysis or {}
    mappings = json.loads(MAPPINGS_PATH.read_text(encoding="utf-8"))
    sectors = regime.get("sector_moves", {})
    materiality = load_thresholds().get("sector_move_material_pct", 1.5)
    result = []
    for signal in regime.get("regimes", []):
        mapping = mappings.get(signal, {})
        for direction, symbols in mapping.items():
            for symbol in symbols:
                move = sectors.get(symbol)
                if move is None:
                    continue
                expected_positive = direction == "positive"
                direction_ok = move > 0 if expected_positive else move < 0
                warnings: list[str] = []
                if not direction_ok:
                    warnings.append("SECTOR_DRIVER_DIRECTION_CONFLICT")
                elif abs(move) < materiality:
                    warnings.append("SECTOR_DRIVER_BELOW_MATERIALITY_THRESHOLD")
                # A sector relationship is a prior, not external causal
                # evidence.  It is therefore never inheritable by itself.
                result.append({
                    "driver_id": f"SECTOR_{signal}_{symbol}",
                    "driver_type": "SECTOR_DRIVER",
                    "sector": symbol,
                    "direction": "up" if move > 0 else "down",
                    "observed_move_pct": move,
                    "expected_direction": "up" if expected_positive else "down",
                    "inherited_market_driver": market_analysis.get("primary_driver") if direction_ok else None,
                    "verification_status": "NO_MARKET_DRIVER_FOUND",
                    "hypothesis_status": "HYPOTHESIS_ONLY",
                    "eligible_for_inheritance": False,
                    "materiality_threshold_pct": materiality,
                    "warnings": warnings,
                    "confidence_reason": "Relationship prior plus observed sector move; not causal proof.",
                })
    return result


def inherit_stock_explanations(stock_drivers: list[dict[str, Any]], sector_drivers: list[dict[str, Any]], market_analysis: dict[str, Any], report_data: dict[str, Any]) -> list[dict[str, Any]]:
    legacy = json.loads(LEGACY_MAPPING_PATH.read_text(encoding="utf-8"))
    aliases = legacy.get("sector_aliases", {})
    broad = market_analysis.get("primary_driver")
    broad_status = (broad or {}).get("verification_status")
    broad_evidence_eligible = bool(
        broad
        and broad_status in {"CONFIRMED", "PLAUSIBLE_NOT_VERIFIED"}
        and broad.get("hypothesis_status") != "HYPOTHESIS_ONLY"
        and broad.get("candidate_stories")
    )
    result = []
    for driver in stock_drivers:
        inheritance_warning = None
        broad_scope_eligible = False
        broad_scope_association = None
        broad_scope = str((broad or {}).get("scope", "US_MARKET")).upper()
        status = driver.get("verification_status")
        related_symbols = {str(value).upper() for value in driver.get("related_symbols", [])}
        relevant_candidates = [item for item in driver.get("candidate_stories", []) if isinstance(item, dict) and related_symbols.intersection(str(value).upper() for value in item.get("affected_symbols", [])) and (item.get("causal_eligible") is True or item.get("verification") == "VERIFIED")]
        if status in {"CONFIRMED", "PLAUSIBLE_NOT_VERIFIED"} and relevant_candidates:
            explanation = "STOCK_SPECIFIC"
            inherited_sector = None
            inherited_market = None
        else:
            symbol = str((driver.get("related_symbols") or [""])[0]).upper()
            move = (driver.get("movement_evidence") or [{}])[0].get("value")
            stock_direction = "up" if _as_float(move) > 0 else "down" if _as_float(move) < 0 else None
            inherited_sector = None
            sector_association = None
            last_sector_rejection = "NO_SECTOR_DRIVER"
            matched_sector_rejection = None
            for candidate in sector_drivers:
                metadata_index = report_data.get("stock_metadata", {})
                metadata = metadata_index.get(symbol, {}) if isinstance(metadata_index, dict) else {}
                association = resolve_stock_sector_association(symbol, metadata, candidate, legacy)
                reason = _sector_inheritance_rejection(candidate, association, stock_direction)
                if reason is None:
                    inherited_sector = candidate
                    sector_association = association
                    break
                last_sector_rejection = reason
                if association["association_status"] in {"DIRECT_SECTOR_MATCH", "SUPPORTED_THEME_MATCH"} and matched_sector_rejection is None:
                    matched_sector_rejection = reason
            if not inherited_sector and matched_sector_rejection:
                last_sector_rejection = matched_sector_rejection
            market_direction = broad.get("direction") if broad else None
            if broad_scope in {"US_MARKET", "MARKET"}:
                broad_scope_eligible = True
            else:
                metadata_index = report_data.get("stock_metadata", {})
                metadata = metadata_index.get(symbol, {}) if isinstance(metadata_index, dict) else {}
                broad_scope_association = resolve_stock_sector_association(symbol, metadata, broad or {}, legacy)
                broad_scope_eligible = broad_scope_association["association_status"] in {"DIRECT_SECTOR_MATCH", "SUPPORTED_THEME_MATCH"}
            inheritance_warning = None
            if broad_evidence_eligible and broad_scope_eligible and market_direction and stock_direction and market_direction != stock_direction:
                inheritance_warning = "MARKET_INHERITANCE_DIRECTION_CONFLICT"
            inherited_market = broad if broad_evidence_eligible and broad_scope_eligible and not inheritance_warning else None
            if inherited_sector and inherited_market:
                explanation = "MULTI_FACTOR"
            elif inherited_sector:
                explanation = "SECTOR_EXPLAINED"
            elif inherited_market:
                explanation = "MARKET_EXPLAINED"
            else:
                explanation = "UNEXPLAINED"
        record = {"symbol": (driver.get("related_symbols") or [None])[0], "move_pct": (driver.get("movement_evidence") or [{}])[0].get("value"), "stock_specific_catalyst_status": "FOUND" if relevant_candidates else "NOT_FOUND", "overall_explanation_status": explanation, "inherited_sector_driver": inherited_sector, "inherited_market_driver": inherited_market, "driver_id": driver.get("driver_id")}
        if status not in {"CONFIRMED", "PLAUSIBLE_NOT_VERIFIED"} or not relevant_candidates:
            record["sector_association_status"] = (sector_association or {}).get("association_status", "NO_MATCH") if inherited_sector else "NO_MATCH"
            record["sector_association_reason"] = (sector_association or {}).get("association_reason") if inherited_sector else last_sector_rejection
            record["matched_sector"] = (sector_association or {}).get("matched_sector") if inherited_sector else None
            record["sector_direction_check"] = "PASS" if inherited_sector else ("FAIL" if last_sector_rejection == "DIRECTION_CONFLICT" else "NOT_EVALUATED")
            record["sector_causal_evidence_status"] = "PASS" if inherited_sector else ("FAIL" if last_sector_rejection in {"NO_CAUSAL_SECTOR_EVIDENCE", "HYPOTHESIS_ONLY", "EVIDENCE_NOT_SECTOR_SCOPED"} else "NOT_EVALUATED")
            record["sector_inheritance_rejection_reason"] = None if inherited_sector else last_sector_rejection
        if inheritance_warning:
            record["inheritance_rejection_reason"] = inheritance_warning
            record["warnings"] = [inheritance_warning]
        elif broad_evidence_eligible and broad and not (status in {"CONFIRMED", "PLAUSIBLE_NOT_VERIFIED"} and relevant_candidates) and broad_scope not in {"US_MARKET", "MARKET"} and not broad_scope_eligible:
            record["inheritance_rejection_reason"] = "MARKET_DRIVER_SCOPE_MISMATCH"
            record["market_scope_association"] = (broad_scope_association or {}).get("association_reason", "NO_EXPLICIT_SECTOR_OR_THEME_MAPPING")
        result.append(record)
    return result


def resolve_stock_sector_association(symbol: str, stock_metadata: dict[str, Any], sector_driver: dict[str, Any], sector_mapping: dict[str, Any]) -> dict[str, str]:
    """Resolve a stock-to-driver sector relationship using explicit config only."""
    symbol = str(symbol).upper()
    memberships: set[str] = set()
    configured_members = sector_mapping.get("sectors", {})
    for sector_name, members in configured_members.items():
        if symbol in {str(member).upper() for member in members}:
            memberships.add(str(sector_name).upper())
    metadata_sector = stock_metadata.get("sector") or stock_metadata.get("sectors") or []
    if isinstance(metadata_sector, str):
        metadata_sector = [metadata_sector]
    memberships.update(str(item).upper().replace(" ", "_") for item in metadata_sector)

    aliases = sector_mapping.get("sector_aliases", {})
    driver_sectors: set[str] = set()
    for value in (sector_driver.get("related_sector"), sector_driver.get("sector"), sector_driver.get("scope")):
        if value:
            canonical = str(value).upper().replace(" ", "_")
            driver_sectors.add(str(aliases.get(canonical, canonical)).upper().replace(" ", "_"))
    for value in sector_driver.get("related_symbols", []):
        canonical = str(value).upper()
        if canonical in aliases:
            driver_sectors.add(str(aliases[canonical]).upper().replace(" ", "_"))
    driver_sectors.discard("")
    matches = sorted(memberships.intersection(driver_sectors))
    if len(matches) == 1:
        return {"association_status": "DIRECT_SECTOR_MATCH", "association_reason": f"EXPLICIT_MEMBERSHIP:{matches[0]}", "matched_sector": matches[0]}
    if len(matches) > 1:
        return {"association_status": "AMBIGUOUS", "association_reason": "MULTIPLE_DIRECT_SECTORS", "matched_sector": ""}

    theme_mapping = sector_mapping.get("sector_theme_associations", {})
    supported_by: dict[str, str] = {}
    for driver_sector in driver_sectors:
        themes = theme_mapping.get(driver_sector, {})
        for stock_sector in set(memberships).intersection(str(key).upper() for key in themes):
            supported_by[stock_sector] = driver_sector
    supported = sorted(supported_by)
    if len(supported) > 1:
        return {"association_status": "AMBIGUOUS", "association_reason": "MULTIPLE_SUPPORTED_THEMES", "matched_sector": ""}
    if len(supported) == 1:
        return {"association_status": "SUPPORTED_THEME_MATCH", "association_reason": f"CONFIGURED_THEME:{supported_by[supported[0]]}->{supported[0]}", "matched_sector": supported[0]}
    return {"association_status": "NO_MATCH", "association_reason": "NO_EXPLICIT_SECTOR_OR_THEME_MAPPING", "matched_sector": ""}


def _sector_inheritance_rejection(driver: dict[str, Any], association: dict[str, str], stock_direction: str | None) -> str | None:
    """Return the first failed inheritance prerequisite, if any."""
    if association["association_status"] not in {"DIRECT_SECTOR_MATCH", "SUPPORTED_THEME_MATCH"}:
        return association["association_reason"]
    if driver.get("hypothesis_status") == "HYPOTHESIS_ONLY" or driver.get("eligible_for_inheritance") is False:
        return "HYPOTHESIS_ONLY"
    if driver.get("verification_status") not in {"CONFIRMED", "PLAUSIBLE_NOT_VERIFIED"}:
        return "NO_CAUSAL_SECTOR_EVIDENCE"
    try:
        from .market_regime import load_thresholds
        materiality = float(load_thresholds().get("sector_move_material_pct", 1.5))
        sector_move = abs(float(driver.get("strength", driver.get("observed_move_pct", 0)) or 0))
    except (TypeError, ValueError):
        return "INVALID_SECTOR_MOVEMENT"
    if sector_move < materiality:
        return "BELOW_EXISTING_MATERIALITY_THRESHOLD"
    sector_direction = str(driver.get("direction", "")).lower()
    if not stock_direction or stock_direction != sector_direction:
        return "DIRECTION_CONFLICT"
    candidates = driver.get("candidate_stories", [])
    eligible = [item for item in candidates if isinstance(item, dict) and item.get("causal_eligible") is True and item.get("causal_gate") != "FAIL" and item.get("event_time_relation") != "FUTURE_EVENT"]
    if not eligible:
        return "NO_CAUSAL_SECTOR_EVIDENCE"
    mapping = json.loads(LEGACY_MAPPING_PATH.read_text(encoding="utf-8"))
    sector = str(association.get("matched_sector") or "").upper()
    terms = mapping.get("sector_evidence_terms", {}).get(sector, [])
    if association["association_status"] == "SUPPORTED_THEME_MATCH":
        terms = mapping.get("sector_theme_associations", {}).get(str(driver.get("related_sector") or driver.get("scope") or "").upper(), {}).get(sector, {}).get("evidence_terms", terms)
    if not terms:
        return "NO_CONFIGURED_SECTOR_EVIDENCE_TERMS"
    text = " ".join(str(item.get(key) or "") for item in eligible for key in ("title", "canonical_event_title", "summary", "description")).casefold()
    if not any(str(term).casefold() in text for term in terms):
        return "EVIDENCE_NOT_SECTOR_SCOPED"
    return None


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
