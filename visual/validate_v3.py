"""Offline structural and safety validation for the V3 card."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .validate import png_metadata

V3_SECTIONS = ("header", "market-strip", "feature", "breadth", "sectors", "macro", "movers", "footer")
FORBIDDEN = ("unconfirmed_leads", "not_confirmed", "authorization", "openai_api_key", "twelve_data_api_key", "rejected_llm_output")


def validate_visual_report_v3(html_path: Path, png_path: Path, *, manifest_path: Path | None = None) -> dict[str, Any]:
    """Validate local resources, dimensions, section bounds, and clipping."""
    errors: list[str] = []
    metadata = png_metadata(png_path)
    if not metadata["exists"]: errors.append("PNG_MISSING")
    elif not metadata["readable"]: errors.append("PNG_UNREADABLE")
    elif (metadata["width"], metadata["height"]) != (1080, 1600): errors.append("PNG_WRONG_DIMENSIONS")
    text = html_path.read_text(encoding="utf-8") if html_path.exists() else ""
    if not html_path.exists(): errors.append("HTML_MISSING")
    lower = text.lower()
    if any(item in lower for item in FORBIDDEN): errors.append("PRIVATE_CONTENT_LEAK")
    if "http://" in lower or "https://" in lower: errors.append("EXTERNAL_RESOURCE_REQUEST")
    sections = {name: False for name in V3_SECTIONS}
    overflow = False
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1080, "height": 1600})
            requests: list[str] = []
            page.on("request", lambda request: requests.append(request.url) if request.url.startswith(("http://", "https://")) else None)
            page.goto(html_path.resolve().as_uri(), wait_until="load")
            if requests: errors.append("EXTERNAL_RESOURCE_REQUEST")
            result = page.evaluate("""names => { const card=document.querySelector('.poster-v3'); const root=document.documentElement; const r={card:card?{w:card.clientWidth,sw:card.scrollWidth,h:card.clientHeight,sh:card.scrollHeight}:null,rtl:root.dir==='rtl' && root.lang==='he',sections:{}}; for(const n of names){const x=document.querySelector(`[data-section="${n}"]`); const b=x?.getBoundingClientRect(); r.sections[n]=b?{w:b.width,h:b.height,l:b.left,t:b.top,r:b.right,bot:b.bottom,sw:x.scrollWidth,cw:x.clientWidth,sh:x.scrollHeight,ch:x.clientHeight}:null;} return r; }""", list(V3_SECTIONS))
            if not result.get("rtl"): errors.append("HEBREW_RTL_NOT_CONFIGURED")
            card = result.get("card")
            if not card or card["sw"] > card["w"] or card["sh"] > card["h"]: errors.append("VISUAL_OVERFLOW"); overflow = True
            for name, info in result.get("sections", {}).items():
                if not info or info["w"] <= 0 or info["h"] <= 0: errors.append(f"MISSING_SECTION:{name}")
                else:
                    sections[name] = True
                    if info["l"] < 0 or info["t"] < 0 or info["r"] > 1080 or info["bot"] > 1600: errors.append(f"SECTION_OUT_OF_BOUNDS:{name}")
                    if info["sw"] > info["cw"] or info["sh"] > info["ch"]: errors.append(f"TEXT_CLIPPED:{name}")
            browser.close()
    except Exception as exc: errors.append(f"PLAYWRIGHT_VALIDATION_ERROR:{type(exc).__name__}")
    result = {"valid": not errors, "dimensions": {"width": metadata.get("width"), "height": metadata.get("height")}, "rtl": bool(locals().get("result", {}).get("rtl", False)), "sections": sections, "overflow": overflow, "external_requests": 0, "errors": sorted(set(errors))}
    if manifest_path: manifest_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result
