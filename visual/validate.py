"""Deterministic structural validation for the publishable visual report."""

from __future__ import annotations

import json
import base64
import html
import re
import struct
from pathlib import Path
from typing import Any

from .builder import OUTPUT_DIR, READINESS_LABELS, build_view_model, format_date, load_json

REQUIRED_SECTIONS = ("header", "core", "sectors", "macro", "gainers", "losers", "footer")
FORBIDDEN_PUBLIC_CONTENT = ("unconfirmed_leads", "evidence_score", "cache/", "openai_api_key", "twelve_data_api_key", "fred_api_key", "rejected_llm_output", "not_confirmed", "source_conflict", "authorization")


def png_metadata(path: Path) -> dict[str, Any]:
    """Read PNG signature, dimensions, and a basic size sanity check without image libraries."""
    if not path.exists():
        return {"exists": False, "readable": False, "width": None, "height": None, "file_size": 0}
    raw = path.read_bytes()
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        return {"exists": True, "readable": False, "width": None, "height": None, "file_size": len(raw)}
    width, height = struct.unpack(">II", raw[16:24])
    return {"exists": True, "readable": True, "width": width, "height": height, "file_size": len(raw)}


def _check_png_content(path: Path) -> tuple[bool, str | None]:
    """Use a local browser canvas to ensure the image is not uniformly blank."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "PLAYWRIGHT_NOT_INSTALLED"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1080, "height": 1350})
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            page.set_content(f'<img id="visual" src="data:image/png;base64,{encoded}">')
            page.wait_for_function("document.getElementById('visual').complete")
            distinct = page.evaluate("""() => {
                const image = document.getElementById('visual');
                const canvas = document.createElement('canvas');
                canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
                const ctx = canvas.getContext('2d'); ctx.drawImage(image, 0, 0);
                const points = [];
                for (const x of [0, Math.floor(canvas.width/4), Math.floor(canvas.width/2), Math.floor(canvas.width*3/4), canvas.width-1]) {
                  for (const y of [0, Math.floor(canvas.height/4), Math.floor(canvas.height/2), Math.floor(canvas.height*3/4), canvas.height-1]) points.push([x,y]);
                }
                return new Set(points.map(([x,y]) => Array.from(ctx.getImageData(x,y,1,1).data).join(','))).size;
            }""")
            browser.close()
            return bool(distinct and distinct > 1), None if distinct and distinct > 1 else "PNG_UNIFORM_OR_BLANK"
    except Exception as exc:
        return False, f"PNG_READ_ERROR:{type(exc).__name__}"


def validate_visual_report(
    html_path: Path | None = None,
    png_path: Path | None = None,
    data_path: Path | None = None,
    final_markdown_path: Path | None = None,
    selection_path: Path | None = None,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Validate PNG, DOM structure, safe content, and local-only rendering."""
    html_path = html_path or OUTPUT_DIR / "daily_report_card.html"
    png_path = png_path or OUTPUT_DIR / "daily_report_card.png"
    data_path = data_path or OUTPUT_DIR / "daily_report_data.json"
    final_markdown_path = final_markdown_path or OUTPUT_DIR / "daily_report_final.md"
    selection_path = selection_path or OUTPUT_DIR / "daily_report_selection.json"
    manifest_path = manifest_path or OUTPUT_DIR / "daily_report_visual_validation.json"
    errors: list[str] = []
    warnings: list[str] = []
    metadata = png_metadata(png_path)
    if not metadata["exists"]:
        errors.append("PNG_MISSING")
    elif not metadata["readable"]:
        errors.append("PNG_UNREADABLE")
    else:
        if metadata["width"] != 1080 or metadata["height"] != 1350:
            errors.append("PNG_WRONG_DIMENSIONS")
        if metadata["file_size"] < 10_000:
            errors.append("PNG_TOO_SMALL")
        else:
            valid_content, content_error = _check_png_content(png_path)
            if not valid_content and content_error:
                errors.append(content_error)
    sections = {section: False for section in REQUIRED_SECTIONS}
    overflow = False
    text_clipping: list[str] = []
    external_requests = 0
    html_text = ""
    data: dict[str, Any] = {}
    view_model: dict[str, Any] = {}
    try:
        data = load_json(data_path)
        final_markdown = final_markdown_path.read_text(encoding="utf-8")
        selection = load_json(selection_path)
        view_model = build_view_model(data, final_markdown, selection)
    except (OSError, ValueError) as exc:
        errors.append(f"INPUT_ERROR:{type(exc).__name__}")
    if not html_path.exists():
        errors.append("HTML_MISSING")
    else:
        html_text = html_path.read_text(encoding="utf-8")
        html_visible_text = html.unescape(html_text)
        lowered = html_text.lower()
        for forbidden in FORBIDDEN_PUBLIC_CONTENT:
            if forbidden in lowered:
                errors.append(f"PRIVATE_CONTENT_LEAK:{forbidden}")
        if re.search(r"\bsk-[A-Za-z0-9_-]{8,}", html_text):
            errors.append("PRIVATE_CONTENT_LEAK:sk-")
        if "http://" in lowered or "https://" in lowered:
            errors.append("EXTERNAL_RESOURCE_REQUEST")
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1080, "height": 1350})
                requests: list[str] = []
                page.on("request", lambda request: requests.append(request.url) if request.url.startswith(("http://", "https://")) else None)
                page.goto(html_path.resolve().as_uri(), wait_until="load")
                external_requests = len(requests)
                if external_requests:
                    errors.append("EXTERNAL_RESOURCE_REQUEST")
                section_info = page.evaluate("""required => {
                    const card = document.querySelector('.card');
                    const cardRect = card ? card.getBoundingClientRect() : null;
                    const result = {card: card ? {clientWidth: card.clientWidth, scrollWidth: card.scrollWidth, clientHeight: card.clientHeight, scrollHeight: card.scrollHeight} : null, sections: {}};
                    for (const name of required) {
                        const node = document.querySelector(`[data-section="${name}"]`);
                        const rect = node ? node.getBoundingClientRect() : null;
                        result.sections[name] = node && rect ? {width: rect.width, height: rect.height, left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom, scrollWidth: node.scrollWidth, clientWidth: node.clientWidth, scrollHeight: node.scrollHeight, clientHeight: node.clientHeight} : null;
                    }
                    return result;
                }""", list(REQUIRED_SECTIONS))
                card = section_info.get("card")
                if not card or card["scrollWidth"] > card["clientWidth"] or card["scrollHeight"] > card["clientHeight"]:
                    overflow = True
                    errors.append("VISUAL_OVERFLOW")
                for section in REQUIRED_SECTIONS:
                    info = section_info.get("sections", {}).get(section)
                    if not info or info["width"] <= 0 or info["height"] <= 0:
                        errors.append(f"MISSING_SECTION:{section}")
                        continue
                    sections[section] = True
                    if info["left"] < 0 or info["top"] < 0 or info["right"] > 1080 or info["bottom"] > 1350:
                        errors.append(f"SECTION_OUT_OF_BOUNDS:{section}")
                    if info["scrollWidth"] > info["clientWidth"] or info["scrollHeight"] > info["clientHeight"]:
                        text_clipping.append(section)
                        errors.append(f"TEXT_CLIPPED:{section}")
                browser.close()
        except Exception as exc:
            errors.append(f"PLAYWRIGHT_VALIDATION_ERROR:{type(exc).__name__}")
    if view_model:
        html_symbols = set(re.findall(r"\b[A-Z][A-Z0-9]{1,5}\b", html_text))
        display_presence = {"GOLD": "זהב", "US10Y": "תשואת אג\"ח ארה\"ב ל־10 שנים"}
        for symbol in ("SPY", "QQQ", "DIA", "IWM", "VIX", "GOLD", "WTI", "DXY", "US10Y"):
            if symbol not in html_symbols and symbol not in display_presence and symbol not in html_visible_text:
                errors.append(f"MISSING_DATA:{symbol}")
            elif symbol in display_presence and display_presence[symbol] not in html_visible_text and symbol not in html_visible_text:
                errors.append(f"MISSING_DATA:{symbol}")
        if not view_model["gainers"]:
            errors.append("MISSING_DATA:GAINERS")
        if not view_model["losers"]:
            errors.append("MISSING_DATA:LOSERS")
        if "positive" not in html_text or "negative" not in html_text or "neutral" not in html_text:
            errors.append("SEMANTIC_COLOR_CLASSES_MISSING")
        for item in view_model["indices"] + view_model["top_sectors"] + view_model["bottom_sectors"]:
            if item["class"] in {"positive", "negative"} and f'class="{item["class"]}"' not in html_text:
                errors.append(f"SEMANTIC_COLOR_MISSING:{item['symbol']}")
        readiness = data.get("publication_readiness", {}).get("status")
        if view_model["readiness"] != READINESS_LABELS.get(readiness, readiness):
            errors.append("READINESS_MISMATCH")
        for warning in view_model["warnings"]:
            if warning not in html_text:
                errors.append(f"MISSING_WARNING:{warning}")
        for symbol, item in data.get("macro", {}).items():
            if item.get("freshness") == "lagging":
                observation = format_date(item.get("latest_observation_date") or item.get("latest_bar_datetime"))
                if observation and observation not in html_text:
                    errors.append(f"MISSING_LAGGING_DATE:{symbol}")
    result = {"valid": not errors, "dimensions": {"width": metadata.get("width"), "height": metadata.get("height")}, "sections": sections, "overflow": overflow, "text_clipping": text_clipping, "external_requests": external_requests, "errors": sorted(set(errors)), "warnings": warnings}
    manifest_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def is_visual_publishable(result: dict[str, Any], report_data: dict[str, Any] | None = None) -> bool:
    """Return true only when structural validation and report readiness both pass."""
    if report_data and report_data.get("publication_readiness", {}).get("status") == "NOT_READY":
        return False
    return bool(result.get("valid"))
