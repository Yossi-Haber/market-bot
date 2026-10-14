"""Build a restricted, public-safe payload for a future narrative model."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from render.formatting import format_bps, format_pct, format_price, format_signed_number

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"
MACRO_DISPLAY = {
    "VIX": {"display_symbol": "VIX", "display_name_he": "מדד הפחד VIX"},
    "GOLD": {"display_symbol": "GOLD", "display_name_he": "זהב"},
    "WTI": {"display_symbol": "WTI", "display_name_he": "נפט WTI"},
    "DXY": {"display_symbol": "DXY", "display_name_he": "מדד הדולר DXY"},
    "US10Y": {"display_symbol": "US10Y", "display_name_he": "תשואת אג\"ח ארה\"ב ל־10 שנים"},
}


class LLMInputNotReadyError(RuntimeError):
    """Raised when the report is not safe to pass to a future LLM."""


def load_public_payload(path: Path | None = None) -> dict[str, Any]:
    """Load report JSON and build only fields suitable for public narration."""
    input_path = path or OUTPUT_DIR / "daily_report_data.json"
    data = json.loads(input_path.read_text(encoding="utf-8"))
    return build_llm_payload(data)


def build_llm_payload(report_data: dict[str, Any]) -> dict[str, Any]:
    """Return a defense-in-depth safe subset, never including leads or debug data."""
    readiness = report_data.get("publication_readiness", {})
    if readiness.get("status") == "NOT_READY":
        raise LLMInputNotReadyError("REPORT_NOT_READY")

    macro = {}
    for symbol in ("VIX", "GOLD", "WTI", "DXY", "US10Y"):
        item = report_data.get("macro", {}).get(symbol)
        if not isinstance(item, dict):
            continue
        display = MACRO_DISPLAY[symbol]
        allowed = {"symbol": display["display_symbol"], **display}
        allowed.update({
            key: item[key]
            for key in ("source", "latest_observation_date", "latest_bar_datetime", "freshness", "warning", "interpretation_flag", "yield_flag")
            if key in item
        })
        if symbol == "US10Y":
            allowed["value_display"] = f"{format_price(item.get('close'))}%"
            allowed["change_bps_display"] = format_bps(item.get("change_bps"))
        else:
            allowed["close_display"] = format_price(item.get("close"), thousands_separator=True)
            allowed["change_display"] = format_signed_number(item.get("change"))
            allowed["change_pct_display"] = format_pct(item.get("change_pct"))
        macro[symbol] = allowed

    safe_stories = []
    for story in report_data.get("verified_stories", []):
        if not isinstance(story, dict):
            continue
        if story.get("verification") == "VERIFIED":
            if story.get("editorial_eligible") is not True:
                continue
            source_name = story.get("primary_source_name") or story.get("source_name")
            safe_stories.append({
                key: story[key]
                for key in ("title", "summary", "category", "affected_symbols", "published_at", "verification", "editorial_eligible", "primary_source_name", "primary_source_domain", "primary_source_type", "supporting_sources_count")
                if key in story
            } | ({"source_display": source_name} if source_name else {}))
            continue
        status = story.get("catalyst_status") or story.get("status")
        quality = story.get("evidence_quality")
        if status and not (status == "CONFIRMED" or (status == "POSSIBLE" and quality == "STRONG")):
            continue
        safe_stories.append({
            key: story[key]
            for key in ("symbol", "type", "headline", "primary_source", "event_datetime", "evidence_quality")
            if key in story
        })

    driver_payload = safe_driver_payload(report_data)
    sectors_data = report_data.get("sectors", {})
    total = sectors_data.get("valid_count")
    up = sectors_data.get("positive_count")
    down = sectors_data.get("negative_count")
    unchanged = sectors_data.get("unchanged_count")
    if unchanged is None and all(isinstance(value, (int, float)) for value in (total, up, down)):
        unchanged = total - up - down
    sector_breadth = {
        key: value for key, value in (("total", total), ("up", up), ("down", down), ("unchanged", unchanged))
        if isinstance(value, (int, float))
    }
    sector_breadth_display = ""
    if {"total", "up", "down"}.issubset(sector_breadth):
        sector_breadth_display = f"{sector_breadth['down']} מתוך {sector_breadth['total']} סקטורים ירדו; {sector_breadth['up']} עלו"
    return {
        "market_date": report_data.get("market_date"),
        "market_direction": report_data.get("market_overview", {}).get("market_direction"),
        "indices": [safe_market_item(item) for item in report_data.get("indices", [])],
        "sectors": {
            "all": [safe_market_item(item) for item in report_data.get("sectors", {}).get("all", [])],
            "top": [safe_market_item(item) for item in report_data.get("sectors", {}).get("top", [])],
            "bottom": [safe_market_item(item) for item in report_data.get("sectors", {}).get("bottom", [])],
            "positive_count": report_data.get("sectors", {}).get("positive_count"),
            "negative_count": report_data.get("sectors", {}).get("negative_count"),
            "valid_count": report_data.get("sectors", {}).get("valid_count"),
            "breadth": report_data.get("sectors", {}).get("breadth", {}),
        },
        "sector_breadth": sector_breadth,
        "sector_breadth_display": sector_breadth_display,
        "macro": macro,
        "movers": {
            "top_gainers": [safe_mover(item) for item in report_data.get("movers", {}).get("top_gainers", [])],
            "top_losers": [safe_mover(item) for item in report_data.get("movers", {}).get("top_losers", [])],
        },
        "verified_stories": safe_stories,
        "notable_moves": [safe_notable_move(item, report_data) for item in report_data.get("notable_moves", []) if isinstance(item, dict)],
        "publication_readiness": {
            "status": readiness.get("status"),
            "reasons": list(readiness.get("reasons", [])),
        },
        **driver_payload,
    }


def safe_driver_payload(report_data: dict[str, Any]) -> dict[str, Any]:
    """Expose driver certainty explicitly; hypotheses never become facts."""
    def safe_context_driver(item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        return {key: item[key] for key in ("driver_id", "hypothesis_id", "event_type", "scope", "direction", "related_symbols", "verification_status", "supporting_market_data", "confidence_reason", "observed_move_pct", "sector", "inherited_market_driver") if key in item}
    def safe_driver(item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        return {key: item.get(key) for key in ("driver_id", "driver_type", "scope", "direction", "strength", "related_symbols", "related_sector", "movement_evidence", "verification_status", "confidence_reason", "selected_event", "event_type", "subject_gate", "causal_gate", "causal_reason", "event_time_relation") if key in item}

    main = safe_driver(report_data.get("main_market_driver"))
    sectors = [safe_driver(item) for item in report_data.get("sector_drivers", []) if safe_driver(item)]
    stocks = [safe_driver(item) for item in report_data.get("stock_catalysts", []) if safe_driver(item)]
    all_drivers = [item for item in [main, *sectors, *stocks] if item]
    confirmed = [item for item in all_drivers if item.get("verification_status") == "CONFIRMED"]
    plausible = [item for item in all_drivers if item.get("verification_status") == "PLAUSIBLE_NOT_VERIFIED"]
    unexplained = [item for item in all_drivers if item.get("verification_status") == "NO_CATALYST_FOUND"]
    return {
        "driver_state": {
            "verified_driver": main if main and main.get("verification_status") == "CONFIRMED" else None,
            "plausible_driver": main if main and main.get("verification_status") == "PLAUSIBLE_NOT_VERIFIED" else None,
            "unexplained_move": main if main and main.get("verification_status") == "NO_CATALYST_FOUND" else None,
        },
        "main_market_driver": main,
        "sector_drivers": sectors,
        "stock_catalysts": stocks,
        "confirmed_drivers": confirmed,
        "plausible_drivers": plausible,
        "unexplained_moves": unexplained,
        "market_context": {
            "primary_market_driver": safe_context_driver(report_data.get("market_driver_analysis", {}).get("primary_driver") or main),
            "secondary_market_drivers": [safe_context_driver(item) for item in report_data.get("market_driver_analysis", {}).get("secondary_drivers", []) if safe_context_driver(item)],
            "market_regime": report_data.get("market_driver_analysis", {}).get("market_regime", []),
            "cross_asset_signals": report_data.get("market_driver_analysis", {}).get("cross_asset_signals", []),
            "breadth": report_data.get("market_driver_analysis", {}).get("breadth", report_data.get("sector_breadth", {})),
        },
        "sector_context": {
            "strongest_sectors": report_data.get("sectors", {}).get("top", []),
            "weakest_sectors": report_data.get("sectors", {}).get("bottom", []),
            "sector_drivers": [safe_context_driver(item) for item in report_data.get("sector_driver_analysis", sectors) if safe_context_driver(item)],
        },
        "stock_context": [
            {
                "symbol": item.get("symbol"),
                "stock_specific_catalyst": item.get("stock_specific_catalyst_status"),
                "inherited_market_driver": item.get("inherited_market_driver"),
                "inherited_sector_driver": item.get("inherited_sector_driver"),
                "explanation_status": item.get("overall_explanation_status", "UNEXPLAINED"),
            }
            for item in report_data.get("stock_explanations", []) if isinstance(item, dict)
        ],
    }


def safe_market_item(item: dict[str, Any]) -> dict[str, Any]:
    """Keep only factual public market fields."""
    result = {key: item[key] for key in ("symbol", "display_name", "representation") if key in item}
    result.update({
        "close_display": format_price(item.get("close"), thousands_separator=True),
        "change_display": format_signed_number(item.get("change")),
        "change_pct_display": format_pct(item.get("change_pct")),
    })
    return result


def safe_mover(item: dict[str, Any]) -> dict[str, Any]:
    """Exclude embedded catalyst/debug fields from mover records."""
    result = {key: item[key] for key in ("symbol", "company_name") if key in item}
    result.update({
        "close_display": format_price(item.get("close"), thousands_separator=True),
        "change_pct_display": format_pct(item.get("change_pct")),
    })
    return result


def safe_notable_move(item: dict[str, Any], report_data: dict[str, Any]) -> dict[str, Any]:
    """Expose notable moves with display values rather than raw floating-point data."""
    result = {key: item[key] for key in ("type", "symbol", "unit") if key in item}
    symbol = item.get("symbol")
    if item.get("type") == "MACRO" and symbol == "US10Y":
        result["value_display"] = format_bps(report_data.get("macro", {}).get(symbol, {}).get("change_bps"))
    elif item.get("type") == "MACRO":
        result["value_display"] = format_pct(report_data.get("macro", {}).get(symbol, {}).get("change_pct"))
    else:
        result["value_display"] = format_pct(item.get("value"))
    return result
