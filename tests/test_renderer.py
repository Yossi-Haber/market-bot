"""Tests for the deterministic Hebrew Markdown renderer."""

import json
from pathlib import Path

import pytest

from render.markdown import HEADLINES, ReportNotReadyError, load_report_data, render_from_file, render_report
from render.formatting import format_bps, format_pct
from llm.input_builder import build_llm_payload


FIXTURE = Path(__file__).parent / "fixtures" / "report_data.json"


def fixture_data():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("direction, headline", HEADLINES.items())
def test_all_headline_mappings(direction, headline):
    data = fixture_data()
    data["market_overview"]["market_direction"] = direction
    assert headline in render_report(data)


def test_formatting_is_signed_and_rounded():
    assert format_pct(0.543548) == "+0.54%"
    assert format_pct(-3.893111) == "-3.89%"
    assert format_pct(0) == "+0.00%"
    assert format_bps(7.0) == "+7 נ\"ב"
    assert "0.543548" not in render_report(fixture_data())


def test_sections_include_proxy_note_sorting_and_lagging_dates():
    text = render_report(fixture_data())
    assert "ETF proxies" in text
    assert "8 מתוך 11" not in text  # fixture intentionally has 2/3 breadth
    assert "2 מתוך 3" in text
    assert text.index("PYPL") < text.index("PANW")
    assert "נתון מעודכן ל־2026-09-22" in text
    assert "+7 נ\"ב" in text


def test_unconfirmed_content_is_not_rendered():
    data = fixture_data()
    text = render_report(data)
    assert "NOT_CONFIRMED" not in text
    assert "unconfirmed_leads" not in text
    assert "רכישה" not in text


def test_warning_text_is_rendered():
    assert "כיסוי החדשות והקטליסטים אינו מלא." not in render_report(fixture_data())
    data = fixture_data()
    data["data_quality"]["warnings"].append("CATALYST_COVERAGE_INCOMPLETE")
    assert "כיסוי החדשות והקטליסטים אינו מלא." in render_report(data)


def test_not_ready_prevents_public_report(monkeypatch, tmp_path):
    data = fixture_data()
    data["publication_readiness"]["status"] = "NOT_READY"
    source = tmp_path / "daily_report_data.json"
    source.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ReportNotReadyError):
        load_report_data(source)
    monkeypatch.setattr("render.markdown.OUTPUT_DIR", tmp_path)
    assert render_from_file(source) is None
    assert not (tmp_path / "daily_report.md").exists()
