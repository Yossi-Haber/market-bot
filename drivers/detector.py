"""Deterministic market/sector/stock driver hypotheses.

This module deliberately stops at hypotheses.  Verification and causal
language remain separate concerns owned by the existing news pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .model import Driver, DriverStatus, DriverType
from .cross_asset import classify_market_hypotheses
from .sector_driver import build_sector_driver_analysis, inherit_stock_explanations
from .sector_evidence import promote_sector_evidence

CONFIG_PATH = Path(__file__).with_name("config") / "sector_mappings.json"


def load_driver_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _items(report: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    if isinstance(report, list):
        return {str(item.get("symbol", "")).upper(): item for item in report if isinstance(item, dict) and item.get("symbol")}
    if not isinstance(report, dict):
        return {}
    if not key:
        return {str(k).upper(): v for k, v in report.items() if isinstance(v, dict)}
    value = report.get(key, {})
    if isinstance(value, dict):
        return {str(k).upper(): v for k, v in value.items() if isinstance(v, dict)}
    return {str(item.get("symbol", "")).upper(): item for item in value if isinstance(item, dict) and item.get("symbol")}


def _move(item: dict[str, Any]) -> float:
    return _number(item.get("change_pct", item.get("change", 0)))


def detect_driver_hypotheses(report_data: dict[str, Any], config: dict[str, Any] | None = None) -> list[Driver]:
    """Build market, sector, and stock hypotheses using only numeric evidence."""
    config = config or load_driver_config()
    thresholds = config.get("thresholds", {})
    market = report_data.get("market", {})
    core = _items(market.get("core", market) if isinstance(market, dict) else {}, "")
    macro = _items(report_data.get("macro", {}), "")
    sectors = _items(report_data.get("sectors", {}).get("all", report_data.get("sectors", {})), "")
    drivers: list[Driver] = []
    core_moves = [_move(core[symbol]) for symbol in ("SPY", "QQQ", "DIA", "IWM") if symbol in core]
    average_market_move = sum(core_moves) / len(core_moves) if core_moves else 0.0
    rates_bps = _number(macro.get("US10Y", {}).get("change_bps"))
    vix_move = _move(macro.get("VIX", {}))
    wti_move = _move(macro.get("WTI", {}))

    if average_market_move <= -abs(_number(thresholds.get("market_move_pct"), 0.75)) and rates_bps >= _number(thresholds.get("rates_change_bps"), 3):
        drivers.append(Driver("RATES_PRESSURE", DriverType.MARKET_DRIVER.value, "US_MARKET", "down", abs(average_market_move), ["SPY", "QQQ", "DIA", "IWM"], movement_evidence=[{"metric": "market_average_change_pct", "value": round(average_market_move, 4)}, {"metric": "US10Y_change_bps", "value": rates_bps}]))
    if average_market_move <= -abs(_number(thresholds.get("market_move_pct"), 0.75)) and vix_move >= _number(thresholds.get("vix_move_pct"), 8):
        drivers.append(Driver("RISK_OFF", DriverType.MARKET_DRIVER.value, "US_MARKET", "down", abs(vix_move), ["SPY", "QQQ", "DIA", "IWM"], movement_evidence=[{"metric": "market_average_change_pct", "value": round(average_market_move, 4)}, {"metric": "VIX_change_pct", "value": vix_move}]))
    if wti_move >= _number(thresholds.get("oil_move_pct"), 1) and any(_move(sectors.get(symbol, {})) >= _number(thresholds.get("sector_move_pct"), 1) for symbol in ("XLE", "ENERGY")):
        drivers.append(Driver("ENERGY_OIL", DriverType.MARKET_DRIVER.value, "US_MARKET", "up", wti_move, ["XLE", "WTI"], related_sector="ENERGY", movement_evidence=[{"metric": "WTI_change_pct", "value": wti_move}]))

    sector_candidates = sorted(((symbol, _move(item), item) for symbol, item in sectors.items()), key=lambda row: row[1], reverse=True)
    threshold = _number(thresholds.get("sector_move_pct"), 1.0)
    selected = [row for row in sector_candidates if abs(row[1]) >= threshold][:2] + [row for row in reversed(sector_candidates) if abs(row[1]) >= threshold][:2]
    seen: set[str] = set()
    mappings = config.get("sectors", {})
    for symbol, move, item in selected:
        aliases = config.get("sector_aliases", {})
        sector_name = str(aliases.get(symbol) or item.get("sector_name") or item.get("display_name") or symbol).upper().replace(" ", "_")
        if sector_name in seen:
            continue
        seen.add(sector_name)
        direction = "up" if move >= 0 else "down"
        related = list(mappings.get(sector_name, []))
        drivers.append(Driver(f"SECTOR_{sector_name}", DriverType.SECTOR_DRIVER.value, sector_name, direction, abs(move), related, related_sector=sector_name, movement_evidence=[{"metric": f"{symbol}_change_pct", "value": move}]))

    movers = report_data.get("movers", {})
    mover_items = [item for group in ("top_gainers", "top_losers") for item in movers.get(group, []) if isinstance(item, dict)] if isinstance(movers, dict) else []
    for item in sorted(mover_items, key=lambda row: abs(_move(row)), reverse=True):
        symbol = str(item.get("symbol", "")).upper()
        if symbol:
            drivers.append(Driver(f"STOCK_{symbol}", DriverType.STOCK_CATALYST.value, "STOCK", "up" if _move(item) >= 0 else "down", abs(_move(item)), [symbol], movement_evidence=[{"metric": "daily_change_pct", "value": _move(item)}]))
    return drivers


def classify_driver(driver: Driver, candidate_stories: list[dict[str, Any]] | None = None, verified_stories: list[dict[str, Any]] | None = None) -> Driver:
    """Attach evidence and classify without upgrading unverified discovery."""
    candidates = candidate_stories or []
    verified = [story for story in (verified_stories or []) if story.get("verification") == "VERIFIED" and story.get("editorial_eligible", True) is True]
    symbols = set(driver.related_symbols)
    relevant_verified = [story for story in verified if symbols.intersection({str(s).upper() for s in story.get("affected_symbols", [])})]
    driver.candidate_stories = candidates
    if relevant_verified:
        driver.verified_story = relevant_verified[0]
        driver.verification_status = DriverStatus.CONFIRMED.value
        driver.confidence_reason = "Movement evidence is supported by an independently verified relevant story."
    elif candidates:
        driver.verification_status = DriverStatus.PLAUSIBLE_NOT_VERIFIED.value
        driver.confidence_reason = "A relevant discovery candidate exists, but it is not independently verified."
    else:
        driver.verification_status = DriverStatus.NO_CATALYST_FOUND.value
        driver.confidence_reason = "Movement evidence exists, but no relevant candidate story was found."
    return driver


def build_driver_analysis(report_data: dict[str, Any]) -> dict[str, Any]:
    """Return public report-ready drivers and safe metrics."""
    hypotheses = detect_driver_hypotheses(report_data)
    candidates = report_data.get("candidate_stories", []) if isinstance(report_data.get("candidate_stories"), list) else []
    # Market evidence has its own discovery pool.  Never use mover/company
    # candidates as a fallback for broad market-driver classification.
    market_candidates = report_data.get("market_candidate_stories", []) if isinstance(report_data.get("market_candidate_stories"), list) else []
    verified = report_data.get("verified_stories", []) if isinstance(report_data.get("verified_stories"), list) else []
    drivers = []
    sector_evidence = report_data.get("sector_candidate_stories", []) if isinstance(report_data.get("sector_candidate_stories"), list) else []
    for driver in hypotheses:
        if driver.driver_type == DriverType.SECTOR_DRIVER.value:
            drivers.append(promote_sector_evidence(driver.as_dict(), sector_evidence, report_data))
        else:
            drivers.append(classify_driver(driver, candidates, verified))
    market = [driver.as_dict() for driver in drivers if isinstance(driver, Driver) and driver.driver_type == DriverType.MARKET_DRIVER.value]
    sector = [driver if isinstance(driver, dict) else driver.as_dict() for driver in drivers if (driver.get("driver_type") if isinstance(driver, dict) else driver.driver_type) == DriverType.SECTOR_DRIVER.value]
    stock = [driver.as_dict() for driver in drivers if isinstance(driver, Driver) and driver.driver_type == DriverType.STOCK_CATALYST.value]
    market_analysis = classify_market_hypotheses({**report_data, "market_date": report_data.get("market_date")}, market_candidates, report_data.get("market_verified_stories", []) if isinstance(report_data.get("market_verified_stories"), list) else [])
    if isinstance(report_data.get("market_discovery"), dict):
        market_analysis["discovery"] = report_data["market_discovery"]
    sector_analysis = build_sector_driver_analysis(report_data, market_analysis)
    # Inheritance requires evidence-bearing sector drivers.  The separate
    # cross-asset sector_analysis list contains movement hypotheses only.
    stock_explanations = inherit_stock_explanations(stock, sector, market_analysis, report_data)
    primary = market_analysis.get("primary_driver")
    primary_record = primary
    if primary_record:
        primary_record = {
            "driver_id": primary["hypothesis_id"], "driver_type": DriverType.MARKET_DRIVER.value, "scope": primary.get("scope", "US_MARKET"), "direction": primary.get("direction", "neutral"), "strength": float(primary.get("supporting_market_data", {}).get("signals", [{}])[0].get("confidence", 0) if primary.get("supporting_market_data", {}).get("signals") else 0), "related_symbols": primary.get("related_symbols", []), "movement_evidence": primary.get("supporting_market_data", {}).get("signals", []), "candidate_stories": primary.get("candidate_stories", []), "verified_story": primary.get("verified_story"), "verification_status": primary.get("verification_status", "NO_MARKET_DRIVER_FOUND"), "hypothesis_status": primary.get("hypothesis_status", "HYPOTHESIS_ONLY"), "warnings": primary.get("warnings", []), "confidence_reason": primary.get("confidence_reason", "")
        }
    elif market:
        primary_record = market[0]
    all_drivers = [item for item in [primary_record, *sector, *stock] if item]
    metrics = {"market_driver_hypotheses": len(market), "sector_driver_hypotheses": len(sector), "stock_catalyst_targets": len(stock), "drivers_confirmed": sum(driver["verification_status"] == "CONFIRMED" for driver in all_drivers), "drivers_unverified": sum(driver["verification_status"] == "PLAUSIBLE_NOT_VERIFIED" for driver in all_drivers), "drivers_unexplained": sum(driver["verification_status"] == "NO_CATALYST_FOUND" for driver in all_drivers), "market_regimes": len(market_analysis.get("market_regime", [])), "market_hypotheses": len(market_analysis.get("secondary_drivers", [])) + (1 if primary else 0)}
    return {"main_market_driver": primary_record, "sector_drivers": sector, "stock_catalysts": stock, "market_driver_analysis": market_analysis, "sector_driver_analysis": sector_analysis, "stock_explanations": stock_explanations, "metrics": metrics}
