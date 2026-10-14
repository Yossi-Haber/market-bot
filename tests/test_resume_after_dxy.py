import json

from evaluation import resume_extended_dataset_after_dxy as resume


def test_missing_market_defers_dependent_components(monkeypatch, tmp_path):
    monkeypatch.setattr(resume, "MILESTONE", tmp_path)
    monkeypatch.setattr(resume.historical, "_validate_historical_market", lambda *args, **kwargs: False)
    monkeypatch.setattr(resume.movers, "load_valid_movers_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(resume.historical, "_market_news_state", lambda _: "MISS")
    monkeypatch.setattr(resume.historical, "_mover_symbols", lambda _: [])
    monkeypatch.setattr(resume.historical, "_finnhub_cache_status", lambda *args: (0, 0))
    result = resume._artifact_states("2026-09-25")
    assert result["market_data_status"] == "MISSING"
    assert result["movers_status"] == "MISSING"
    assert result["verification_status"] == "DEFERRED"
    assert result["remaining_actions"] == "market"
    assert result["deferred"] == "movers,market_news,stock_news,verification,market_driver_snapshot"


def test_sep22_driver_is_stale_when_mover_cache_is_invalid(monkeypatch, tmp_path):
    monkeypatch.setattr(resume, "MILESTONE", tmp_path)
    day_dir = tmp_path / "history" / "2026-09-22"
    day_dir.mkdir(parents=True)
    (day_dir / "market_snapshot.json").write_text(json.dumps({"market_date": "2026-09-22"}), encoding="utf-8")
    (day_dir / "movers_snapshot.json").write_text(json.dumps({"market_date": "2026-09-22", "collection_status": "FAILED"}), encoding="utf-8")
    (day_dir / "market_driver_snapshot.json").write_text(json.dumps({"market_date": "2026-09-22", "driver_analysis": {}, "metrics": {}}), encoding="utf-8")
    monkeypatch.setattr(resume.historical, "_validate_historical_market", lambda *args, **kwargs: True)
    monkeypatch.setattr(resume.movers, "load_valid_movers_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(resume.historical, "_market_news_state", lambda _: "HIT")
    monkeypatch.setattr(resume.historical, "_mover_symbols", lambda _: [])
    monkeypatch.setattr(resume.historical, "_finnhub_cache_status", lambda *args: (0, 0))
    result = resume._artifact_states("2026-09-22")
    assert result["market_data_status"] == "HIT"
    assert result["movers_status"] == "INVALID"
    assert result["verification_status"] == "DEFERRED"
    assert result["snapshot_status"] == "STALE_MOVER_DEPENDENCY"
    assert result["remaining_actions"] == "movers"


def test_all_valid_components_require_no_resume_actions(monkeypatch, tmp_path):
    monkeypatch.setattr(resume, "MILESTONE", tmp_path)
    day_dir = tmp_path / "history" / "2026-09-25"
    day_dir.mkdir(parents=True)
    (day_dir / "market_snapshot.json").write_text(json.dumps({"market_date": "2026-09-25"}), encoding="utf-8")
    (day_dir / "movers_snapshot.json").write_text(json.dumps({"market_date": "2026-09-25"}), encoding="utf-8")
    (day_dir / "market_driver_snapshot.json").write_text(json.dumps({"market_date": "2026-09-25", "driver_analysis": {}, "metrics": {}}), encoding="utf-8")
    monkeypatch.setattr(resume.historical, "_validate_historical_market", lambda *args, **kwargs: True)
    monkeypatch.setattr(resume.movers, "load_valid_movers_cache", lambda *args, **kwargs: {"top_gainers": []})
    monkeypatch.setattr(resume.historical, "_market_news_state", lambda _: "HIT")
    monkeypatch.setattr(resume.historical, "_mover_symbols", lambda _: [])
    monkeypatch.setattr(resume.historical, "_finnhub_cache_status", lambda *args: (0, 0))
    result = resume._artifact_states("2026-09-25")
    assert result["remaining_actions"] == "none"
    assert result["verification_status"] == "READY"
    assert result["snapshot_status"] == "HIT"
