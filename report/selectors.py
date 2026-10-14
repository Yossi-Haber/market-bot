"""Deterministic selectors and validators for daily report data."""

from __future__ import annotations

import math
from typing import Any

CORE_MOVE_NOTABLE_PCT = 1.0
SECTOR_MOVE_NOTABLE_PCT = 1.5
STOCK_MOVE_NOTABLE_PCT = 3.0
VIX_MOVE_NOTABLE_PCT = 5.0
US10Y_MOVE_NOTABLE_BPS = 5.0
DXY_MOVE_NOTABLE_PCT = 0.5
GOLD_MOVE_NOTABLE_PCT = 1.0
WTI_MOVE_NOTABLE_PCT = 1.5
VIX_FLAT_THRESHOLD_PCT = 0.25
YIELD_FLAT_THRESHOLD_BPS = 1.0


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def valid_market_item(item: Any) -> bool:
    return isinstance(item, dict) and item.get("verified") is True and finite(item.get("close")) and finite(item.get("change")) and finite(item.get("change_pct"))


def compact_market_item(item: dict[str, Any], *, representation: str | None = None) -> dict[str, Any]:
    result = {key: item.get(key) for key in ("symbol", "display_name", "close", "change", "change_pct")}
    if representation:
        result["representation"] = representation
    return result


def build_indices(market: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    items: list[dict[str, Any]] = []
    invalid: list[str] = []
    for symbol in ("SPY", "QQQ", "DIA", "IWM"):
        item = market.get("core", {}).get(symbol)
        if valid_market_item(item):
            items.append(compact_market_item(item, representation="ETF_PROXY"))
        else:
            invalid.append(symbol)
    return items, invalid


def build_market_overview(indices: list[dict[str, Any]], sectors: list[dict[str, Any]]) -> dict[str, Any]:
    changes = [item["change_pct"] for item in indices]
    positive = sum(value > 0 for value in changes)
    negative = sum(value < 0 for value in changes)
    average = sum(changes) / len(changes) if changes else None
    sector_positive = sum(item["change_pct"] > 0 for item in sectors)
    sector_breadth = sector_positive / len(sectors) if sectors else 0.0
    direction = classify_direction(positive, negative, average, sector_breadth)
    return {
        "representation": "ETF_PROXY",
        "advancing_core_count": positive,
        "declining_core_count": negative,
        "average_core_change_pct": average,
        "best_core_index": max(indices, key=lambda item: item["change_pct"])["symbol"] if indices else None,
        "worst_core_index": min(indices, key=lambda item: item["change_pct"])["symbol"] if indices else None,
        "market_direction": direction,
        "direction_rule": {
            "positive_core_required_for_broad_positive": 3,
            "negative_core_required_for_broad_negative": 3,
            "average_positive_threshold_pct": 0.25,
            "average_negative_threshold_pct": -0.25,
            "sector_breadth": sector_breadth,
        },
    }


def classify_direction(positive: int, negative: int, average: float | None, sector_breadth: float) -> str:
    if average is None:
        return "MIXED"
    if positive >= 3 and average >= 0.25 and sector_breadth >= 0.5:
        return "BROADLY_POSITIVE"
    if negative >= 3 and average <= -0.25 and sector_breadth <= 0.5:
        return "BROADLY_NEGATIVE"
    if average > 0 and positive >= 2:
        return "MIXED_POSITIVE"
    if average < 0 and negative >= 2:
        return "MIXED_NEGATIVE"
    return "MIXED"


def build_sectors(market: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    values = [compact_market_item(item) for item in market.get("sectors", {}).values() if valid_market_item(item)]
    invalid = [symbol for symbol, item in market.get("sectors", {}).items() if not valid_market_item(item)]
    values.sort(key=lambda item: item["change_pct"], reverse=True)
    positive = sum(item["change_pct"] > 0 for item in values)
    negative = sum(item["change_pct"] < 0 for item in values)
    fraction = positive / len(values) if values else 0.0
    return {"all": values, "top": values[:3], "bottom": list(reversed(values[-3:])), "positive_count": positive, "negative_count": negative, "valid_count": len(values), "breadth": {"fraction": fraction, "percentage": fraction * 100}}, invalid


def build_macro(market: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int], list[str]]:
    macro: dict[str, Any] = {}
    invalid: list[str] = []
    current = lagging = stale = 0
    for symbol in ("VIX", "GOLD", "WTI", "DXY", "US10Y"):
        item = market.get("macro", {}).get(symbol)
        if not isinstance(item, dict) or item.get("verified") is not True or not finite(item.get("close")):
            invalid.append(symbol)
            continue
        selected = {key: item.get(key) for key in ("symbol", "display_name", "close", "change", "change_pct", "change_bps", "source", "latest_observation_date", "latest_bar_datetime", "freshness", "warning") if key in item}
        if symbol == "VIX":
            pct = item.get("change_pct")
            selected["interpretation_flag"] = "VIX_FLAT" if not finite(pct) or abs(pct) < VIX_FLAT_THRESHOLD_PCT else "VIX_UP" if pct > 0 else "VIX_DOWN"
        if symbol == "US10Y":
            bps = item.get("change_bps")
            selected["yield_flag"] = "YIELD_FLAT" if not finite(bps) or abs(bps) < YIELD_FLAT_THRESHOLD_BPS else "YIELD_UP" if bps > 0 else "YIELD_DOWN"
        macro[symbol] = selected
        freshness = item.get("freshness")
        if freshness == "current":
            current += 1
        elif freshness == "lagging":
            lagging += 1
        if item.get("warning") == "STALE_SOURCE_DATA" or freshness == "stale":
            stale += 1
    return macro, {"current_count": current, "lagging_count": lagging, "stale_count": stale}, invalid


def validate_movers(movers: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], list[str]]:
    gainers = [item for item in movers.get("top_gainers", []) if isinstance(item, dict)]
    losers = [item for item in movers.get("top_losers", []) if isinstance(item, dict)]
    warnings: list[str] = []
    symbols = [item.get("symbol") for item in gainers + losers]
    duplicates = sorted({symbol for symbol in symbols if symbols.count(symbol) > 1 and symbol})
    if duplicates:
        warnings.append("DUPLICATE_MOVER_SYMBOLS:" + ",".join(duplicates))
    if any(gainers[i].get("change_pct", 0) < gainers[i + 1].get("change_pct", 0) for i in range(len(gainers) - 1)):
        warnings.append("GAINER_ORDER_CORRECTED")
        gainers.sort(key=lambda item: item.get("change_pct", float("-inf")), reverse=True)
    if any(losers[i].get("change_pct", 0) > losers[i + 1].get("change_pct", 0) for i in range(len(losers) - 1)):
        warnings.append("LOSER_ORDER_CORRECTED")
        losers.sort(key=lambda item: item.get("change_pct", float("inf")))
    return gainers, losers, warnings, duplicates


