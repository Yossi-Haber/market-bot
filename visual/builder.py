"""Build a compact visual view model and deterministic local HTML document."""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from render.formatting import format_bps, format_pct, format_price

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"
TEMPLATE_PATH = Path(__file__).resolve().parent / "template.html"
STYLES_PATH = Path(__file__).resolve().parent / "styles.css"

INDEX_LABELS = {"SPY": "S&P 500", "QQQ": "Nasdaq 100", "DIA": "Dow Jones", "IWM": "Russell 2000"}
SECTOR_LABELS = {"XLK": "טכנולוגיה", "XLF": "פיננסים", "XLE": "אנרגיה", "XLV": "בריאות", "XLY": "צריכה מחזורית", "XLP": "צריכה בסיסית", "XLI": "תעשייה", "XLB": "חומרי גלם", "XLRE": "נדל\"ן", "XLU": "תשתיות", "XLC": "שירותי תקשורת"}
MACRO_LABELS = {"VIX": "מדד הפחד VIX", "GOLD": "זהב", "WTI": "נפט WTI", "DXY": "מדד הדולר DXY", "US10Y": "תשואת אג\"ח ארה\"ב ל־10 שנים"}
WARNING_LABELS = {"CATALYST_COVERAGE_INCOMPLETE": "כיסוי החדשות חלקי", "GDELT_RATE_LIMITED": "מקור חדשות אחד הוגבל", "MACRO_DATA_LAGGING": "חלק מנתוני המאקרו בפיגור", "MOVER_VALIDATION_WARNINGS": "נתוני המניות הבולטות כוללים אזהרה"}
READINESS_LABELS = {"READY": "מוכן לפרסום", "READY_WITH_WARNINGS": "מוכן לפרסום עם הסתייגויות", "NOT_READY": "לא מוכן לפרסום"}


def load_json(path: Path) -> dict[str, Any]:
    """Load one local JSON input as an object."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"INVALID_JSON_OBJECT:{path.name}")
    return value


def format_date(value: str | None) -> str:
    """Format an ISO date as DD.MM.YYYY for the card."""
    if not value:
        return ""
    try:
        return datetime.fromisoformat(str(value)[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return str(value)


def change_class(value: Any) -> str:
    """Return a semantic CSS class for a numeric move."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "neutral"
    return "positive" if number > 0 else "negative" if number < 0 else "neutral"


def extract_narrative(markdown: str, limit: int = 500) -> str:
    """Extract only the first public overview section from approved Markdown."""
    lines = markdown.splitlines()
    selected: list[str] = []
    in_overview = False
    for line in lines:
        if line.startswith("## "):
            if in_overview:
                break
            in_overview = "תמונת" in line or "מצב" in line
            continue
        if in_overview and line.strip() and not line.startswith("#"):
            if selected and line.lstrip().startswith("-"):
                break
            cleaned = re.sub(r"[*_`#]", "", line).strip(" -")
            if cleaned:
                selected.append(cleaned)
    text = " ".join(selected)
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


def build_view_model(data: dict[str, Any], final_markdown: str, selection: dict[str, Any]) -> dict[str, Any]:
    """Convert structured report data into a small template-ready presentation model."""
    indices = []
    for item in data.get("indices", []):
        indices.append({"symbol": item.get("symbol"), "label": INDEX_LABELS.get(item.get("symbol"), item.get("display_name", "")), "close": format_price(item.get("close")), "change_pct": format_pct(item.get("change_pct")), "class": change_class(item.get("change_pct"))})
    sectors = data.get("sectors", {})
    top_sectors = [{"symbol": item.get("symbol"), "label": SECTOR_LABELS.get(item.get("symbol"), item.get("symbol")), "change_pct": format_pct(item.get("change_pct")), "class": change_class(item.get("change_pct"))} for item in sectors.get("top", [])[:3]]
    bottom_sectors = [{"symbol": item.get("symbol"), "label": SECTOR_LABELS.get(item.get("symbol"), item.get("symbol")), "change_pct": format_pct(item.get("change_pct")), "class": change_class(item.get("change_pct"))} for item in sectors.get("bottom", [])[:3]]
    macro = []
    for symbol in ("VIX", "GOLD", "WTI", "DXY", "US10Y"):
        item = data.get("macro", {}).get(symbol)
        if not isinstance(item, dict):
            continue
        if symbol == "US10Y":
            value = f"{format_price(item.get('close'))}%"
            move = format_bps(item.get("change_bps"))
        else:
            value = format_price(item.get("close"), thousands_separator=True)
            move = format_pct(item.get("change_pct"))
        date_note = ""
        if item.get("freshness") == "lagging":
            date_note = f"נכון ל־{format_date(item.get('latest_observation_date') or item.get('latest_bar_datetime'))}"
        macro.append({"symbol": symbol, "label": MACRO_LABELS[symbol], "value": value, "move": move, "date_note": date_note, "class": change_class(item.get("change_bps") if symbol == "US10Y" else item.get("change_pct"))})
    def movers(key: str) -> list[dict[str, Any]]:
        return [{"symbol": item.get("symbol"), "company": item.get("company_name", ""), "change_pct": format_pct(item.get("change_pct")), "class": change_class(item.get("change_pct"))} for item in data.get("movers", {}).get(key, [])[:5]]
    readiness = data.get("publication_readiness", {}).get("status", "NOT_READY")
    warning_codes = data.get("data_quality", {}).get("warnings", [])
    return {
        "title": "סיכום שוק יומי",
        "market_date": format_date(data.get("market_date")),
        "source": "LLM" if selection.get("selected_source") == "LLM" else "Deterministic",
        "narrative": extract_narrative(final_markdown),
        "indices": indices,
        "breadth": f"{sectors.get('positive_count', 0)} מתוך {sectors.get('valid_count', 0)} עלו",
        "top_sectors": top_sectors,
        "bottom_sectors": bottom_sectors,
        "macro": macro,
        "gainers": movers("top_gainers"),
        "losers": movers("top_losers"),
        "readiness": READINESS_LABELS.get(readiness, readiness),
        "warnings": [WARNING_LABELS.get(code, code) for code in warning_codes if code in WARNING_LABELS],
    }


