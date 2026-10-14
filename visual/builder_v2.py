"""Build the richer, deterministic V2 market-card presentation model."""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from render.formatting import format_bps, format_pct, format_price
from visual.builder import (
    BASE_DIR,
    INDEX_LABELS,
    MACRO_LABELS,
    READINESS_LABELS,
    SECTOR_LABELS,
    WARNING_LABELS,
    change_class,
    extract_narrative,
    load_json,
)

OUTPUT_DIR = BASE_DIR / "output"
TEMPLATE_PATH = Path(__file__).resolve().parent / "template_v2.html"
STYLES_PATH = Path(__file__).resolve().parent / "styles_v2.css"


def _direction_text(value: float, positive: str = "עלה", negative: str = "ירד") -> str:
    return positive if value > 0 else negative if value < 0 else "נותר ללא שינוי"


def _date(value: Any) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(str(value)[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return str(value)


def _item(symbol: str, label: str, value: Any, move: Any, *, date_note: str = "") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "label": label,
        "value": value,
        "move": move,
        "raw_move": move,
        "class": change_class(move),
        "date_note": date_note,
    }


def _build_drivers(data: dict[str, Any], indices: list[dict[str, Any]], top: list[dict[str, Any]], macro: dict[str, Any], stories: list[dict[str, Any]]) -> list[str]:
    drivers: list[str] = []
    for story in stories[:2]:
        symbols = [str(value) for value in story.get("affected_symbols", []) if value]
        title, _ = _story_copy(story, symbols)
        title = str(title).strip()
        if title:
            drivers.append(title)
    if drivers:
        if top:
            drivers.append(f"{top[0]['label']} הוביל את הסקטורים ({top[0]['change_pct']})")
        return drivers[:4]
    breadth = data.get("sectors", {})
    if top:
        drivers.append(f"{top[0]['label']} הוביל את הסקטורים ({top[0]['change_pct']})")
    for symbol in ("VIX", "US10Y", "GOLD", "WTI", "DXY"):
        item = macro.get(symbol)
        if not item or len(drivers) >= 5:
            continue
        if symbol == "US10Y":
            move = item.get("change_bps")
            if move is not None and abs(float(move)) >= 1:
                drivers.append(f"תשואת אג\"ח ארה\"ב ל־10 שנים { _direction_text(float(move), 'עלתה', 'ירדה') } ב־{format_bps(move)}")
        elif item.get("change_pct") is not None and abs(float(item.get("change_pct"))) >= 0.5:
            label = MACRO_LABELS.get(symbol, symbol)
            drivers.append(f"{label} { _direction_text(float(item['change_pct'])) } ב־{format_pct(item['change_pct'])}")
    if not drivers and breadth.get("valid_count"):
        drivers.append(f"רוחב השוק: {breadth.get('positive_count', 0)} מתוך {breadth.get('valid_count', 0)} סקטורים עלו")
    return drivers[:4]


def _story_time(story: dict[str, Any], market_date: str) -> str:
    value = story.get("published_at")
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        time_text = parsed.strftime("%H:%M")
        return f"היום · {time_text}" if parsed.date().isoformat() == market_date else f"אתמול · {time_text}"
    except ValueError:
        return ""


def _has_hebrew(value: Any) -> bool:
    return bool(re.search(r"[\u0590-\u05ff]", str(value or "")))


def _story_category(value: Any) -> str:
    return {"EARNINGS": "דוחות / תחזית", "M&A": "מיזוגים ורכישות", "REGULATORY": "רגולציה", "TECH_PRODUCT": "מוצר / טכנולוגיה"}.get(str(value or ""), str(value or "סיפור מאומת"))


def _story_copy(story: dict[str, Any], symbols: list[str]) -> tuple[str, str]:
    """Use Hebrew narrative fields, with a narrow factual fallback for legacy English KLXE data."""
    title = story.get("title_he") or story.get("title") or story.get("headline") or ""
    summary = story.get("summary_he") or story.get("summary") or ""
    if _has_hebrew(title) and _has_hebrew(summary):
        return str(title), str(summary)
    source_text = f"{title} {summary}".lower()
    symbol = symbols[0] if symbols else ""
    if str(story.get("category")) == "EARNINGS" and "guidance" in source_text:
        range_match = re.search(r"\$?\s*(\d+(?:\.\d+)?)\s*million\s*(?:to|-)\s*\$?\s*(\d+(?:\.\d+)?)\s*million", source_text)
        range_text = f" לטווח של {range_match.group(1)}–{range_match.group(2)} מיליון דולר" if range_match else ""
        return f"{symbol} העלתה את תחזית ההכנסות לרבעון השלישי", f"החברה עדכנה את תחזית ההכנסות לרבעון השלישי{range_text}."
    return str(title), str(summary)


def _build_stories(market_date: str, top_stories: list[dict[str, Any]]) -> list[dict[str, str]]:
    stories: list[dict[str, str]] = []
    for story in top_stories[:4]:
        if not isinstance(story, dict):
            continue
        symbols = [str(value) for value in story.get("affected_symbols", []) if value]
        time_text = _story_time(story, market_date)
        source = story.get("primary_source_name") or story.get("source_name") or "מקור מאומת"
        title, summary = _story_copy(story, symbols)
        details = " · ".join(value for value in (f"מקור: {source}", time_text, ", ".join(symbols)) if value)
        stories.append({"tag": _story_category(story.get("category")), "title": title, "summary": summary, "text": details, "class": "accent"})
    return stories[:4]


def build_view_model_v2(data: dict[str, Any], final_markdown: str, selection: dict[str, Any]) -> dict[str, Any]:
    """Create V2 content from approved structured data and narrative only."""
    indices = []
    for item in data.get("indices", []):
        raw = float(item.get("change_pct", 0) or 0)
        indices.append({"symbol": item.get("symbol"), "label": INDEX_LABELS.get(item.get("symbol"), item.get("display_name", "")), "close": format_price(item.get("close")), "change_pct": format_pct(raw), "raw_change_pct": raw, "class": change_class(raw)})
    sectors = data.get("sectors", {})
    top = [{"symbol": item.get("symbol"), "label": SECTOR_LABELS.get(item.get("symbol"), item.get("symbol", "")), "change_pct": format_pct(item.get("change_pct")), "raw_change_pct": float(item.get("change_pct", 0) or 0), "class": change_class(item.get("change_pct"))} for item in sectors.get("top", [])[:3]]
    bottom = [{"symbol": item.get("symbol"), "label": SECTOR_LABELS.get(item.get("symbol"), item.get("symbol", "")), "change_pct": format_pct(item.get("change_pct")), "raw_change_pct": float(item.get("change_pct", 0) or 0), "class": change_class(item.get("change_pct"))} for item in sectors.get("bottom", [])[:3]]
    macro: dict[str, dict[str, Any]] = {}
    for symbol in ("VIX", "GOLD", "WTI", "DXY", "US10Y"):
        item = data.get("macro", {}).get(symbol)
        if not isinstance(item, dict):
            continue
        raw_move = item.get("change_bps") if symbol == "US10Y" else item.get("change_pct")
        value = f"{format_price(item.get('close'), thousands_separator=True)}%" if symbol == "US10Y" else format_price(item.get("close"), thousands_separator=True)
        move = format_bps(raw_move) if symbol == "US10Y" else format_pct(raw_move)
        date_note = f"נכון ל־{_date(item.get('latest_observation_date') or item.get('latest_bar_datetime'))}" if item.get("freshness") == "lagging" else ""
        macro[symbol] = _item(symbol, MACRO_LABELS[symbol], value, raw_move, date_note=date_note) | {"move": move, "freshness_class": "lagging" if item.get("freshness") == "lagging" else "current"}
    def movers(key: str) -> list[dict[str, Any]]:
        return [{"symbol": item.get("symbol"), "company": item.get("company_name", ""), "change_pct": format_pct(item.get("change_pct")), "class": change_class(item.get("change_pct"))} for item in data.get("movers", {}).get(key, [])[:3]]
    direction = data.get("market_overview", {}).get("market_direction", "MIXED")
    headline = {"BROADLY_POSITIVE": "וול סטריט סיימה בעליות רחבות", "MIXED_POSITIVE": "וול סטריט סיימה בעליות, אך לא באופן אחיד", "MIXED": "וול סטריט סיימה במגמה מעורבת", "MIXED_NEGATIVE": "וול סטריט סיימה בירידות, אך לא באופן אחיד", "BROADLY_NEGATIVE": "וול סטריט סיימה בירידות רחבות"}.get(direction, "וול סטריט סיימה במגמה מעורבת")
    subheadline = extract_narrative(final_markdown, 220) or "תמונת מצב יומית של המדדים, הסקטורים והמאקרו."
    warning_codes = data.get("data_quality", {}).get("warnings", [])
    top_stories = data.get("top_stories")
    if not isinstance(top_stories, list):
        top_stories = []
    return {
        "title": "סיכום שוק יומי", "market_date": _date(data.get("market_date")), "headline": headline, "subheadline": subheadline,
        "tone": {"BROADLY_POSITIVE": "חיובי", "MIXED_POSITIVE": "חיובי־מעורב", "MIXED": "מעורב", "MIXED_NEGATIVE": "שלילי־מעורב", "BROADLY_NEGATIVE": "שלילי"}.get(direction, "מעורב"),
        "indices": indices, "breadth": f"{sectors.get('positive_count', 0)} מתוך {sectors.get('valid_count', 0)} עלו", "breadth_positive": sectors.get("positive_count", 0), "breadth_valid": sectors.get("valid_count", 0),
        "top_sectors": top, "bottom_sectors": bottom, "macro": list(macro.values()), "drivers": _build_drivers(data, indices, top, macro, top_stories), "stories": _build_stories(str(data.get("market_date", "")), top_stories), "top_stories": top_stories, "top_stories_count": len(top_stories), "story_grid_class": f"story-count-{min(len(top_stories), 4)}" if top_stories else "story-count-0",
        "gainers": movers("top_gainers"), "losers": movers("top_losers"), "readiness": READINESS_LABELS.get(data.get("publication_readiness", {}).get("status", "NOT_READY"), "לא מוכן לפרסום"),
        "warnings": [WARNING_LABELS.get(code, code) for code in warning_codes if code in WARNING_LABELS], "source": "LLM" if selection.get("selected_source") == "LLM" else "Deterministic",
    }


def _repeat(items: list[dict[str, Any]], markup: str) -> str:
    result = []
    for item in items:
        values = {key: html.escape(str(value)) for key, value in item.items()}
        result.append(re.sub(r"\{\{(\w+)\}\}", lambda match: values.get(match.group(1), ""), markup))
    return "".join(result)


def render_html_v2(model: dict[str, Any]) -> str:
    """Render the local V2 template with escaped, offline-only content."""
    context = {"top_stories": model.get("top_stories", [])}
    source_count = int(model.get("top_stories_count", len(context["top_stories"])))
    logging.getLogger("market_bot.visual_v2").info("Visual V2 builder context: top_stories_count=%d", len(context["top_stories"]))
    if source_count > 0 and len(context["top_stories"]) == 0:
        raise RuntimeError("Visual V2 lost top_stories during build")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    styles = STYLES_PATH.read_text(encoding="utf-8")
    replacements = {
        "styles": styles,
        "title": model["title"], "market_date": model["market_date"], "headline": model["headline"], "subheadline": model["subheadline"], "tone": model["tone"], "source": model["source"], "breadth": model["breadth"], "readiness": model["readiness"],
        "indices": _repeat(model["indices"], '<article class="index-card {{class}}"><div><b>{{symbol}}</b><span>{{change_pct}}</span></div><small>{{label}}</small><strong>{{close}}</strong></article>'),
        "top_sectors": _repeat(model["top_sectors"], '<div class="bar-row"><span>{{label}}</span><i><em class="{{class}}" style="width:{{raw_change_pct}}%"></em></i><b class="{{class}}">{{change_pct}}</b></div>'),
        "bottom_sectors": _repeat(model["bottom_sectors"], '<div class="bar-row"><span>{{label}}</span><i><em class="{{class}}" style="width:{{raw_change_pct}}%"></em></i><b class="{{class}}">{{change_pct}}</b></div>'),
        "macro": _repeat(model["macro"], '<div class="macro-item {{class}} {{freshness_class}}"><small>{{label}}</small><strong>{{value}}</strong><span>{{move}}</span><em>{{date_note}}</em></div>'),
        "drivers": "".join(f'<li>{html.escape(str(item))}</li>' for item in model["drivers"]),
        "stories": _repeat(model["stories"], '<article class="story-card {{class}}"><small>{{tag}}</small><h3>{{title}}</h3><p>{{summary}}</p><small>{{text}}</small></article>') if len(model["stories"]) > 0 else '<div class="stories-empty">לא נמצאו היום סיפורים מאומתים בעלי השפעה מהותית</div>',
        "story_grid_class": f"story-count-{min(len(model['stories']), 4)}" if model["stories"] else "story-count-0",
        "gainers": _repeat(model["gainers"], '<li><b>{{symbol}}</b><span>{{company}}</span><strong class="{{class}}">{{change_pct}}</strong></li>'),
        "losers": _repeat(model["losers"], '<li><b>{{symbol}}</b><span>{{company}}</span><strong class="{{class}}">{{change_pct}}</strong></li>'),
        "warnings": "".join(f'<span>{html.escape(str(item))}</span>' for item in model["warnings"]) or '<span>ללא אזהרות נוספות</span>',
    }
    for key, value in replacements.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template
