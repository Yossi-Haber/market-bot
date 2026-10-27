"""Deterministic point-in-time market regime and signal detection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from macro_freshness import current_macro

CONFIG_PATH = Path(__file__).with_name("config") / "market_driver_thresholds.json"


def load_thresholds(path: Path = CONFIG_PATH) -> dict[str, float]:
    return {key: float(value) for key, value in json.loads(path.read_text(encoding="utf-8")).items()}


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _move(item: Any) -> float | None:
    if not isinstance(item, dict):
        return None
    return _number(item.get("change_pct", item.get("change")))


def _group(report: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    value = report.get(key, {})
    if isinstance(value, dict):
        if isinstance(value.get("all"), list):
            return {str(item.get("symbol", "")).upper(): item for item in value["all"] if isinstance(item, dict) and item.get("symbol")}
        return {str(symbol).upper(): item for symbol, item in value.items() if isinstance(item, dict)}
    if isinstance(value, list):
        return {str(item.get("symbol", "")).upper(): item for item in value if isinstance(item, dict) and item.get("symbol")}
    return {}


def _signal(signal_type: str, observed: Any, baseline: Any, magnitude: str, direction: str, confidence: float, data: dict[str, Any]) -> dict[str, Any]:
    return {"signal_type": signal_type, "observed_value": observed, "baseline": baseline, "magnitude": magnitude, "direction": direction, "confidence": round(confidence, 3), "supporting_market_data": data}


def detect_market_regimes(report_data: dict[str, Any], thresholds: dict[str, float] | None = None) -> dict[str, Any]:
    """Return observations only; no causal claim is made by this function."""
    t = {**load_thresholds(), **(thresholds or {})}
    core = _group(report_data, "core") or _group(report_data.get("market", {}), "core")
    sectors = _group(report_data, "sectors")
    macro = _group(report_data, "macro")
    index_moves = {symbol: _move(core.get(symbol)) for symbol in ("SPY", "QQQ", "DIA", "IWM")}
    valid_indices = {key: value for key, value in index_moves.items() if value is not None}
    average = sum(valid_indices.values()) / len(valid_indices) if valid_indices else 0.0
    market_date = report_data.get("market_date")
    us10y = (_number(macro.get("US10Y", {}).get("change_bps")) or 0.0) if current_macro(macro.get("US10Y"), market_date) else 0.0
    vix = (_move(macro.get("VIX")) or 0.0) if current_macro(macro.get("VIX"), market_date) else 0.0
    dxy = (_move(macro.get("DXY")) or 0.0) if current_macro(macro.get("DXY"), market_date) else 0.0
    wti = (_move(macro.get("WTI")) or 0.0) if current_macro(macro.get("WTI"), market_date) else 0.0
    gold = (_move(macro.get("GOLD")) or 0.0) if current_macro(macro.get("GOLD"), market_date) else 0.0
    sector_moves = {symbol: _move(item) for symbol, item in sectors.items()}
    valid_sectors = {key: value for key, value in sector_moves.items() if value is not None}
    up = sum(value > 0 for value in valid_sectors.values())
    down = sum(value < 0 for value in valid_sectors.values())
    regimes: list[str] = []
    signals: list[dict[str, Any]] = []
    if average <= -t["index_move_material_pct"]:
        regimes.append("BROAD_RISK_OFF")
        signals.append(_signal("BROAD_RISK_OFF", round(average, 4), 0, "STRONG" if average <= -1.5 else "MATERIAL", "down", min(1.0, abs(average) / 2), {**valid_indices, "average_index_change_pct": round(average, 4)}))
    elif average >= t["index_move_material_pct"]:
        regimes.append("BROAD_RISK_ON")
        signals.append(_signal("BROAD_RISK_ON", round(average, 4), 0, "STRONG" if average >= 1.5 else "MATERIAL", "up", min(1.0, average / 2), {**valid_indices, "average_index_change_pct": round(average, 4)}))
    if us10y >= t["us10y_move_material_bp"]:
        regimes.extend(["RISING_YIELDS", "BOND_SELLOFF"])
        signals.append(_signal("RISING_YIELDS", f"+{us10y:g}bp", 0, "STRONG" if us10y >= 14 else "MATERIAL", "up", min(1.0, us10y / 20), {"US10Y_change_bp": us10y, **valid_indices}))
    elif us10y <= -t["us10y_move_material_bp"]:
        regimes.append("FALLING_YIELDS")
        signals.append(_signal("FALLING_YIELDS", f"{us10y:g}bp", 0, "MATERIAL", "down", min(1.0, abs(us10y) / 20), {"US10Y_change_bp": us10y, **valid_indices}))
    if (index_moves.get("QQQ") or 0) <= -t["index_move_material_pct"] and (sector_moves.get("XLK") or 0) <= -t["sector_move_material_pct"]:
        regimes.append("TECH_SELL_OFF")
    if (index_moves.get("IWM") or 0) - (index_moves.get("SPY") or 0) <= -t["index_move_material_pct"]:
        regimes.append("SMALL_CAP_UNDERPERFORMANCE")
    if vix >= t["vix_move_material_pct"]:
        regimes.append("VOLATILITY_SPIKE")
        signals.append(_signal("VOLATILITY_SPIKE", f"+{vix:g}%", 0, "STRONG" if vix >= 12 else "MATERIAL", "up", min(1.0, vix / 20), {"VIX_change_pct": vix, "average_index_change_pct": round(average, 4)}))
    if dxy >= t["dxy_move_material_pct"]:
        regimes.append("USD_RALLY")
        signals.append(_signal("USD_RALLY", f"+{dxy:g}%", 0, "MATERIAL", "up", min(1.0, dxy / 1.0), {"DXY_change_pct": dxy}))
    elif dxy <= -t["dxy_move_material_pct"]:
        regimes.append("USD_SELL_OFF")
    if wti >= t["wti_move_material_pct"]:
        regimes.append("OIL_SHOCK_UP")
        signals.append(_signal("OIL_SHOCK_UP", f"+{wti:g}%", 0, "MATERIAL", "up", min(1.0, wti / 4), {"WTI_change_pct": wti}))
    elif wti <= -t["wti_move_material_pct"]:
        regimes.append("OIL_SHOCK_DOWN")
    if gold >= t["gold_move_material_pct"]:
        regimes.append("GOLD_RISK_HEDGE")
    if down >= t["breadth_weak_count"]:
        regimes.append("MARKET_BREADTH_WEAKNESS")
    if up >= t["breadth_strong_count"]:
        regimes.append("MARKET_BREADTH_STRENGTH")
    if valid_sectors and max(valid_sectors.values()) - min(valid_sectors.values()) >= t["sector_dispersion_material_pct"]:
        regimes.append("SECTOR_DISPERSION")
    return {"regimes": list(dict.fromkeys(regimes)), "signals": signals, "breadth": {"total": len(valid_sectors), "up": up, "down": down, "unchanged": len(valid_sectors) - up - down}, "index_moves": valid_indices, "sector_moves": valid_sectors, "macro_moves": {"US10Y_change_bp": us10y, "VIX_change_pct": vix, "DXY_change_pct": dxy, "WTI_change_pct": wti, "GOLD_change_pct": gold}}
