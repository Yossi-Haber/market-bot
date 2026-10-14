"""Render and validate a saved report fixture without network or delivery."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
from pathlib import Path
import subprocess
import sys
from typing import Any

from render.markdown import render_report
from visual.builder_v3 import build_view_model_v3, render_html_v3
from visual.validate_v3 import validate_visual_report_v3
from visual.screenshot import export_png


ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "tests" / "fixtures" / "report_data.json"
STORY_FIXTURE = ROOT / "tests" / "fixtures" / "linux_compatibility_story.json"
DEFAULT_OUTPUT = ROOT / "output" / "qa" / "github_actions_migration_phase1" / "fixture_validation"
ASSET_DIR = ROOT / "visual" / "assets" / "v3"


def _local_images_loaded(html_path: Path) -> dict[str, Any]:
    """Ensure both bundled poster images resolve from the generated HTML path."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        browser_version = browser.version
        page = browser.new_page(viewport={"width": 1080, "height": 1600})
        page.goto(html_path.resolve().as_uri(), wait_until="load")
        loaded = page.locator(".generated-visual img").evaluate_all(
            "images => images.length === 2 && images.every(image => image.complete && image.naturalWidth > 0)"
        )
        browser.close()
    return {"loaded": bool(loaded), "chromium_version": browser_version}


def _font_version(pattern: str) -> str:
    """Return the resolved local font face without requiring a specific OS."""
    try:
        completed = subprocess.run(
            ["fc-match", "-f", "%{family} | %{style} | %{file}", pattern],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return completed.stdout.strip() or "NOT_RESOLVED"
    except (FileNotFoundError, subprocess.SubprocessError):
        return "FONTCONFIG_UNAVAILABLE"


def _runtime_metadata(chromium_version: str) -> dict[str, Any]:
    """Capture exact runtime versions for reproducible CI diagnostics."""
    packages = ("requests", "python-dotenv", "openai", "playwright", "pytest")
    versions: dict[str, str] = {}
    for package in packages:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "NOT_INSTALLED"
    os_release: dict[str, str] = {}
    release_file = Path("/etc/os-release")
    if release_file.exists():
        for line in release_file.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                os_release[key] = value.strip().strip('"')
    return {
        "os": {"platform": platform.platform(), "release": os_release},
        "python": sys.version,
        "browser": {"name": "Playwright Chromium", "version": chromium_version},
        "dependencies": versions,
        "fonts": {
            "Noto Hebrew": _font_version("Noto Sans Hebrew"),
            "Liberation Sans": _font_version("Liberation Sans"),
        },
    }


def validate_fixture(output_dir: Path = DEFAULT_OUTPUT, fixture_path: Path = FIXTURE) -> dict[str, Any]:
    """Create a fixture-backed Hebrew Markdown report and V3 poster locally."""
    output_dir = output_dir.resolve()
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("FIXTURE_MUST_BE_JSON_OBJECT")
    story = json.loads(STORY_FIXTURE.read_text(encoding="utf-8"))
    data["top_stories"] = [story]
    data["verified_stories"] = [story]
    output_dir.mkdir(parents=True, exist_ok=True)

    report_json = output_dir / "daily_report_data.json"
    report_md = output_dir / "daily_report_final.md"
    selection_path = output_dir / "daily_report_selection.json"
    html_path = output_dir / "daily_report_card_v3.html"
    png_path = output_dir / "daily_report_card_v3.png"
    manifest_path = output_dir / "daily_report_v3_visual_validation.json"

    report_json.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown = render_report(data)
    report_md.write_text(markdown, encoding="utf-8")
    selection = {"selected_source": "DETERMINISTIC", "fixture_validation": True}
    selection_path.write_text(json.dumps(selection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    model = build_view_model_v3(data, markdown, selection)
    local_assets_uri = ASSET_DIR.as_uri()
    html_path.write_text(render_html_v3(model, asset_base_path=local_assets_uri), encoding="utf-8")
    export_png(html_path, png_path, width=1080, height=1600)
    visual_result = validate_visual_report_v3(html_path, png_path, manifest_path=manifest_path)
    image_check = _local_images_loaded(html_path)
    images_loaded = image_check["loaded"]

    result = {
        "mode": "OFFLINE_FIXTURE_ONLY",
        "fixture": str(fixture_path.resolve()),
        "story_fixture": str(STORY_FIXTURE.resolve()),
        "output_dir": str(output_dir),
        "market_date": data.get("market_date"),
        "report_markdown": str(report_md),
        "poster_html": str(html_path),
        "poster_png": str(png_path),
        "validation_manifest": str(manifest_path),
        "visual_validation": visual_result,
        "local_images_loaded": images_loaded,
        "runtime": _runtime_metadata(image_check["chromium_version"]),
        "telegram_sent": False,
        "network_calls": 0,
    }
    (output_dir / "validation_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if not visual_result.get("valid") or not images_loaded:
        errors = list(visual_result.get("errors", []))
        if not images_loaded:
            errors.append("LOCAL_POSTER_ASSET_NOT_LOADED")
        raise RuntimeError("FIXTURE_VISUAL_VALIDATION_FAILED:" + ",".join(errors))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    args = parser.parse_args()
    # This validation command is always fixture-only; fail closed if rendering
    # or a future code path attempts DNS resolution or an external connection.
    from tests.offline_network_guard import activate_offline_network_guard

    activate_offline_network_guard()
    result = validate_fixture(args.output_dir, args.fixture)
    print(f"OFFLINE_FIXTURE_VALIDATION={result['visual_validation']['valid']}")
    print(f"MARKET_DATE={result['market_date']}")
    print(f"PNG={result['poster_png']}")
    print(f"CHROMIUM_VERSION={result['runtime']['browser']['version']}")
    print(f"OS={result['runtime']['os']['platform']}")
    print("NETWORK_CALLS=0 TELEGRAM_SENT=False")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
