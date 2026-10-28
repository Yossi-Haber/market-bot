"""Render composed report JSON as concise, deterministic Hebrew Markdown."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from news.editorial import public_hebrew_copy

from .formatting import format_bps, format_pct, format_price, format_signed_number

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"

CORE_LABELS = {
    "SPY": "S&P 500 ‏(SPY)",
    "QQQ": "Nasdaq 100 ‏(QQQ)",
    "DIA": "Dow Jones ‏(DIA)",
    "IWM": "Russell 2000 ‏(IWM)",
}
SECTOR_LABELS = {
    "XLK": "טכנולוגיה",
    "XLF": "פיננסים",
    "XLE": "אנרגיה",
    "XLV": "בריאות",
    "XLY": "צריכה מחזורית",
    "XLP": "צריכה בסיסית",
    "XLI": "תעשייה",
    "XLB": "חומרי גלם",
    "XLRE": "נדל\"ן",
    "XLU": "תשתיות",
    "XLC": "שירותי תקשורת",
}
MACRO_LABELS = {
    "VIX": "מדד הפחד VIX",
    "GOLD": "זהב",
    "WTI": "נפט WTI",
    "DXY": "מדד הדולר DXY",
    "US10Y": "תשואת אג\"ח ארה\"ב ל־10 שנים",
}
HEADLINES = {
    "BROADLY_POSITIVE": "וול סטריט סיימה את יום המסחר בעליות רחבות.",
    "MIXED_POSITIVE": "המסחר הסתיים במגמה חיובית, אך לא באופן אחיד.",
    "MIXED": "המסחר הסתיים במגמה מעורבת.",
    "MIXED_NEGATIVE": "המסחר הסתיים במגמה שלילית, אך לא באופן אחיד.",
    "BROADLY_NEGATIVE": "וול סטריט סיימה את יום המסחר בירידות רחבות.",
}
WARNING_LABELS = {
    "CATALYST_COVERAGE_INCOMPLETE": "כיסוי החדשות והקטליסטים אינו מלא.",
    "GDELT_RATE_LIMITED": "מקור חדשות אחד היה מוגבל בזמן הריצה.",
    "MACRO_DATA_LAGGING": "חלק מנתוני המאקרו מעודכנים לתאריך מוקדם יותר.",
    "MOVER_VALIDATION_WARNINGS": "נתוני המניות הבולטות כללו אזהרת בדיקה.",
}


class ReportNotReadyError(RuntimeError):
    """Raised when the composed report is explicitly not ready for publication."""


def load_report_data(path: Path) -> dict[str, Any]:
    """Load and minimally validate the composed report JSON."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("INVALID_DAILY_REPORT_DATA") from exc
    if not isinstance(data, dict):
        raise ValueError("INVALID_DAILY_REPORT_DATA")
    if not data.get("market_date") or not isinstance(data.get("indices"), list):
        raise ValueError("INCOMPLETE_DAILY_REPORT_DATA")
    if not isinstance(data.get("movers"), dict):
        raise ValueError("INCOMPLETE_DAILY_REPORT_DATA")
    readiness = data.get("publication_readiness", {})
    if readiness.get("status") == "NOT_READY":
        raise ReportNotReadyError("REPORT_NOT_READY")
    return data


def render_report(data: dict[str, Any]) -> str:
    """Render report data without adding facts or making external calls."""
    lines: list[str] = ["# סיכום השוק היומי", "", f"תאריך: {data['market_date']}", ""]
    lines.extend(["## תמונת מצב", "", HEADLINES.get(data.get("market_overview", {}).get("market_direction"), "המסחר הסתיים במגמה מעורבת."), ""])
    lines.extend(render_indices(data.get("indices", [])))
    lines.extend(render_sectors(data.get("sectors", {})))
    lines.extend(render_macro(data.get("macro", {})))
    lines.extend(render_movers(data.get("movers", {})))
    lines.extend(render_verified_stories(data))
    notable = render_notable_moves(data.get("notable_moves", []), data)
    if notable:
        lines.extend(notable)
    lines.extend(render_quality(data))
    return "\n".join(lines).rstrip() + "\n"


def render_indices(indices: list[dict[str, Any]]) -> list[str]:
    lines = ["## המדדים המרכזיים", "", "הנתונים מבוססים על ETF proxies ולא על המדדים עצמם.", ""]
    for item in indices:
        symbol = item.get("symbol", "")
        label = CORE_LABELS.get(symbol, symbol)
        lines.append(f"- {label}: {format_pct(item.get('change_pct'))} | {format_price(item.get('close'))}")
    return lines + [""]


