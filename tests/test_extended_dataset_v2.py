"""Offline safeguards for the staged Extended Dataset V2 runner."""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from collectors.rate_limit import QuotaPauseRequired
from evaluation import run_extended_dataset_v2 as runner


def test_staged_resume_reuses_only_valid_point_in_time_market_items() -> None:
    valid = {"verified": True, "validation_passed": True, "selected_source_date": "2026-08-17"}
    assert runner.reusable_market_item(valid, "2026-08-17")
    assert not runner.reusable_market_item(valid | {"selected_source_date": "2026-08-18"}, "2026-08-17")
    assert not runner.reusable_market_item(valid | {"validation_passed": False}, "2026-08-17")
    assert not runner.reusable_market_item({"verified": True}, "2026-08-17")


def test_established_sep22_failed_movers_fixture_is_preserved() -> None:
    state = {"movers_snapshot": "SEEDED"}
    diagnostic = {"market_date": "2026-09-22", "collection_status": "FAILED"}
    assert runner.preserve_known_mover_regression("2026-09-22", state, diagnostic)
    assert not runner.preserve_known_mover_regression("2026-09-23", state, diagnostic)
    assert not runner.preserve_known_mover_regression("2026-09-22", {"movers_snapshot": "MISSING"}, diagnostic)


def test_batch_rate_limiter_instance_is_shared_and_restored() -> None:
    created = []

    class FakeLimiter:
        pass

    def factory():
        limiter = FakeLimiter()
        created.append(limiter)
        return limiter

    app_module = SimpleNamespace(CreditRateLimitController=factory)
    with runner.shared_batch_rate_limiter(app_module) as shared:
        assert app_module.CreditRateLimitController() is shared
        assert app_module.CreditRateLimitController() is shared
    assert len(created) == 1
    assert app_module.CreditRateLimitController is factory


def test_interrupted_date_writes_only_to_staging_and_defers_movers_on_invalid_market(tmp_path: Path, monkeypatch) -> None:
    milestone = tmp_path / "output" / "evaluation_extended_v2" / "30_day_milestone"
    staged_date = milestone / "history" / "2026-08-17"
    original_output = tmp_path / "output"
    original_output.mkdir(parents=True)
    (original_output / "market_snapshot.json").write_text('{"market_date":"2026-09-28"}', encoding="utf-8")
    monkeypatch.setattr(runner, "MILESTONE", milestone)
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(
        runner,
        "seed_date",
        lambda *args, **kwargs: {"market_valid": False, "movers_valid": False},
    )

    live_movers_calls: list[bool] = []
    app_module = SimpleNamespace(OUTPUT_DIR=original_output)
    movers_module = SimpleNamespace(OUTPUT_DIR=original_output)

    def write_snapshot(snapshot, generated_at):  # pragma: no cover - should only target staged output
        path = app_module.OUTPUT_DIR / "market_snapshot.json"
        runner.atomic_json(path, snapshot)
        return path, path

    def live_movers(*args, **kwargs):
        live_movers_calls.append(True)
        path = movers_module.OUTPUT_DIR / "movers_snapshot.json"
        runner.atomic_json(path, {"market_date": "2026-08-17"})
        return path

    def main(**kwargs):
        write_snapshot(
            {"market_date": "2026-08-17", "market": {}, "summary": {"failed": 1}},
            None,
        )
        return app_module.run_movers_snapshot(None, None, "2026-08-17")

    app_module.write_snapshot = write_snapshot
    app_module.run_movers_snapshot = live_movers
    app_module.collect_instrument = lambda *args, **kwargs: {}
    app_module.main = main

    @contextlib.contextmanager
    def stage_output(directory: Path):
        prior_app, prior_movers = app_module.OUTPUT_DIR, movers_module.OUTPUT_DIR
        app_module.OUTPUT_DIR = directory
        movers_module.OUTPUT_DIR = directory
        try:
            yield
        finally:
            app_module.OUTPUT_DIR, movers_module.OUTPUT_DIR = prior_app, prior_movers

    historical = SimpleNamespace(
        _stage_output=stage_output,
        _validate_historical_market=lambda snapshot, date_value: False,
        _market_news_state=lambda date_value: "MISS",
    )
    movers_module.load_valid_movers_cache = lambda *args, **kwargs: None
    progress, _ = runner.process_date(
        "2026-08-17",
        logger=logging.getLogger("test_extended_dataset_v2"),
        historical=historical,
        app_module=app_module,
        movers_module=movers_module,
        driver_runtime=SimpleNamespace(),
        prevent_sleep=lambda: None,
    )

    staged_snapshot = staged_date / "market_snapshot.json"
    assert staged_snapshot.is_file()
    assert json.loads(staged_snapshot.read_text(encoding="utf-8"))["market_date"] == "2026-08-17"
    assert json.loads((original_output / "market_snapshot.json").read_text(encoding="utf-8"))["market_date"] == "2026-09-28"
    assert not live_movers_calls
    assert progress["status"] == "PARTIAL"
    assert app_module.OUTPUT_DIR == original_output
    assert movers_module.OUTPUT_DIR == original_output


def test_process_date_handles_quota_pause_and_preserves_wait_checkpoint(tmp_path: Path, monkeypatch) -> None:
    milestone = tmp_path / "milestone"
    stage = milestone / "history" / "2026-08-17"
    monkeypatch.setattr(runner, "MILESTONE", milestone)
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "seed_date", lambda *args, **kwargs: {"market_valid": False, "movers_valid": False})
    pause = QuotaPauseRequired(
        expected_wait=42.5, next_eligible_at="2026-08-17T12:01:00Z",
        context={"purpose": "MOVER_FINAL", "symbol": "AAPL"},
    )
    app_module = SimpleNamespace(
        write_snapshot=lambda *args, **kwargs: None,
        run_movers_snapshot=lambda *args, **kwargs: None,
        collect_instrument=lambda *args, **kwargs: None,
        main=lambda **kwargs: (_ for _ in ()).throw(pause),
    )
    movers_module = SimpleNamespace(
        OUTPUT_DIR=tmp_path / "output",
        load_valid_movers_cache=lambda *args, **kwargs: None,
    )
    historical = SimpleNamespace(
        _stage_output=lambda directory: __import__("contextlib").nullcontext(),
        _validate_historical_market=lambda *args, **kwargs: False,
        _market_news_state=lambda *args, **kwargs: "MISS",
    )

    progress, _ = runner.process_date(
        "2026-08-17", logger=logging.getLogger("quota-pause-test"),
        historical=historical, app_module=app_module, movers_module=movers_module,
        driver_runtime=SimpleNamespace(), prevent_sleep=lambda: None,
    )

    assert progress["status"] == "QUOTA_PAUSED"
    assert progress["quota_pause"]["next_eligible_at"] == pause.next_eligible_at
    assert progress["quota_pause"]["required_wait_seconds"] == 42.5
    assert progress["quota_pause"]["current_component"] == "MOVER_FINAL"
    assert stage.is_dir()
