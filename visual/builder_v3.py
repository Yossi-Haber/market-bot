"""Build and render the isolated V3 editorial market-card presentation."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from .builder_v2 import build_view_model_v2

TEMPLATE_PATH = Path(__file__).resolve().parent / "template_v3.html"
STYLES_PATH = Path(__file__).resolve().parent / "styles_v3.css"


def _svg_chart(kind: str = "hero", asset_base_path: str = "../visual/assets/v3") -> str:
    """Return a local inline SVG motif; no external asset or network is used."""
    if kind == "story":
        return f'''<div class="generated-visual story-image-wrap"><img src="{asset_base_path}/story_energy.png" alt=""><div class="image-wash"></div><svg class="story-art-svg story-overlay" viewBox="0 0 360 260" aria-hidden="true"><defs><linearGradient id="storyGlow" x1="0" x2="1"><stop stop-color="#57e6b1"/><stop offset="1" stop-color="#6c8cff"/></linearGradient></defs><path class="chart-grid" d="M20 218H340M20 170H340M20 122H340M20 74H340"/><path class="chart-line" d="M20 198 C70 194 72 151 115 164 S160 190 204 116 S252 140 286 82 S320 91 340 36" stroke="url(#storyGlow)"/><circle cx="340" cy="36" r="8" fill="#57e6b1"/><path class="story-sun" d="M264 42h36M282 24v36M269 29l26 26M295 29l-26 26"/></svg></div>'''
    return f'''<div class="generated-visual hero-image-wrap"><img src="{asset_base_path}/hero_wall_street.png" alt=""><div class="image-wash"></div><svg class="hero-art-svg hero-overlay" viewBox="0 0 420 260" aria-hidden="true"><defs><linearGradient id="heroGlow" x1="0" x2="1"><stop stop-color="#57e6b1"/><stop offset=".55" stop-color="#6c8cff"/><stop offset="1" stop-color="#ffb86b"/></linearGradient></defs><path class="hero-grid" d="M16 222H408M16 176H408M16 130H408M16 84H408"/><path class="hero-line" d="M18 202 C64 188 82 206 118 160 S175 166 205 116 S255 146 284 96 S338 102 402 38" stroke="url(#heroGlow)"/><circle cx="402" cy="38" r="9" fill="#57e6b1"/></svg></div>'''


def _macro_icon(symbol: str) -> str:
    paths = {
        "WTI": '<path d="M12 2c-3 5-6 8-6 12a6 6 0 0 0 12 0c0-4-3-7-6-12Z"/>',
        "GOLD": '<rect x="4" y="7" width="16" height="11" rx="2"/><path d="m7 7 2-3h6l2 3M8 12h8"/>',
        "VIX": '<path d="M2 13c3-9 5 9 8 0s5 9 8 0 3 2 4-2"/>',
        "DXY": '<circle cx="12" cy="12" r="9"/><path d="M12 6v12M15 9c-1-2-5-2-6 0s5 2 5 4-4 2-6 0"/>',
        "US10Y": '<path d="M4 20h16M6 20V9h12v11M4 9h16l-8-5-8 5ZM9 13h2M13 13h2M9 16h2M13 16h2"/>',
    }
    return f'<svg viewBox="0 0 24 24" aria-hidden="true">{paths.get(symbol, "<circle cx=12 cy=12 r=8/>")}</svg>'


def _sector_icon(label: str) -> str:
    paths = {
        "תעשייה": '<path d="M3 20V9l6 3V8l6 3V5l6 3v12Z"/>',
        "טכנולוגיה": '<rect x="4" y="5" width="16" height="12" rx="2"/><path d="M8 21h8M12 17v4"/>',
        "פיננסים": '<path d="M3 10h18M5 10v9M9 10v9M15 10v9M19 10v9M2 21h20M12 3l10 7H2Z"/>',
        "שירותי תקשורת": '<path d="M5 18a10 10 0 0 1 14 0M8 15a6 6 0 0 1 8 0M11 12a2 2 0 0 1 2 0M12 21h.01"/>',
        "אנרגיה": '<path d="M13 2 5 13h6l-1 9 8-11h-6l1-9Z"/>',
    }
    return f'<svg viewBox="0 0 24 24" aria-hidden="true">{paths.get(label, "<circle cx=12 cy=12 r=8/>")}</svg>'


def build_view_model_v3(data: dict[str, Any], final_markdown: str, selection: dict[str, Any]) -> dict[str, Any]:
    """Create V3 layout data from the existing, approved V2-normalized model."""
    base = build_view_model_v2(data, final_markdown, selection)
    macro_by_symbol = {item.get("symbol"): item for item in base["macro"]}
    vix = macro_by_symbol.get("VIX")
    market_tiles = list(base["indices"])
    if vix:
        market_tiles.append({"symbol": "VIX", "label": vix["label"], "close": vix["value"], "change_pct": vix["move"], "class": vix["class"]})
    stories = base["stories"]
    feature = stories[0] if stories else None
    notices = []
    for notice in data.get("unexplained_move_notices", []):
        if not isinstance(notice, dict) or notice.get("status") != "UNEXPLAINED" or not notice.get("message_he"):
            continue
        notices.append({"message": str(notice["message_he"]), "symbols": list(notice.get("symbols", [])), "industry": notice.get("industry_he") or ""})
    sector_icons = {"טכנולוגיה": "⌁", "תעשייה": "▦", "פיננסים": "◈", "אנרגיה": "△", "שירותי תקשורת": "◌"}
    for item in base["top_sectors"] + base["bottom_sectors"]:
        item["icon"] = _sector_icon(item.get("label", ""))
    macro_icons = {"VIX": "波", "GOLD": "◆", "WTI": "◒", "DXY": "$", "US10Y": "10Y"}
    for item in base["macro"]:
        item["icon"] = _macro_icon(item.get("symbol", ""))
    return base | {
        "market_tiles": market_tiles,
        "feature_story": feature,
        "unexplained_notices": notices,
        "hero_visual": _svg_chart("hero"),
        "story_visual": _svg_chart("story"),
        "story_count_label": f"{len(stories)} סיפור{'ים' if len(stories) != 1 else ''} מאומת{'ים' if len(stories) != 1 else ''}",
    }


def _repeat(items: list[dict[str, Any]], markup: str, *, raw_keys: set[str] | None = None) -> str:
    result: list[str] = []
    for item in items:
        raw_keys = raw_keys or set()
        values = {key: str(value) if key in raw_keys else html.escape(str(value)) for key, value in item.items()}
        rendered = markup
        for key, value in values.items():
            rendered = rendered.replace("{{" + key + "}}", value)
        result.append(rendered)
    return "".join(result)


def render_html_v3(model: dict[str, Any], *, asset_base_path: str = "../visual/assets/v3") -> str:
    """Render V3 using only local template and inline SVG/CSS assets."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    styles = STYLES_PATH.read_text(encoding="utf-8") + "\n.main-grid,.side-rail,.feature-story,.feature-copy{min-height:0}.feature-copy{padding-top:24px;padding-bottom:24px;overflow:hidden}.sector-list{gap:7px;margin-top:10px}.sector-row{padding-bottom:6px}.macro-card{position:relative}.macro-card.lagging:before{top:10px;left:10px;font-size:8px;color:#ffb86b}.feature-empty{display:flex;flex-direction:column;align-items:stretch;justify-content:center;gap:18px;padding:28px}.feature-empty-label{font-size:14px;color:#9ab0d0;text-align:center}.unexplained-notice{padding:18px 20px;border:1px solid rgba(255,184,107,.45);border-right:4px solid #ffb86b;border-radius:13px;background:linear-gradient(135deg,rgba(255,184,107,.12),rgba(19,31,54,.72));color:#f4f7ff;text-align:right;direction:rtl}.unexplained-notice>span{font-size:10px;font-weight:700;letter-spacing:.6px;color:#ffd08f}.unexplained-notice p{font-size:17px;line-height:1.5;margin:9px 0;color:#eef3ff}.unexplained-notice small{font-size:10px;color:#a9bbd5;direction:ltr;display:block}\n"
    styles += '\nhtml,body,.poster-v3{font-family:Arial,"Segoe UI","Noto Sans Hebrew","Liberation Sans",sans-serif}\n'
    story = model.get("feature_story")
    if story:
        story_html = (
            '<div class="feature-art">{{story_visual}}</div>'
            '<div class="feature-copy"><small class="eyebrow category-badge">{{tag}}</small>'
            '<h2>{{title}}</h2><p>{{summary}}</p><div class="story-meta">{{text}}</div></div>'
        )
        story_html = story_html.replace(
            "{{story_visual}}",
            model["story_visual"].replace("../visual/assets/v3", asset_base_path),
        )
        for key, value in story.items():
            story_html = story_html.replace("{{" + key + "}}", html.escape(str(value)))
        if model.get("unexplained_notices"):
            story_html = story_html.replace("</div></div>", _notice_html(model["unexplained_notices"]) + "</div></div>")
    else:
        empty_label = '<div class="feature-empty-label">לא קודמו סיפורים מאומתים היום</div>'
        story_html = '<div class="feature-empty">' + empty_label + _notice_html(model.get("unexplained_notices", [])) + '</div>'
    sectors = model["top_sectors"] + model["bottom_sectors"]
    replacements = {
        "styles": styles,
        "title": html.escape(model["title"]), "market_date": html.escape(model["market_date"]),
        "headline": html.escape(model["headline"]), "subheadline": html.escape(model["subheadline"]),
        "tone": html.escape(model["tone"]), "readiness": html.escape(model["readiness"]),
        "hero_visual": model["hero_visual"].replace("../visual/assets/v3", asset_base_path), "story_count_label": html.escape(model["story_count_label"]),
        "market_tiles": _repeat(model["market_tiles"], '<article class="market-tile {{class}}"><div><b>{{symbol}}</b><strong>{{change_pct}}</strong></div><small>{{label}}</small><span>{{close}}</span><i class="tile-spark"></i></article>'),
        "story": story_html,
        "breadth": html.escape(model["breadth"]),
        "drivers": "".join(f'<li>{html.escape(str(item))}</li>' for item in model["drivers"][:3]),
        "sectors": _repeat(sectors[:5], '<div class="sector-row"><i class="sector-icon">{{icon}}</i><span>{{label}}</span><b class="{{class}}">{{change_pct}}</b></div>', raw_keys={"icon"}),
        "macro": _repeat(model["macro"], '<article class="macro-card {{freshness_class}}"><i class="macro-icon">{{icon}}</i><small>{{label}}</small><strong>{{value}}</strong><span class="{{class}}">{{move}}</span><em>{{date_note}}</em></article>', raw_keys={"icon"}),
        "gainers": _repeat(model["gainers"], '<li><b>{{symbol}}</b><span>{{company}}</span><strong class="{{class}}">{{change_pct}}</strong></li>'),
        "losers": _repeat(model["losers"], '<li><b>{{symbol}}</b><span>{{company}}</span><strong class="{{class}}">{{change_pct}}</strong></li>'),
        "warnings": " · ".join(html.escape(str(item)) for item in model["warnings"]) or "ללא אזהרות נוספות",
        "source": html.escape(model["source"]),
    }
    for key, value in replacements.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template


def _notice_html(notices: list[dict[str, Any]]) -> str:
    """Render concise factual notices without adding causal attribution."""
    blocks = []
    for notice in notices:
        symbols = " · ".join(html.escape(str(symbol)) for symbol in notice.get("symbols", []))
        blocks.append(f'<div class="unexplained-notice" data-status="UNEXPLAINED"><span>UNEXPLAINED · תנועה משמעותית ללא גורם מאומת</span><p>{html.escape(str(notice.get("message", "")))}</p><small>{symbols}</small></div>')
    return "".join(blocks)
