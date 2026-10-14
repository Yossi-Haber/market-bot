import json
from pathlib import Path

from llm.guardrails import validate_llm_output
from llm.input_builder import build_llm_payload
from report.composer import merge_verified_stories, select_verified_live_stories
from visual.builder_v2 import build_view_model_v2, render_html_v2


def story(verified="VERIFIED", eligible=True):
    return {
        "title": "KLXE Raises Q3 Revenue Guidance",
        "summary": "KLXE updated third-quarter revenue guidance.",
        "category": "EARNINGS",
        "affected_symbols": ["KLXE"],
        "published_at": "2026-09-25T12:30:00Z",
        "verification": verified,
        "editorial_eligible": eligible,
        "primary_source_name": "SEC EDGAR",
        "primary_source_domain": "sec.gov",
        "primary_source_url": "https://www.sec.gov/Archives/edgar/data/1/filing.htm",
        "primary_source_type": "OFFICIAL",
        "supporting_sources_count": 1,
    }


def test_daily_report_live_selection_excludes_unconfirmed(tmp_path):
    path = tmp_path / "live_news_v3_snapshot.json"
    path.write_text(json.dumps({"market_date": "2026-09-25", "stories": [story(), story("NOT_CONFIRMED", False)]}), encoding="utf-8")
    selected = select_verified_live_stories(path, "2026-09-25")
    assert len(selected) == 1
    assert selected[0]["primary_source_domain"] == "sec.gov"
    assert merge_verified_stories([], selected)[0]["verification"] == "VERIFIED"


def test_visual_v2_renders_verified_story_with_official_source():
    fixture = Path(__file__).parent / "fixtures" / "report_data.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["verified_stories"] = [story()]
    data["movers"]["top_gainers"].append({"symbol": "KLXE", "company_name": "KLXE Inc."})
    data["top_stories"] = [story()]
    model = build_view_model_v2(data, "# דוח", {"selected_source": "DETERMINISTIC"})
    document = render_html_v2(model)
    assert model["drivers"][0] == "KLXE העלתה את תחזית ההכנסות לרבעון השלישי"
    assert "KLXE" in document
    assert "SEC EDGAR" in document
    assert "gurufocus.com" not in document
    assert "KLXE Raises Q3 Revenue Guidance" not in model["drivers"]
    assert "TOP / BOTTOM" not in document
    assert "compact" not in document
    assert "לא נמצאו היום סיפורים מאומתים בעלי השפעה מהותית" not in document


def test_single_story_uses_wide_editorial_layout_and_compact_card():
    fixture = Path(__file__).parent / "fixtures" / "report_data.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["top_stories"] = [story()]
    model = build_view_model_v2(data, "# דוח", {"selected_source": "DETERMINISTIC"})
    document = render_html_v2(model)
    assert model["story_grid_class"] == "story-count-1"
    assert "stories-grid story-count-1" in document
    assert document.count('class="story-card accent"') == 1


def test_visual_v2_uses_top_stories_only_and_renders_multiple_cards():
    fixture = Path(__file__).parent / "fixtures" / "report_data.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    first = story()
    second = {**story(), "title_he": "זהב עלה", "summary_he": "זהב התחזק במהלך המסחר.", "category": "COMMODITIES", "affected_symbols": ["GOLD"], "primary_source_name": "BLS"}
    data["verified_stories"] = []
    data["top_stories"] = [first, second]
    model = build_view_model_v2(data, "# דוח", {"selected_source": "DETERMINISTIC"})
    document = render_html_v2(model)
    assert document.count('class="story-card accent"') == 2
    assert model["story_grid_class"] == "story-count-2"
    assert "לא נמצאו היום סיפורים מאומתים בעלי השפעה מהותית" not in document


def test_visual_v2_empty_state_requires_empty_top_stories():
    fixture = Path(__file__).parent / "fixtures" / "report_data.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["verified_stories"] = [story()]
    data["top_stories"] = []
    model = build_view_model_v2(data, "# דוח", {"selected_source": "DETERMINISTIC"})
    document = render_html_v2(model)
    assert "לא נמצאו היום סיפורים מאומתים בעלי השפעה מהותית" in document
    assert model["story_grid_class"] == "story-count-0"


def test_visual_v2_preserves_hebrew_title_and_summary():
    fixture = Path(__file__).parent / "fixtures" / "report_data.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    item = {**story(), "title_he": "KLXE העלתה את תחזית ההכנסות", "summary_he": "החברה עדכנה את התחזית.", "affected_symbols": ["KLXE"]}
    data["top_stories"] = [item]
    model = build_view_model_v2(data, "# דוח", {"selected_source": "DETERMINISTIC"})
    document = render_html_v2(model)
    assert "KLXE העלתה את תחזית ההכנסות" in document
    assert "החברה עדכנה את התחזית" in document


def test_llm_payload_contains_verified_story_and_guardrails_reject_causality():
    fixture = Path(__file__).parent / "fixtures" / "report_data.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["verified_stories"] = [story()]
    data["movers"]["top_gainers"].append({"symbol": "KLXE", "company_name": "KLXE Inc."})
    payload = build_llm_payload(data)
    assert payload["verified_stories"][0]["primary_source_domain"] == "sec.gov"
    result = validate_llm_output("KLXE עלתה בגלל העסקה", data, payload)
    assert not result.valid
    assert any(error.startswith("UNVERIFIED_CAUSAL_CLAIM") for error in result.errors)
