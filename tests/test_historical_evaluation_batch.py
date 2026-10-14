import csv
from pathlib import Path

import build_historical_evaluation_batch as batch


def test_select_dates_skips_weekends_and_deduplicates():
    assert batch.select_dates(from_date="2026-09-17", to_date="2026-09-21") == [
        "2026-09-17", "2026-09-18", "2026-09-21"
    ]
    assert batch.select_dates(market_dates="2026-09-21,2026-09-19,2026-09-21") == ["2026-09-21"]


def test_plan_mode_does_not_build_or_evaluate(monkeypatch):
    monkeypatch.setattr(batch, "inspect_components", lambda value: {
        "date": value, "market_data": "HIT", "movers": "HIT", "market_news": "HIT",
        "stock_news": "HIT", "verification": "READY", "coverage": {},
        "actions_needed": "none", "deferred": "none", "plan_ready": True,
        "expected_network_work": False,
    })
    monkeypatch.setattr(batch.historical, "build", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("build called")))
    monkeypatch.setattr(batch, "evaluate_product", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("evaluate called")))
    result = batch.run_batch(["2026-09-28"], plan=True)
    assert result["components"][0]["status"] == "PLAN"
    assert result["offline_network_calls"] == 0


def test_resume_skips_plan_ready_date(monkeypatch, tmp_path):
    monkeypatch.setattr(batch, "BATCH_DIR", tmp_path / "evaluation_batch")
    monkeypatch.setattr(batch, "inspect_components", lambda value: {
        "date": value, "actions_needed": "none", "deferred": "none",
        "plan_ready": True, "expected_network_work": False,
    })
    monkeypatch.setattr(batch, "_integrity_ok", lambda value: False)
    monkeypatch.setattr(batch.historical, "build", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("build called")))
    result = batch.run_batch(["2026-09-28"], resume=True)
    assert result["components"][0]["status"] == "SKIPPED"


def test_csv_manual_annotations_are_preserved(tmp_path):
    path = tmp_path / "market_day_review_queue.csv"
    path.write_text("date,manual_label,review_notes\n2026-09-28,KEEP,checked\n", encoding="utf-8")
    batch._write_csv(path, [{"date": "2026-09-28", "manual_label": "", "review_notes": "", "priority": "HIGH"}], ["date", "manual_label", "review_notes", "priority"], ("date",))
    with path.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["manual_label"] == "KEEP"
    assert row["review_notes"] == "checked"


def test_failed_date_is_retained_without_evaluating_it(monkeypatch, tmp_path):
    monkeypatch.setattr(batch, "BATCH_DIR", tmp_path / "evaluation_batch")
    monkeypatch.setattr(batch, "inspect_components", lambda value: {"date": value, "actions_needed": "market", "plan_ready": False, "expected_network_work": True})
    monkeypatch.setattr(batch.historical, "build", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline fixture failure")))
    monkeypatch.setattr(batch, "evaluate_product", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("evaluate called")))
    result = batch.run_batch(["2026-09-28"])
    assert result["components"][0]["status"] == "FAILED"
    assert result["integrity_failures"] == ["2026-09-28"]


def test_single_execution_uses_range_specific_package_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(batch, "BATCH_DIR", tmp_path / "evaluation_batch")
    monkeypatch.setattr(batch, "inspect_components", lambda value: {
        "date": value, "trading_day_status": "TRADING_DAY", "actions_needed": "none",
        "plan_ready": True, "expected_network_work": False, "market_news": "HIT",
    })
    monkeypatch.setattr(batch, "_offline_evaluate_dates", lambda dates, logger: ([], {}))
    result = batch.run_batch(["2026-09-24"], offline_only=True)
    assert "runs" in result["package_dir"]
    assert not (batch.BATCH_DIR / "historical_batch_summary.json").exists()


def test_offline_only_never_calls_historical_builder(monkeypatch, tmp_path):
    monkeypatch.setattr(batch, "BATCH_DIR", tmp_path / "evaluation_batch")
    monkeypatch.setattr(batch, "inspect_components", lambda value: {
        "date": value, "trading_day_status": "TRADING_DAY", "actions_needed": "market",
        "plan_ready": False, "expected_network_work": True, "market_news": "MISS",
    })
    monkeypatch.setattr(batch.historical, "build", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("builder called")))
    monkeypatch.setattr(batch, "_offline_evaluate_dates", lambda dates, logger: ([], {dates[0]: "MISSING_MARKET_OR_MOVERS_SNAPSHOT"}))
    result = batch.run_batch(["2026-09-24"], offline_only=True)
    assert result["offline_network_calls"] == 0


def test_build_missing_components_skips_existing_caches(monkeypatch):
    monkeypatch.setattr(batch, "inspect_components", lambda value: {
        "market_news": "HIT", "stock_news": "HIT",
    })
    monkeypatch.setattr(batch.historical, "_build_market_news", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("market cache rebuilt")))
    monkeypatch.setattr(batch.historical, "run_market_driver_pipeline", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("stock cache rebuilt")))
    result = batch.build_missing_components(["2026-09-24"])
    assert result["dates"] == [{"market_date": "2026-09-24", "market_news": "HIT", "stock_news": "HIT"}]