def list_html(items: list[dict[str, Any]], template: str) -> str:
    """Render escaped repeated items into a trusted local template."""
    return "".join(template.replace("{{symbol}}", html.escape(str(item.get("symbol", ""))).replace("&amp;", "&amp;"))
        .replace("{{label}}", html.escape(str(item.get("label", ""))))
        .replace("{{company}}", html.escape(str(item.get("company", ""))))
        .replace("{{close}}", html.escape(str(item.get("close", ""))))
        .replace("{{value}}", html.escape(str(item.get("value", ""))))
        .replace("{{move}}", html.escape(str(item.get("move", ""))))
        .replace("{{change_pct}}", html.escape(str(item.get("change_pct", ""))))
        .replace("{{date_note}}", html.escape(str(item.get("date_note", ""))))
        .replace("{{class}}", html.escape(str(item.get("class", "neutral")))) for item in items)


def render_html(view_model: dict[str, Any]) -> str:
    """Render the local template and stylesheet with no external assets."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    styles = STYLES_PATH.read_text(encoding="utf-8")
    replacements = {
        "{{title}}": html.escape(view_model["title"]), "{{market_date}}": html.escape(view_model["market_date"]), "{{source}}": html.escape(view_model["source"]), "{{narrative}}": html.escape(view_model["narrative"]), "{{breadth}}": html.escape(view_model["breadth"]), "{{readiness}}": html.escape(view_model["readiness"]), "{{styles}}": styles,
        "{{indices}}": list_html(view_model["indices"], '<div class="metric {{class}}"><div class="metric-top"><span class="symbol">{{symbol}}</span><span class="move">{{change_pct}}</span></div><div class="metric-label">{{label}}</div><div class="close">{{close}}</div></div>'),
        "{{top_sectors}}": list_html(view_model["top_sectors"], '<div class="rank-row"><span>{{label}} <small>({{symbol}})</small></span><strong class="{{class}}">{{change_pct}}</strong></div>'),
        "{{bottom_sectors}}": list_html(view_model["bottom_sectors"], '<div class="rank-row"><span>{{label}} <small>({{symbol}})</small></span><strong class="{{class}}">{{change_pct}}</strong></div>'),
        "{{macro}}": list_html(view_model["macro"], '<div class="macro-row"><span>{{label}} <small>{{date_note}}</small></span><strong class="{{class}}">{{value}} <em>{{move}}</em></strong></div>'),
        "{{gainers}}": list_html(view_model["gainers"], '<div class="mover-row"><span><b>{{symbol}}</b> {{company}}</span><strong class="{{class}}">{{change_pct}}</strong></div>'),
        "{{losers}}": list_html(view_model["losers"], '<div class="mover-row"><span><b>{{symbol}}</b> {{company}}</span><strong class="{{class}}">{{change_pct}}</strong></div>'),
        "{{warnings}}": "".join(f'<span class="chip">{html.escape(str(warning))}</span>' for warning in view_model["warnings"]) or '<span class="chip muted">ללא אזהרות נוספות</span>',
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template
