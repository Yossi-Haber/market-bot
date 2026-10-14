"""Offline regression tests for the richer V2 market card."""

import json
from pathlib import Path

from visual.builder_v2 import build_view_model_v2, render_html_v2


FIXTURE = Path(__file__).parent / "fixtures" / "report_data.json"


def model():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return build_view_model_v2(data, "## תמונת שוק\n\nהשוק נסגר במגמה חיובית ברובה.", {"selected_source": "LLM"})


def test_v2_hero_drivers_and_stories_are_generated():
    value = model()
    assert value["headline"]
    assert value["subheadline"]
    assert 1 <= len(value["drivers"]) <= 5
    assert value["stories"] == []


def test_v2_hides_unconfirmed_leads_and_provider_symbols():
    document = render_html_v2(model())
    assert "unconfirmed_leads" not in document
    assert "not_confirmed" not in document
    assert "XAU/USD" not in document
    assert "https://" not in document


def test_v2_contains_editorial_sections_and_display_values():
    document = render_html_v2(model())
    for section in ("header", "core", "breadth", "drivers", "sectors", "macro", "stories", "movers", "footer"):
        assert f'data-section="{section}"' in document
    assert "771.35" in document
    assert "+0.54%" in document
    assert "נכון ל־22.09.2026" in document


def test_v2_validation_contract_uses_moderate_tall_canvas():
    from visual.validate_v2 import V2_SECTIONS
    assert len(V2_SECTIONS) == 9


def test_current_v2_artifact_passes_validation_and_footer_is_in_bounds():
    from visual.validate_v2 import validate_visual_report_v2
    result = validate_visual_report_v2(
        Path(__file__).parent.parent / "output" / "daily_report_card_v2.html",
        Path(__file__).parent.parent / "output" / "daily_report_card_v2.png",
    )
    assert result["valid"], result["errors"]
    assert result["sections"]["footer"] is True
    assert result["overflow"] is False