def render_sectors(sectors: dict[str, Any]) -> list[str]:
    breadth = sectors.get("breadth", {})
    positive = sectors.get("positive_count", 0)
    valid = sectors.get("valid_count", 0)
    percentage = breadth.get("percentage", 0)
    lines = ["## סקטורים", "", f"{positive} מתוך {valid} הסקטורים סיימו בעליות ({format_price(percentage)}%).", "", "המובילים:"]
    for item in sectors.get("top", [])[:3]:
        lines.append(f"- {SECTOR_LABELS.get(item.get('symbol'), item.get('symbol', ''))} ({item.get('symbol', '')}): {format_pct(item.get('change_pct'))}")
    lines.append("")
    lines.append("החלשים:")
    for item in sectors.get("bottom", [])[:3]:
        lines.append(f"- {SECTOR_LABELS.get(item.get('symbol'), item.get('symbol', ''))} ({item.get('symbol', '')}): {format_pct(item.get('change_pct'))}")
    return lines + [""]


def render_macro(macro: dict[str, Any]) -> list[str]:
    lines = ["## מאקרו", ""]
    for symbol in ("VIX", "GOLD", "WTI", "DXY", "US10Y"):
        item = macro.get(symbol)
        if not isinstance(item, dict):
            continue
        label = MACRO_LABELS[symbol]
        freshness = item.get("freshness")
        date = item.get("latest_observation_date") or item.get("latest_bar_datetime")
        suffix = ""
        if freshness == "lagging" and date:
            suffix = f" (נתון מעודכן ל־{str(date)[:10]})"
        elif freshness == "stale":
            suffix = " (נתון מיושן יחסית)"
        if symbol == "US10Y":
            move = format_bps(item.get("change_bps"))
            lines.append(f"- {label}: {format_price(item.get('close'))}% | {move}{suffix}")
        else:
            lines.append(f"- {label}: {format_price(item.get('close'))} | {format_pct(item.get('change_pct'))}{suffix}")
    return lines + [""]


def render_movers(movers: dict[str, Any]) -> list[str]:
    lines = ["## מניות בולטות", "", "### עולות"]
    for index, item in enumerate(movers.get("top_gainers", []), 1):
        lines.append(f"{index}. {mover_text(item)}")
    lines.extend(["", "### יורדות"])
    for index, item in enumerate(movers.get("top_losers", []), 1):
        lines.append(f"{index}. {mover_text(item)}")
    return lines + [""]


def mover_text(item: dict[str, Any]) -> str:
    """Format one mover using only its supplied fields."""
    symbol = item.get("symbol", "")
    company = item.get("company_name")
    middle = f" — {company}" if company else ""
    return f"{symbol}{middle} — {format_pct(item.get('change_pct'))}"


def render_verified_stories(data: dict[str, Any]) -> list[str]:
    stories = data.get("verified_stories", [])
    lines = ["## סיפורים מאומתים", ""]
    if not stories:
        return lines + ["לא נמצאו סיפורי מניות עם קטליסט מאומת ברמת האמינות שנדרשה לדוח.", ""]
    mover_by_symbol = {item.get("symbol"): item for item in data.get("movers", {}).get("top_gainers", []) + data.get("movers", {}).get("top_losers", [])}
    for story in stories:
        symbol = story.get("symbol") or next(iter(story.get("affected_symbols", [])), "")
        move = mover_by_symbol.get(symbol, {}).get("change_pct")
        move_text = f" | שינוי: {format_pct(move)}" if move is not None else ""
        lines.append(f"- {symbol}{move_text}")
        category = story.get("category") or story.get("type")
        if category:
            category_display = {"EARNINGS": "דוחות / תחזית"}.get(str(category), category)
            lines.append(f"  - סוג אירוע: {category_display}")
        public_copy = public_hebrew_copy(story)
        title = public_copy.get("title_he")
        if title:
            lines.append(f"  - כותרת: {title}")
        elif story.get("title") or story.get("headline"):
            lines.append(f"  - כותרת (לא תורגמה): {story.get('title') or story.get('headline')}")
        source = story.get("primary_source_name") or story.get("source_name")
        if not source and isinstance(story.get("primary_source"), str):
            source = story["primary_source"]
        if source:
            lines.append(f"  - מקור: {source}")
    return lines + [""]


