"""Offline structural validation tests for the visual publish artifact."""

import json
import struct
from pathlib import Path

from visual.validate import validate_visual_report
from visual.builder import build_view_model, render_html
from visual.screenshot import export_png


ROOT = Path(__file__).resolve().parent.parent
def copy_inputs(tmp_path):
    return render_explicit_fixture(tmp_path)


def validate(paths):
    return validate_visual_report(paths["daily_report_card.html"], paths["daily_report_card.png"], paths["daily_report_data.json"], paths["daily_report_final.md"], paths["daily_report_selection.json"], paths["manifest.json"])


def render_explicit_fixture(tmp_path):
    fixture = ROOT / "tests" / "fixtures" / "report_data.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    markdown = "## תמונת מצב\nתנודתיות מתונה."
    selection = {"selected_source": "DETERMINISTIC"}
    html_path, png_path = tmp_path / "daily_report_card.html", tmp_path / "daily_report_card.png"
    view_model = build_view_model(data, markdown, selection)
    html_path.write_text(render_html(view_model), encoding="utf-8")
    export_png(html_path, png_path, height=1350)
    data_path = tmp_path / "daily_report_data.json"
    data_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    md_path = tmp_path / "daily_report_final.md"
    md_path.write_text(markdown, encoding="utf-8")
    selection_path = tmp_path / "daily_report_selection.json"
    selection_path.write_text(json.dumps(selection), encoding="utf-8")
    lagging_marker = next(item["date_note"] for item in view_model["macro"] if item["symbol"] == "WTI")
    return {"daily_report_card.html": html_path, "daily_report_card.png": png_path, "daily_report_data.json": data_path, "daily_report_final.md": md_path, "daily_report_selection.json": selection_path, "manifest.json": tmp_path / "manifest.json", "lagging_marker": lagging_marker}


def test_current_visual_card_passes(tmp_path):
    result = validate(render_explicit_fixture(tmp_path))
    assert result["valid"], result["errors"]
    assert result["dimensions"] == {"width": 1080, "height": 1350}
    assert result["overflow"] is False
    assert result["external_requests"] == 0


def test_wrong_dimensions_fail(tmp_path):
    paths = copy_inputs(tmp_path)
    raw = bytearray(paths["daily_report_card.png"].read_bytes())
    raw[16:20] = struct.pack(">I", 999)
    paths["daily_report_card.png"].write_bytes(raw)
    assert "PNG_WRONG_DIMENSIONS" in validate(paths)["errors"]


def test_missing_section_fails(tmp_path):
    paths = copy_inputs(tmp_path)
    paths["daily_report_card.html"].write_text(paths["daily_report_card.html"].read_text(encoding="utf-8").replace('data-section="macro"', 'data-section="missing"'), encoding="utf-8")
    assert "MISSING_SECTION:macro" in validate(paths)["errors"]


def test_overflow_fails(tmp_path):
    paths = copy_inputs(tmp_path)
    html = paths["daily_report_card.html"].read_text(encoding="utf-8")
    html = html.replace("</main>", '<div style="width:2000px">overflow</div></main>')
    paths["daily_report_card.html"].write_text(html, encoding="utf-8")
    assert "VISUAL_OVERFLOW" in validate(paths)["errors"]


def test_external_resource_fails(tmp_path):
    paths = copy_inputs(tmp_path)
    paths["daily_report_card.html"].write_text(paths["daily_report_card.html"].read_text(encoding="utf-8") + '<img src="https://example.com/image.png">', encoding="utf-8")
    assert "EXTERNAL_RESOURCE_REQUEST" in validate(paths)["errors"]


def test_private_leakage_fails(tmp_path):
    paths = copy_inputs(tmp_path)
    paths["daily_report_card.html"].write_text(paths["daily_report_card.html"].read_text(encoding="utf-8") + " unconfirmed_leads", encoding="utf-8")
    assert any(error.startswith("PRIVATE_CONTENT_LEAK") for error in validate(paths)["errors"])


def test_missing_lagging_date_fails(tmp_path):
    paths = copy_inputs(tmp_path)
    html = paths["daily_report_card.html"].read_text(encoding="utf-8").replace(paths["lagging_marker"], "")
    paths["daily_report_card.html"].write_text(html, encoding="utf-8")
    assert "MISSING_LAGGING_DATE:WTI" in validate(paths)["errors"]
