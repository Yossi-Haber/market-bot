"""Offline Playwright screenshot helper."""

from __future__ import annotations

from pathlib import Path


def export_png(html_path: Path, png_path: Path, width: int = 1080, height: int = 1350) -> None:
    """Open a local HTML file in headless Playwright and capture the card."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("PLAYWRIGHT_NOT_INSTALLED") from exc
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
        page = context.new_page()
        page.goto(html_path.resolve().as_uri(), wait_until="load")
        page.screenshot(path=str(png_path), full_page=False)
        context.close()
        browser.close()