def render_notable_moves(items: list[dict[str, Any]], data: dict[str, Any]) -> list[str]:
    if not items:
        return []
    lines = ["## נקודות בולטות", ""]
    mover_by_symbol = {item.get("symbol"): item for item in data.get("movers", {}).get("top_gainers", []) + data.get("movers", {}).get("top_losers", [])}
    macro = data.get("macro", {})
    sectors = {item.get("symbol"): item for item in data.get("sectors", {}).get("all", [])}
    def notable_priority(item: dict[str, Any]) -> float:
        symbol = item.get("symbol")
        if item.get("type") == "STOCK_MOVER":
            return float(mover_by_symbol.get(symbol, {}).get("report_priority_score", abs(float(item.get("value", 0)))))
        return abs(float(item.get("value", 0)))

    for item in sorted(items, key=notable_priority, reverse=True)[:5]:
        symbol = item.get("symbol", "")
        kind = item.get("type")
        if kind == "STOCK_MOVER":
            value = mover_by_symbol.get(symbol, {}).get("change_pct", item.get("value"))
            lines.append(f"- {symbol}: שינוי יומי של {format_pct(value)}")
        elif kind == "MACRO" and symbol in macro:
            if symbol == "US10Y":
                lines.append(f"- {MACRO_LABELS[symbol]}: שינוי של {format_bps(macro[symbol].get('change_bps'))}")
            else:
                lines.append(f"- {MACRO_LABELS.get(symbol, symbol)}: שינוי יומי של {format_pct(macro[symbol].get('change_pct'))}")
        elif kind == "SECTOR_MOVE" and symbol in sectors:
            lines.append(f"- {SECTOR_LABELS.get(symbol, symbol)} ({symbol}): שינוי יומי של {format_pct(sectors[symbol].get('change_pct'))}")
    return lines + [""] if len(lines) > 2 else []


def render_quality(data: dict[str, Any]) -> list[str]:
    readiness = data.get("publication_readiness", {})
    status = readiness.get("status")
    if status == "READY":
        status_text = "סטטוס הדוח: מוכן לפרסום."
    elif status == "READY_WITH_WARNINGS":
        status_text = "סטטוס הדוח: מוכן לפרסום עם הסתייגויות."
    else:
        status_text = "סטטוס הדוח: אינו מוכן לפרסום."
    lines = ["## איכות הנתונים", "", status_text]
    for warning in sorted(set(data.get("data_quality", {}).get("warnings", []))):
        lines.append(f"- {WARNING_LABELS.get(warning, warning)}")
    return lines + [""]


def write_rendered_files(data: dict[str, Any], logger: logging.Logger | None = None) -> Path:
    """Write the current and timestamped public Markdown files."""
    logger = logger or logging.getLogger("market_bot.render")
    text = render_report(data)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    current = OUTPUT_DIR / "daily_report.md"
    history = OUTPUT_DIR / f"daily_report_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.md"
    current.write_text(text, encoding="utf-8")
    history.write_text(text, encoding="utf-8")
    logger.info("Daily Hebrew Markdown report created: %s", current)
    return current


def write_audit(data: dict[str, Any], logger: logging.Logger | None = None) -> Path:
    """Write a small internal audit without secrets or raw news content."""
    logger = logger or logging.getLogger("market_bot.render")
    audit = [
        "# Daily Report Audit",
        "",
        f"- generated_at_utc: {datetime.now(timezone.utc).isoformat()}",
        "- input: output/daily_report_data.json",
        f"- market_date: {data.get('market_date')}",
        f"- publication_readiness: {data.get('publication_readiness', {}).get('status')}",
        f"- verified_story_count: {len(data.get('verified_stories', []))}",
        f"- macro_lagging_items: {', '.join(data.get('data_quality', {}).get('macro_lagging_items', [])) or 'none'}",
        f"- warnings: {', '.join(data.get('data_quality', {}).get('warnings', [])) or 'none'}",
    ]
    path = OUTPUT_DIR / "daily_report_audit.md"
    path.write_text("\n".join(audit) + "\n", encoding="utf-8")
    logger.info("Daily report audit created: %s", path)
    return path


def render_from_file(input_path: Path | None = None, logger: logging.Logger | None = None) -> Path | None:
    """Render the composed JSON; return None and log when it is not ready."""
    logger = logger or logging.getLogger("market_bot.render")
    input_path = input_path or OUTPUT_DIR / "daily_report_data.json"
    started = time.monotonic()
    try:
        data = load_report_data(input_path)
    except ReportNotReadyError:
        logger.error("REPORT_NOT_READY")
        return None
    write_audit(data, logger)
    result = write_rendered_files(data, logger)
    logger.info("Rendering completed in %.3f seconds", time.monotonic() - started)
    return result
