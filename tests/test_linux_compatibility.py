import ast
from pathlib import Path

import requests

from validate_linux_compatibility import validate_fixture


def test_daily_entrypoint_does_not_import_windows_scheduler_or_power_api():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "run_daily.py").read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert not any(
        name in {"ctypes", "run_daily_scheduled", "utils.windows_power"}
        for name in imported
    )


def test_fixture_validation_generates_hebrew_1080x1600_card_offline(tmp_path, monkeypatch):
    def no_http(*args, **kwargs):
        raise AssertionError("fixture validation must not make HTTP requests")

    monkeypatch.setattr(requests, "get", no_http)
    monkeypatch.setattr(requests, "post", no_http)
    output = tmp_path / "folder with spaces" / "phase 1"
    result = validate_fixture(output)

    html = Path(result["poster_html"]).read_text(encoding="utf-8")
    report = Path(result["report_markdown"]).read_text(encoding="utf-8")
    assert result["mode"] == "OFFLINE_FIXTURE_ONLY"
    assert result["network_calls"] == 0
    assert result["telegram_sent"] is False
    assert result["visual_validation"]["valid"] is True
    assert result["visual_validation"]["dimensions"] == {"width": 1080, "height": 1600}
    assert result["local_images_loaded"] is True
    assert result["runtime"]["browser"]["version"]
    assert result["runtime"]["dependencies"]["playwright"]
    assert 'lang="he" dir="rtl"' in html
    assert "KLXE" in html and "SEC EDGAR" in html
    assert "עליות" in report or "ירידות" in report
    assert Path(result["poster_png"]).is_file()
    assert result["visual_validation"]["rtl"] is True


def test_cloud_fixture_renderer_uses_local_asset_paths_for_nested_output(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = validate_fixture(tmp_path / "nested" / "output")
    html = Path(result["poster_html"]).read_text(encoding="utf-8")
    for filename in ("hero_wall_street.png", "story_energy.png"):
        assert f"{(root / 'visual' / 'assets' / 'v3').as_uri()}/{filename}" in html
    assert (root / "visual" / "assets" / "v3").is_dir()