def notable_moves(indices: list[dict[str, Any]], sectors: dict[str, Any], macro: dict[str, Any], movers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    notable: list[dict[str, Any]] = []
    for item in indices:
        if abs(item["change_pct"]) >= CORE_MOVE_NOTABLE_PCT:
            notable.append({"type": "CORE_ETF", "symbol": item["symbol"], "value": item["change_pct"], "threshold": CORE_MOVE_NOTABLE_PCT})
    for item in sectors.get("all", []):
        if abs(item["change_pct"]) >= SECTOR_MOVE_NOTABLE_PCT:
            notable.append({"type": "SECTOR_ETF", "symbol": item["symbol"], "value": item["change_pct"], "threshold": SECTOR_MOVE_NOTABLE_PCT})
    for item in movers:
        if finite(item.get("change_pct")) and abs(item["change_pct"]) >= STOCK_MOVE_NOTABLE_PCT:
            notable.append({"type": "STOCK_MOVER", "symbol": item["symbol"], "value": item["change_pct"], "threshold": STOCK_MOVE_NOTABLE_PCT})
    thresholds = {"VIX": VIX_MOVE_NOTABLE_PCT, "US10Y": US10Y_MOVE_NOTABLE_BPS, "DXY": DXY_MOVE_NOTABLE_PCT, "GOLD": GOLD_MOVE_NOTABLE_PCT, "WTI": WTI_MOVE_NOTABLE_PCT}
    for symbol, threshold in thresholds.items():
        item = macro.get(symbol)
        if not item:
            continue
        value = item.get("change_bps") if symbol == "US10Y" else item.get("change_pct") if item else None
        if finite(value) and abs(value) >= threshold:
            notable.append({"type": "MACRO", "symbol": symbol, "value": value, "threshold": threshold, "unit": "bps" if symbol == "US10Y" else "pct"})
    return notable


def highlights(indices: list[dict[str, Any]], sectors: dict[str, Any], movers: list[dict[str, Any]], macro: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if indices:
        result.extend([{"type": "STRONGEST_CORE_ETF", "symbol": max(indices, key=lambda x: x["change_pct"])["symbol"], "value": max(indices, key=lambda x: x["change_pct"])["change_pct"]}, {"type": "WEAKEST_CORE_ETF", "symbol": min(indices, key=lambda x: x["change_pct"])["symbol"], "value": min(indices, key=lambda x: x["change_pct"])["change_pct"]}])
    if sectors.get("all"):
        result.extend([{"type": "TOP_SECTOR", "symbol": sectors["all"][0]["symbol"], "value": sectors["all"][0]["change_pct"]}, {"type": "BOTTOM_SECTOR", "symbol": sectors["all"][-1]["symbol"], "value": sectors["all"][-1]["change_pct"]}])
    if movers:
        result.extend([{"type": "LARGEST_STOCK_GAINER", "symbol": movers[0]["symbol"], "value": movers[0].get("change_pct")}, {"type": "LARGEST_STOCK_LOSER", "symbol": movers[-1]["symbol"], "value": movers[-1].get("change_pct")}])
    macro_values = [(symbol, abs(item.get("change_bps")) if symbol == "US10Y" else abs(item.get("change_pct"))) for symbol, item in macro.items() if finite(item.get("change_bps" if symbol == "US10Y" else "change_pct"))]
    if macro_values:
        symbol, value = max(macro_values, key=lambda pair: pair[1])
        result.append({"type": "LARGEST_MACRO_MOVE", "symbol": symbol, "value": macro[symbol].get("change_bps") if symbol == "US10Y" else macro[symbol].get("change_pct")})
    return result
