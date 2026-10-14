from pathlib import Path

import json
from visual.screenshot import export_png
from visual.builder_v3 import build_view_model_v3, render_html_v3
from visual.validate_v3 import validate_visual_report_v3


FIXTURE = Path(__file__).parent / "fixtures" / "report_data.json"


def _render(tmp_path, *, notices=None, stories=True):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["market_date"] = "2026-09-28"
    data["top_stories"] = [{"title_he": "KLXE העלתה את תחזית ההכנסות לרבעון השלישי", "summary_he": "תחזית החברה לרבעון השלישי עודכנה.", "category": "EARNINGS", "published_at": "2026-09-28T16:00:00Z", "primary_source_name": "SEC EDGAR", "affected_symbols": ["KLXE"], "verification": "VERIFIED", "editorial_eligible": True}] if stories else []
    data["unexplained_move_notices"] = notices or []
    selection = {"selected_source": "DETERMINISTIC"}
    markdown = "## תמונת מצב\nשוק מעורב."
    model = build_view_model_v3(data, markdown, selection)
    text = render_html_v3(model)
    html_path, png_path = tmp_path / "card.html", tmp_path / "card.png"
    html_path.write_text(text, encoding="utf-8")
    export_png(html_path, png_path, height=1600)
    return model, text, html_path, png_path


def test_v3_has_editorial_grid_and_local_visuals(tmp_path):
    model, text, _, _ = _render(tmp_path)
    for section in ("header", "market-strip", "feature", "breadth", "sectors", "macro", "movers", "footer"):
        assert f'data-section="{section}"' in text
    assert "<svg" in text
    assert "KLXE" in text
    assert "SEC EDGAR" in text
    assert "https://" not in text
    assert len(model["market_tiles"]) == 5
    assert "GAINERS" in text and "LOSERS" in text
    assert "macro-icon" in text and "sector-icon" in text
    assert "../visual/assets/v3/hero_wall_street.png" in text
    assert "../visual/assets/v3/story_energy.png" in text


def test_v3_preserves_macro_and_movers_data(tmp_path):
    model, text, _, _ = _render(tmp_path)
    assert {item["symbol"] for item in model["macro"]} == {"VIX", "GOLD", "WTI", "DXY", "US10Y"}
    assert "עליות" in text and "ירידות" in text


def test_v3_generated_artifacts_validate(tmp_path):
    _, _, html_path, png_path = _render(tmp_path)
    result = validate_visual_report_v3(html_path, png_path)
    assert result["valid"], result
    assert result["dimensions"] == {"width": 1080, "height": 1600}
    assert not result["overflow"]


def test_v3_displays_unexplained_moves_without_displacing_verified_story(tmp_path):
    notice = {"status": "UNEXPLAINED", "industry": "COMMUNICATION_SERVICES", "industry_he": "שירותי התקשורת", "symbols": ["T", "VZ"], "message_he": "מניות שירותי התקשורת: AT&T (T) ירדה ב־10.86%; Verizon (VZ) ירדה ב־10.14%. לא אומת הגורם."}
    _, text, html_path, png_path = _render(tmp_path, notices=[notice])
    assert "KLXE" in text and "SEC EDGAR" in text
    assert "UNEXPLAINED" in text and "T" in text and "VZ" in text
    assert "Starlink" not in text
    assert "לא נמצאו היום סיפורים מאומתים בעלי השפעה מהותית" not in text
    result = validate_visual_report_v3(html_path, png_path)
    assert result["valid"], result


def test_v3_empty_story_area_distinguishes_unpromoted_from_unexplained(tmp_path):
    notice = {"status": "UNEXPLAINED", "symbols": ["T", "VZ"], "message_he": "תנועה משמעותית נותרה ללא גורם מאומת."}
    _, text, _, _ = _render(tmp_path, notices=[notice], stories=False)
    assert "לא קודמו סיפורים מאומתים היום" in text
    assert "תנועה משמעותית נותרה ללא גורם מאומת" in text
