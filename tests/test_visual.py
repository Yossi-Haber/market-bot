"""Lightweight tests for the offline visual report builder."""

import json
from pathlib import Path

from visual.builder import build_view_model, extract_narrative, render_html


FIXTURE = Path(__file__).parent / "fixtures" / "report_data.json"


def fixture_data():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_narrative_extraction_is_compact():
    text = "## תמונת שוק\n\nהשוק נסגר בעליות.\n\n- SPY נסגר על 771.35.\n\n## סקטורים\n"
    assert extract_narrative(text) == "השוק נסגר בעליות."


def test_view_model_maps_readiness_warnings_and_hides_leads():
    data = fixture_data()
    model = build_view_model(data, "## תמונת שוק\n\nהשוק נסגר בעליות.", {"selected_source": "DETERMINISTIC"})
    assert model["source"] == "Deterministic"
    assert model["readiness"] == "מוכן לפרסום עם הסתייגויות"
    assert "חלק מנתוני המאקרו בפיגור" in model["warnings"]
    assert "unconfirmed_leads" not in json.dumps(model, ensure_ascii=False)


def test_html_generation_is_local_and_complete():
    data = fixture_data()
    model = build_view_model(data, "## תמונת שוק\n\nהשוק נסגר בעליות.", {"selected_source": "LLM"})
    document = render_html(model)
    assert "סיכום שוק יומי" in document
    assert "ETF PROXIES" in document
    assert "XAU/USD" not in document
    assert "unconfirmed_leads" not in document
    assert "http://" not in document
    assert "https://" not in document
    assert "{{" not in document
