import argparse
import json
import logging
import sys
import types
from pathlib import Path

import run_daily


def _stub_pipeline(monkeypatch, tmp_path, *, delivery_code=0, readiness="READY_WITH_WARNINGS", selected_source="DETERMINISTIC"):
    monkeypatch.setattr(run_daily, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(run_daily, "STATUS_PATH", tmp_path / "daily_run_status.json")
    monkeypatch.setattr(run_daily, "run_preflight", lambda args, logger: None)
    monkeypatch.setattr(run_daily, "collect_market_data", lambda market_date_override=None, refresh_movers=False, refresh_market=False, movers_prefilter_enabled=True: 0)
    monkeypatch.setattr(run_daily, "_market_date", lambda: "2026-09-25")
    monkeypatch.setattr(run_daily, "_market_date_resolution", lambda: {"source": "CORE_ETF_CONSENSUS", "agreement": "4/4"})
    monkeypatch.setattr(run_daily, "_require_file", lambda path, label: path)
    monkeypatch.setattr(run_daily, "run_live_news_stage", lambda *args, **kwargs: tmp_path / "live_news_v3_snapshot.json")
    monkeypatch.setattr(run_daily, "run_market_driver_stage", lambda *args, **kwargs: tmp_path / "market_driver_snapshot.json")
    monkeypatch.setattr(run_daily, "run_catalyst_mapping_stage", lambda logger: tmp_path / "catalysts_snapshot.json")
    monkeypatch.setattr(run_daily, "compose_daily_report", lambda logger=None: tmp_path / "daily_report_data.json")
    monkeypatch.setattr(run_daily, "render_from_file", lambda logger=None: tmp_path / "daily_report.md")
    monkeypatch.setattr(run_daily, "run_narrative", lambda logger=None: tmp_path / "daily_report_final.md")
    monkeypatch.setattr(run_daily, "generate_visual_v3", lambda: None)
    gate = {"report": {"market_date": "2026-09-25", "publication_readiness": {"status": readiness}, "top_stories": [{"title_he": "Story"}]}, "selection": {"selected_source": selected_source, "guardrails_valid": selected_source != "LLM"}, "html": tmp_path / "daily_report_card_v3.html", "png": tmp_path / "daily_report_card_v3.png", "validation": {"valid": True}}
    monkeypatch.setattr(run_daily, "validate_send_gate", lambda: gate)

    def fake_delivery(**kwargs):
        (tmp_path / "daily_report_delivery.json").write_text(json.dumps({"status": "SENT" if delivery_code == 0 else "PARTIAL"}), encoding="utf-8")
        return delivery_code

    monkeypatch.setattr(run_daily, "run_delivery", fake_delivery)


def test_full_dry_run_selects_v3_and_does_not_send(monkeypatch, tmp_path, capsys):
    _stub_pipeline(monkeypatch, tmp_path)
    called = []
    monkeypatch.setattr(run_daily, "run_delivery", lambda **kwargs: called.append(kwargs) or 0)
    assert run_daily.run(["--dry-run"]) == 0
    assert called == []
    assert "DAILY RUN READY — TELEGRAM NOT SENT (DRY RUN)" in capsys.readouterr().out
    status = json.loads((tmp_path / "daily_run_status.json").read_text(encoding="utf-8"))
    assert status["visual_version"] == "V3" and status["telegram_sent"] is False


def test_deterministic_fallback_and_warnings_allow_send(monkeypatch, tmp_path):
    _stub_pipeline(monkeypatch, tmp_path)
    assert run_daily.run([]) == 0
    status = json.loads((tmp_path / "daily_run_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "SUCCESS" and status["telegram_sent"] is True


def test_validation_failure_blocks_delivery(monkeypatch, tmp_path):
    _stub_pipeline(monkeypatch, tmp_path)
    sent = []
    monkeypatch.setattr(run_daily, "validate_send_gate", lambda: (_ for _ in ()).throw(RuntimeError("VISUAL_VALIDATION_FAILED")))
    monkeypatch.setattr(run_daily, "run_delivery", lambda **kwargs: sent.append(True) or 0)
    assert run_daily.run([]) == 2
    assert sent == []
    status = json.loads((tmp_path / "daily_run_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "FAILED" and status["telegram_sent"] is False


def test_telegram_failure_has_exit_code_three(monkeypatch, tmp_path):
    _stub_pipeline(monkeypatch, tmp_path, delivery_code=1)
    assert run_daily.run([]) == 3
    status = json.loads((tmp_path / "daily_run_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "FAILED" and status["failed_stage"] == "TELEGRAM_DELIVERY"


def _install_fake_playwright(monkeypatch, *, launch_error=None):
    class FakeBrowser:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    browser = FakeBrowser()

    def launch(*, headless):
        assert headless is True
        if launch_error:
            raise launch_error
        return browser

    class FakeContext:
        def __enter__(self):
            return types.SimpleNamespace(chromium=types.SimpleNamespace(launch=launch))

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    package = types.ModuleType("playwright")
    package.__path__ = []
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: FakeContext()
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    return browser


def _preflight_args(*, dry_run=False, skip_telegram=False):
    return argparse.Namespace(dry_run=dry_run, skip_telegram=skip_telegram)


def test_preflight_dry_run_skips_telegram_configuration(monkeypatch, tmp_path):
    _install_fake_playwright(monkeypatch)
    monkeypatch.setattr(run_daily, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "test-key")
    monkeypatch.setenv("ENABLE_LLM", "false")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_IDS", raising=False)

    run_daily.run_preflight(_preflight_args(dry_run=True), logging.getLogger("test-preflight"))


def test_preflight_skip_telegram_flag_skips_telegram_configuration(monkeypatch, tmp_path):
    _install_fake_playwright(monkeypatch)
    monkeypatch.setattr(run_daily, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "test-key")
    monkeypatch.setenv("ENABLE_LLM", "false")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_IDS", raising=False)

    run_daily.run_preflight(_preflight_args(skip_telegram=True), logging.getLogger("test-preflight"))


def test_preflight_requires_telegram_configuration_for_production(monkeypatch, tmp_path):
    _install_fake_playwright(monkeypatch)
    monkeypatch.setattr(run_daily, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "test-key")
    monkeypatch.setenv("ENABLE_LLM", "false")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_IDS", raising=False)

    try:
        run_daily.run_preflight(_preflight_args(), logging.getLogger("test-preflight"))
    except run_daily.PreflightFailure as exc:
        assert str(exc) == "TELEGRAM_CONFIG_MISSING"
    else:
        raise AssertionError("expected Telegram preflight failure")


def test_preflight_requires_market_api_key_before_collection(monkeypatch, tmp_path):
    _install_fake_playwright(monkeypatch)
    monkeypatch.setattr(run_daily, "OUTPUT_DIR", tmp_path)
    monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
    monkeypatch.setenv("ENABLE_LLM", "false")

    try:
        run_daily.run_preflight(_preflight_args(dry_run=True), logging.getLogger("test-preflight"))
    except run_daily.PreflightFailure as exc:
        assert str(exc) == "TWELVE_DATA_API_KEY_MISSING"
    else:
        raise AssertionError("expected market API preflight failure")


def test_preflight_requires_openai_key_when_llm_enabled(monkeypatch, tmp_path):
    _install_fake_playwright(monkeypatch)
    monkeypatch.setattr(run_daily, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "test-key")
    monkeypatch.setenv("ENABLE_LLM", "true")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    try:
        run_daily.run_preflight(_preflight_args(dry_run=True), logging.getLogger("test-preflight"))
    except run_daily.PreflightFailure as exc:
        assert str(exc) == "OPENAI_API_KEY_MISSING"
    else:
        raise AssertionError("expected LLM preflight failure")


def test_preflight_maps_browser_launch_failure(monkeypatch, tmp_path):
    _install_fake_playwright(monkeypatch, launch_error=RuntimeError("browser unavailable"))
    monkeypatch.setattr(run_daily, "OUTPUT_DIR", tmp_path)

    try:
        run_daily.run_preflight(_preflight_args(dry_run=True), logging.getLogger("test-preflight"))
    except run_daily.PreflightFailure as exc:
        assert str(exc) == "PLAYWRIGHT_CHROMIUM_UNAVAILABLE"
    else:
        raise AssertionError("expected browser preflight failure")


def test_run_stops_at_preflight_before_market_collection(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(run_daily, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(run_daily, "STATUS_PATH", tmp_path / "daily_run_status.json")
    collected = []
    monkeypatch.setattr(run_daily, "run_preflight", lambda args, logger: (_ for _ in ()).throw(run_daily.PreflightFailure("PLAYWRIGHT_CHROMIUM_UNAVAILABLE")))
    monkeypatch.setattr(run_daily, "collect_market_data", lambda **kwargs: collected.append(True) or 0)

    assert run_daily.run(["--dry-run"]) == 1
    assert collected == []
    assert "PREFLIGHT FAILED — PLAYWRIGHT_CHROMIUM_UNAVAILABLE" in capsys.readouterr().out
    status = json.loads((tmp_path / "daily_run_status.json").read_text(encoding="utf-8"))
    assert status["failed_stage"] == "PREFLIGHT"
