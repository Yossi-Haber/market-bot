"""Generate the isolated V3 editorial daily market card."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from visual.builder import OUTPUT_DIR, load_json
from visual.builder_v3 import build_view_model_v3, render_html_v3
from visual.screenshot import export_png
from visual.validate_v3 import validate_visual_report_v3


def main() -> None:
    """Generate current and timestamped V3 HTML/PNG artifacts without network calls."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s visual-v3 - %(message)s")
    input_path = (OUTPUT_DIR / "daily_report_data.json").resolve()
    data = load_json(input_path)
    stories = data.get("top_stories") if isinstance(data.get("top_stories"), list) else []
    logging.info("Visual V3 input: %s", input_path)
    logging.info("top_stories_count=%d", len(stories))
    selection = load_json(OUTPUT_DIR / "daily_report_selection.json")
    markdown = (OUTPUT_DIR / "daily_report_final.md").read_text(encoding="utf-8")
    model = build_view_model_v3(data, markdown, selection)
    text = render_html_v3(model)
    html_path = OUTPUT_DIR / "daily_report_card_v3.html"
    png_path = OUTPUT_DIR / "daily_report_card_v3.png"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    history_html = OUTPUT_DIR / f"daily_report_card_v3_{stamp}.html"
    history_png = OUTPUT_DIR / f"daily_report_card_v3_{stamp}.png"
    html_path.write_text(text, encoding="utf-8")
    history_html.write_text(text, encoding="utf-8")
    if stories and (not model.get("feature_story") or str(model["feature_story"].get("title", "")) not in text):
        raise RuntimeError("V3 feature story was lost during rendering")
    logging.info("Visual V3 HTML generated_at=%s path=%s", datetime.now(timezone.utc).isoformat(), html_path.resolve())
    logging.info("Visual V3 screenshot_html_path=%s", html_path.resolve())
    logging.info("Visual V3 PNG path=%s", png_path.resolve())
    export_png(html_path, png_path, height=1600)
    export_png(html_path, history_png, height=1600)
    validation = validate_visual_report_v3(html_path, png_path, manifest_path=OUTPUT_DIR / "daily_report_v3_visual_validation.json")
    logging.info("V3 visual validation: %s", "PASS" if validation["valid"] else "FAIL")
    if not validation["valid"]:
        raise RuntimeError("V3 visual validation failed: " + ", ".join(validation["errors"]))


if __name__ == "__main__":
    main()
