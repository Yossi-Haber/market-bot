"""Generate the offline HTML and PNG daily market card."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from visual.builder import OUTPUT_DIR, build_view_model, load_json, render_html
from visual.screenshot import export_png
from visual.validate import validate_visual_report


def main() -> None:
    """Build current and timestamped visual artifacts from local report files."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s visual - %(message)s")
    data = load_json(OUTPUT_DIR / "daily_report_data.json")
    selection = load_json(OUTPUT_DIR / "daily_report_selection.json")
    final_markdown = (OUTPUT_DIR / "daily_report_final.md").read_text(encoding="utf-8")
    view_model = build_view_model(data, final_markdown, selection)
    html_text = render_html(view_model)
    html_path = OUTPUT_DIR / "daily_report_card.html"
    png_path = OUTPUT_DIR / "daily_report_card.png"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    history_html = OUTPUT_DIR / f"daily_report_card_{stamp}.html"
    history_png = OUTPUT_DIR / f"daily_report_card_{stamp}.png"
    html_path.write_text(html_text, encoding="utf-8")
    history_html.write_text(html_text, encoding="utf-8")
    export_png(html_path, png_path)
    export_png(html_path, history_png)
    validation = validate_visual_report()
    logging.info("Visual report created: %s", html_path)
    logging.info("Visual PNG created: %s", png_path)
    logging.info("Visual publish validation: %s", "PASS" if validation["valid"] else "FAIL")


if __name__ == "__main__":
    main()
