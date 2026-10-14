"""Generate the isolated V2 daily market card from local report artifacts."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from visual.builder import OUTPUT_DIR, load_json
from visual.builder_v2 import build_view_model_v2, render_html_v2
from visual.screenshot import export_png
from visual.validate_v2 import validate_visual_report_v2


def main() -> None:
    """Write current and timestamped V2 HTML/PNG artifacts without network calls."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s visual-v2 - %(message)s")
    input_path = (OUTPUT_DIR / "daily_report_data.json").resolve()
    data = load_json(input_path)
    top_stories = data.get("top_stories")
    top_stories = top_stories if isinstance(top_stories, list) else []
    logging.info("Visual V2 input: %s", input_path)
    logging.info("top_stories_count=%d", len(top_stories))
    logging.info("top_story_titles=%s", [item.get("title_he") or item.get("title") or item.get("headline") for item in top_stories if isinstance(item, dict)])
    selection = load_json(OUTPUT_DIR / "daily_report_selection.json")
    markdown = (OUTPUT_DIR / "daily_report_final.md").read_text(encoding="utf-8")
    model = build_view_model_v2(data, markdown, selection)
    text = render_html_v2(model)
    html_path = OUTPUT_DIR / "daily_report_card_v2.html"
    png_path = OUTPUT_DIR / "daily_report_card_v2.png"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    history_html = OUTPUT_DIR / f"daily_report_card_v2_{stamp}.html"
    history_png = OUTPUT_DIR / f"daily_report_card_v2_{stamp}.png"
    html_path.write_text(text, encoding="utf-8")
    history_html.write_text(text, encoding="utf-8")
    if top_stories:
        rendered_stories = model.get("stories", [])
        if not rendered_stories:
            raise RuntimeError("Visual V2 lost top_stories during build")
        first_rendered = rendered_stories[0]
        first_title = str(first_rendered.get("title") or "")
        first_source = str(first_rendered.get("text") or "")
        if first_title and first_title not in text:
            raise AssertionError("Visual V2 generated HTML is missing the first top story title")
        source_name = str(top_stories[0].get("primary_source_name") or top_stories[0].get("source_name") or "")
        if source_name and source_name not in text and source_name not in first_source:
            raise AssertionError("Visual V2 generated HTML is missing the first top story source")
        if "לא נמצאו היום סיפורים מאומתים בעלי השפעה מהותית" in text:
            raise AssertionError("Visual V2 rendered empty state with non-empty top_stories")
    generated_at = datetime.now(timezone.utc).isoformat()
    logging.info("Visual V2 HTML generated_at=%s path=%s", generated_at, html_path.resolve())
    logging.info("Visual V2 screenshot_html_path=%s", html_path.resolve())
    logging.info("Visual V2 PNG path=%s", png_path.resolve())
    export_png(html_path, png_path, height=1600)
    export_png(html_path, history_png, height=1600)
    validation = validate_visual_report_v2(html_path, png_path, manifest_path=OUTPUT_DIR / "daily_report_v2_visual_validation.json")
    logging.info("V2 visual report created: %s", html_path)
    logging.info("V2 visual validation: %s", "PASS" if validation["valid"] else "FAIL")


if __name__ == "__main__":
    main()
