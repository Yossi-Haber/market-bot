"""Compose deterministic daily report data from existing local snapshots."""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from report.selectors import build_indices, build_macro, build_market_overview, build_sectors, highlights, notable_moves, validate_movers
from news.editorial import build_public_stories
from drivers import build_driver_analysis

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"
UNEXPLAINED_MOVE_THRESHOLD_PCT = 10.0


def build_unexplained_move_notices(movers: dict[str, Any], stock_catalysts: list[dict[str, Any]], verified_stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build factual notices for material movers lacking verified explanations.

    This is presentation-only: same-direction movement is never treated as
    evidence of a shared cause. Only validated report movers are considered.
    """
    explained_symbols = {
        str(symbol)
        for story in verified_stories
        if story.get("verification") == "VERIFIED"
        for symbol in story.get("affected_symbols", [])
    }
    for driver in stock_catalysts:
        if driver.get("verification_status") == "CONFIRMED":
            explained_symbols.update(str(symbol) for symbol in driver.get("related_symbols", []))

    classifications = {"T": ("COMMUNICATION_SERVICES", "שירותי התקשורת"), "VZ": ("COMMUNICATION_SERVICES", "שירותי התקשורת")}
    candidates: list[dict[str, Any]] = []
    for key in ("top_gainers", "top_losers"):
        for mover in movers.get(key, []) if isinstance(movers.get(key), list) else []:
            symbol = str(mover.get("symbol") or "")
            try:
                change = float(mover.get("change_pct"))
            except (TypeError, ValueError):
                continue
            if not symbol or not math.isfinite(change) or abs(change) < UNEXPLAINED_MOVE_THRESHOLD_PCT or symbol in explained_symbols:
                continue
            candidates.append({"symbol": symbol, "company_name": mover.get("company_name") or symbol, "change_pct": change, "industry_code": classifications.get(symbol, (None, None))[0], "industry_he": classifications.get(symbol, (None, None))[1]})

    notices: list[dict[str, Any]] = []
    grouped_symbols: set[str] = set()
    by_industry_direction: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in candidates:
        code = item.get("industry_code")
        if code:
            by_industry_direction.setdefault((code, "down" if item["change_pct"] < 0 else "up"), []).append(item)
    for (code, direction), group in by_industry_direction.items():
        if len(group) < 2:
            continue
        grouped_symbols.update(item["symbol"] for item in group)
        verb = "ירדו" if direction == "down" else "עלו"
        descriptions = []
        for item in group:
            company = "AT&T" if item["symbol"] == "T" else "Verizon" if item["symbol"] == "VZ" else str(item["company_name"])
            move_verb = "ירדה" if item["change_pct"] < 0 else "עלתה"
            descriptions.append(f"{company} ({item['symbol']}) {move_verb} ב־{abs(item['change_pct']):.2f}%")
        message = f"מניות {group[0]['industry_he']}: {'; '.join(descriptions)}. נרשמה תנועה ענפית דומה, אך המערכת לא הצליחה לאמת את הגורם לה."
        notices.append({"status": "UNEXPLAINED", "industry": code, "industry_he": group[0]["industry_he"], "symbols": [item["symbol"] for item in group], "moves": [{"symbol": item["symbol"], "change_pct": item["change_pct"]} for item in group], "message_he": message})
    for item in candidates:
        if item["symbol"] in grouped_symbols:
            continue
        direction = "ירדה" if item["change_pct"] < 0 else "עלתה"
        message = f"{item['company_name']} ({item['symbol']}) {direction} בכ־{abs(item['change_pct']):.0f}%. לא נמצא הסבר סיבתי מאומת לתנועה."
        notices.append({"status": "UNEXPLAINED", "industry": item.get("industry_code"), "industry_he": item.get("industry_he"), "symbols": [item["symbol"]], "moves": [{"symbol": item["symbol"], "change_pct": item["change_pct"]}], "message_he": message})
    return notices


class ReportCompositionError(RuntimeError):
    """Raised when report inputs cannot safely be combined."""


def load_snapshot(path: Path, name: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReportCompositionError(f"INVALID_{name.upper()}_SNAPSHOT") from exc
    if not isinstance(payload, dict):
        raise ReportCompositionError(f"INVALID_{name.upper()}_SNAPSHOT")
    return payload


def compose_daily_report(logger: logging.Logger | None = None) -> Path:
    logger = logger or logging.getLogger("market_bot.report")
    started = time.monotonic()
    logger.info("Daily report composer started")
    market = load_snapshot(OUTPUT_DIR / "market_snapshot.json", "market")
    logger.info("Market snapshot loaded")
    movers = load_snapshot(OUTPUT_DIR / "movers_snapshot.json", "movers")
    logger.info("Movers snapshot loaded")
    catalysts = load_snapshot(OUTPUT_DIR / "catalysts_snapshot.json", "catalysts")
    logger.info("Catalyst snapshot loaded")
    dates = {market.get("market_date"), movers.get("market_date"), catalysts.get("market_date")}
    if len(dates) != 1 or None in dates:
        logger.error("MARKET_DATE_MISMATCH: %s", sorted(str(value) for value in dates))
        raise ReportCompositionError("MARKET_DATE_MISMATCH")
    market_date = market["market_date"]
    logger.info("Market date verified: %s", market_date)

    indices, invalid_core = build_indices(market.get("market", {}))
    sectors, invalid_sectors = build_sectors(market.get("market", {}))
    macro, macro_quality, invalid_macro = build_macro(market.get("market", {}))
    logger.info("Sector breadth calculated")
    gainers, losers, mover_warnings, duplicate_movers = validate_movers(movers)
    all_movers = gainers + losers
    catalyst_map = catalysts.get("catalysts", {}) if isinstance(catalysts.get("catalysts"), dict) else {}
    joined_movers = [join_mover(item, catalyst_map.get(item.get("symbol"))) for item in all_movers]
    logger.info("Mover/catalyst join completed")

    verified_stories = select_verified_stories(catalyst_map, market_date)
    try:
        live_news_payload = load_snapshot(OUTPUT_DIR / "live_news_v3_snapshot.json", "live_news")
    except ReportCompositionError:
        live_news_payload = {}
    live_stories = select_verified_live_stories(OUTPUT_DIR / "live_news_v3_snapshot.json", market_date)
    if live_stories:
        verified_stories = merge_verified_stories(verified_stories, live_stories)
        logger.info("Verified live stories selected: %d", len(live_stories))
    unconfirmed_leads = select_unconfirmed_leads(catalyst_map)
    logger.info("Verified stories selected: %d", len(verified_stories))
    market_overview = build_market_overview(indices, sectors["all"])
    notable = notable_moves(indices, sectors, macro, all_movers)
    highlight_candidates = highlights(indices, sectors, all_movers, macro)
    for item in joined_movers:
        item["report_priority_score"] = priority_score(item, catalyst_map.get(item.get("symbol")))

    warnings = list(mover_warnings)
    news_coverage_status = str(live_news_payload.get("request_budget", {}).get("news_coverage_status", "SUFFICIENT"))
    if news_coverage_status == "INSUFFICIENT":
        warnings.append("NEWS_COVERAGE_INSUFFICIENT")
    warnings.extend(f"INVALID_CORE:{symbol}" for symbol in invalid_core)
    warnings.extend(f"INVALID_SECTOR:{symbol}" for symbol in invalid_sectors)
    warnings.extend(f"INVALID_MACRO:{symbol}" for symbol in invalid_macro)
    if macro_quality["lagging_count"]:
        warnings.append("MACRO_DATA_LAGGING")
    if catalysts.get("summary", {}).get("search_incomplete", 0):
        warnings.append("CATALYST_COVERAGE_INCOMPLETE")
    if "GDELT_RATE_LIMITED" in {limitation for result in catalyst_map.values() for limitation in result.get("coverage_limitations", [])}:
        warnings.append("GDELT_RATE_LIMITED")
    failed_instruments = list(market.get("summary", {}).get("failed", []) if isinstance(market.get("summary", {}).get("failed"), list) else [])
    if market.get("summary", {}).get("failed", 0):
        warnings.append("MARKET_SNAPSHOT_HAS_FAILURES")
    driver_input = {
        "market": market.get("market", {}),
        "macro": macro,
        "sectors": sectors,
        "movers": {"top_gainers": joined_movers[:len(gainers)], "top_losers": joined_movers[len(gainers):]},
        "verified_stories": verified_stories,
        "candidate_stories": [],
    }
    market_driver_snapshot = None
    snapshot_path = OUTPUT_DIR / "market_driver_snapshot.json"
    if snapshot_path.is_file():
        try:
            candidate_snapshot = load_snapshot(snapshot_path, "market_driver")
            if candidate_snapshot.get("market_date") == market_date and isinstance(candidate_snapshot.get("driver_analysis"), dict):
                market_driver_snapshot = candidate_snapshot
        except ReportCompositionError:
            market_driver_snapshot = None
    if market_driver_snapshot:
        driver_analysis = market_driver_snapshot["driver_analysis"]
        logger.info("Market Drivers source of truth loaded; legacy catalyst output is not used for driver explanation")
    else:
        driver_analysis = build_driver_analysis(driver_input)
        logger.info("Market driver hypotheses calculated from legacy report inputs")
    logger.info("Market driver hypotheses calculated: market=%d sector=%d stock=%d", driver_analysis["metrics"]["market_driver_hypotheses"], driver_analysis["metrics"]["sector_driver_hypotheses"], driver_analysis["metrics"]["stock_catalyst_targets"])
    readiness = publication_readiness(indices, sectors, mover_warnings, dates, warnings)
    logger.info("Publication readiness: %s", readiness["status"])
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "market_date": market_date,
        "market_overview": market_overview,
        "indices": indices,
        "macro": {**macro, "macro_quality": macro_quality},
        "sectors": sectors,
        "movers": {"top_gainers": joined_movers[:len(gainers)], "top_losers": joined_movers[len(gainers):]},
        "verified_stories": verified_stories,
        "top_stories": verified_stories,
        "news_coverage_status": news_coverage_status,
        "unconfirmed_leads": unconfirmed_leads,
        "highlight_candidates": highlight_candidates,
        "notable_moves": notable,
        "main_market_driver": driver_analysis["main_market_driver"],
        "sector_drivers": driver_analysis["sector_drivers"],
        "stock_catalysts": driver_analysis["stock_catalysts"],
        "confirmed_drivers": [item for item in [driver_analysis.get("main_market_driver"), *driver_analysis.get("sector_drivers", []), *driver_analysis.get("stock_catalysts", [])] if item and item.get("verification_status") == "CONFIRMED"],
        "plausible_drivers": [item for item in [driver_analysis.get("main_market_driver"), *driver_analysis.get("sector_drivers", []), *driver_analysis.get("stock_catalysts", [])] if item and item.get("verification_status") == "PLAUSIBLE_NOT_VERIFIED"],
        "unexplained_moves": [item for item in [driver_analysis.get("main_market_driver"), *driver_analysis.get("sector_drivers", []), *driver_analysis.get("stock_catalysts", [])] if item and item.get("verification_status") == "NO_CATALYST_FOUND"],
        "unexplained_move_notices": build_unexplained_move_notices({"top_gainers": joined_movers[:len(gainers)], "top_losers": joined_movers[len(gainers):]}, driver_analysis.get("stock_catalysts", []), verified_stories),
        "market_drivers_source_of_truth": bool(market_driver_snapshot),
        "legacy_catalyst_used_for_driver": False,
        "driver_metrics": {**driver_analysis["metrics"], **(market_driver_snapshot.get("metrics", {}) if market_driver_snapshot else {}), "driver_discovery_requests": int(live_news_payload.get("request_budget", {}).get("marketaux_requests_attempted", 0) or 0), "marketaux_requests": int(live_news_payload.get("request_budget", {}).get("marketaux_requests_attempted", 0) or 0), "finnhub_requests": int((market_driver_snapshot or {}).get("metrics", {}).get("finnhub_live_requests", 0) or 0), "marketaux_candidates": int(live_news_payload.get("request_budget", {}).get("marketaux_candidates_total", 0) or 0), "finnhub_candidates": len((market_driver_snapshot or {}).get("candidate_stories", [])), "cross_provider_duplicates_removed": 0},
        "data_quality": {"market_snapshot_verified": not invalid_core and not invalid_sectors and not invalid_macro, "movers_snapshot_verified": not duplicate_movers and not mover_warnings, "catalyst_snapshot_loaded": True, "macro_lagging_items": [symbol for symbol, item in macro.items() if item.get("freshness") == "lagging"], "failed_instruments": failed_instruments + invalid_core + invalid_sectors + invalid_macro, "warnings": sorted(set(warnings))},
        "publication_readiness": readiness,
        "composer_statistics": {"http_requests": 0, "runtime_seconds": round(time.monotonic() - started, 3)},
    }
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    current = OUTPUT_DIR / "daily_report_data.json"
    history = OUTPUT_DIR / f"daily_report_data_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    current.write_text(text, encoding="utf-8")
    history.write_text(text, encoding="utf-8")
    logger.info("Daily report data created: %s", current)
    return current


def join_mover(mover: dict[str, Any], catalyst: dict[str, Any] | None) -> dict[str, Any]:
    result = {key: mover.get(key) for key in ("symbol", "company_name", "close", "change_pct", "volume", "source", "latest_bar_datetime")}
    if catalyst:
        result["catalyst"] = {key: catalyst[key] for key in ("catalyst_status", "catalyst_type", "evidence_quality", "headline", "event_datetime", "explicit_market_link") if key in catalyst}
    return result


def select_verified_stories(catalysts: dict[str, Any], market_date: str | None = None) -> list[dict[str, Any]]:
    """Return only public-safe, verified event stories; price moves are not stories."""
    stories = build_public_stories(catalysts, market_date)
    # Keep the legacy symbol field for internal callers while exposing the new public schema.
    for story in stories:
        symbols = story.get("affected_symbols", [])
        story["symbol"] = symbols[0] if symbols else ""
        story["headline"] = story.get("title_he")
        story["type"] = story.get("category")
    return stories


def select_unconfirmed_leads(catalysts: dict[str, Any]) -> list[dict[str, Any]]:
    leads: list[dict[str, Any]] = []
    for symbol, catalyst in catalysts.items():
        if catalyst.get("catalyst_status") == "NOT_CONFIRMED" and catalyst.get("evidence_quality") == "MODERATE":
            leads.append({"symbol": symbol, "type": catalyst.get("catalyst_type"), "headline": catalyst.get("headline"), "status": catalyst.get("catalyst_status"), "evidence_quality": catalyst.get("evidence_quality"), "display_in_public_report": False})
    return leads[:5]


def select_verified_live_stories(path: Path, market_date: str) -> list[dict[str, Any]]:
    """Load only the public-safe verified stories from the optional live snapshot."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if payload.get("market_date") != market_date:
        return []
    selected: list[dict[str, Any]] = []
    for item in payload.get("stories", []):
        if not isinstance(item, dict) or item.get("verification") != "VERIFIED" or item.get("editorial_eligible") is not True:
            continue
        symbols = [str(value) for value in item.get("affected_symbols", []) if value]
        primary_name = item.get("primary_source_name") or item.get("source_name") or "מקור מאומת"
        story = {
            "title_he": item.get("title_he") or item.get("title"),
            "summary_he": item.get("summary_he") or item.get("summary") or item.get("title"),
            "category": item.get("category") or "COMPANY",
            "published_at": item.get("published_at"),
            "source_name": primary_name,
            "source_domain": item.get("primary_source_domain") or item.get("source_domain"),
            "source_url": item.get("primary_source_url") or item.get("source_url"),
            "affected_symbols": symbols,
            "verification": "VERIFIED",
            "editorial_eligible": True,
            "primary_source_name": primary_name,
            "primary_source_domain": item.get("primary_source_domain") or item.get("source_domain"),
            "primary_source_url": item.get("primary_source_url") or item.get("source_url"),
            "primary_source_type": item.get("primary_source_type") or "OFFICIAL",
            "supporting_sources_count": int(item.get("supporting_sources_count", 1) or 1),
        }
        if story["title_he"] and story["published_at"]:
            selected.append(story)
    return selected


def merge_verified_stories(existing: list[dict[str, Any]], live: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge live stories without padding or duplicating an existing title/event."""
    merged = list(existing)
    keys = {(str(item.get("title_he") or item.get("headline") or "").strip().lower(), tuple(item.get("affected_symbols", []))) for item in merged}
    for item in live:
        key = (str(item.get("title_he") or "").strip().lower(), tuple(item.get("affected_symbols", [])))
        if key not in keys:
            merged.append(item)
            keys.add(key)
    return merged


def priority_score(mover: dict[str, Any], catalyst: dict[str, Any] | None) -> float:
    score = abs(float(mover.get("change_pct") or 0))
    if catalyst and catalyst.get("catalyst_status") == "CONFIRMED":
        score += 10.0
    if catalyst and catalyst.get("catalyst_status") == "POSSIBLE" and catalyst.get("evidence_quality") == "STRONG":
        score += 3.0
    return round(score, 4)


def publication_readiness(indices: list[dict[str, Any]], sectors: dict[str, Any], mover_warnings: list[str], dates: set[Any], warnings: list[str]) -> dict[str, Any]:
    reasons: list[str] = []
    if len(indices) < 4:
        reasons.append("MISSING_CORE_MARKET_DATA")
    if sectors.get("valid_count", 0) < 11:
        reasons.append("INCOMPLETE_SECTOR_DATA")
    if mover_warnings:
        reasons.append("MOVER_VALIDATION_WARNINGS")
    if len(dates) != 1:
        reasons.append("MARKET_DATE_MISMATCH")
    if reasons:
        return {"status": "NOT_READY", "reasons": reasons}
    if warnings:
        return {"status": "READY_WITH_WARNINGS", "reasons": sorted(set(warnings))}
    return {"status": "READY", "reasons": []}
