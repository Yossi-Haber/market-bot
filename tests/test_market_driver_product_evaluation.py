import json
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path

import evaluate_market_drivers_product as product
from drivers.model import DriverStatus
from news.schema import NewsCandidate


def _driver(symbol, move):
    return {"driver_id": f"STOCK_{symbol}", "driver_type": "STOCK_CATALYST", "scope": "STOCK", "direction": "down", "strength": abs(move), "related_symbols": [symbol], "movement_evidence": [{"value": move}], "verification_status": "PLAUSIBLE_NOT_VERIFIED", "candidate_stories": []}


def _day():
    ba = {"title": "FAA delays Boeing 737 Max 10 certification over software glitch", "published_at": "2026-09-28T12:00:00+00:00", "affected_symbols": ["BA"], "event_type": "REGULATORY", "event_actions": ["SOFTWARE_GLITCH", "CERTIFICATION_DELAY"], "target_role": "PRIMARY_SUBJECT", "subject_score": 80, "source_name": "Reuters"}
    dash = {"title": "A Look at DoorDash: GF Value after the decline", "published_at": "2026-09-28T12:00:00+00:00", "affected_symbols": ["DASH"], "event_type": "OTHER", "event_actions": [], "target_role": "PRIMARY_SUBJECT", "subject_score": 80, "source_name": "GuruFocus"}
    return {"market_date": "2026-09-28", "news_context": {"candidate_stories": [ba, dash], "verified_stories": [], "verification": {"records": [], "verified_stories": []}}, "stock_catalysts": [_driver("BA", -6.9), _driver("DASH", -7.74)], "metrics": {"fmp_requests": 0}}


def test_ba_and_dash_causal_regression():
    day = _day()
    candidates = product._candidate_records(day)
    records = product._stock_records(day, candidates)
    by_symbol = {item["symbol"]: item for item in records}
    assert by_symbol["BA"]["final_driver_status"] == "PLAUSIBLE_NOT_VERIFIED"
    assert by_symbol["BA"]["causal_gate"] == "PASS"
    assert by_symbol["DASH"]["final_driver_status"] == "NO_CATALYST_FOUND"
    assert by_symbol["DASH"]["causal_reason"] == "COMMENTARY_ONLY"
    assert by_symbol["DASH"]["selected_event"] is None
    assert by_symbol["DASH"]["best_rejected_candidate"]


def test_review_queue_contains_large_moves():
    day = _day()
    candidates = product._candidate_records(day)
    records = product._stock_records(day, candidates)
    assert {item["symbol"] for item in records if abs(item["move_pct"]) >= 4} == {"BA", "DASH"}


def test_manual_label_metrics_are_optional_and_calculated(tmp_path):
    records = [{"date": "2026-09-28", "symbol": "BA", "final_driver_status": "PLAUSIBLE_NOT_VERIFIED"}]
    labels = [{"date": "2026-09-28", "symbol": "BA", "label": "PLAUSIBLE_CAUSAL"}]
    metrics = product._label_metrics(records, labels)
    assert metrics["labeled_records"] == 1
    assert metrics["precision_on_labeled"] == 1
    assert product._label_metrics(records, [])['labeled_records'] == 0


def test_output_writer_creates_json_markdown_and_csv(tmp_path):
    result = {"metrics": {"days_evaluated": 0}, "days": [], "review_queue": [], "warnings": []}
    product._write_outputs(result, tmp_path)
    assert (tmp_path / "evaluation" / "market_driver_multiday.json").exists()
    assert (tmp_path / "evaluation" / "market_driver_multiday.md").exists()
    assert (tmp_path / "evaluation" / "market_driver_review_queue.csv").exists()
    json.loads((tmp_path / "evaluation" / "market_driver_multiday.json").read_text(encoding="utf-8"))


def test_output_writer_serializes_nested_news_candidate_and_primitives(tmp_path):
    candidate = NewsCandidate(
        title="FAA delays Boeing certification",
        summary="Software issue.",
        published_at="2026-09-28T12:00:00+00:00",
        source_name="Reuters",
        source_domain="reuters.com",
        source_tier=3,
        source_url="https://example.test/ba",
        category="REGULATORY",
        affected_symbols=["BA"],
        raw_metadata={"observed_at": datetime(2026, 9, 28, 12, tzinfo=timezone.utc)},
    )
    result = {
        "metrics": {"days_evaluated": 1, "confirmed_drivers": 0},
        "days": [{"market_date": "2026-09-28", "status": "READY", "stock_records": [], "nested": {"candidate": candidate}}],
        "review_queue": [{"date": date(2026, 9, 28), "symbol": "BA", "move_pct": 5, "final_driver_status": "PLAUSIBLE_NOT_VERIFIED", "selected_event": candidate, "causal_gate": "PASS", "verification_status": "NOT_CONFIRMED", "causal_reason": "EVENT_SIGNAL_PRESENT"}],
        "warnings": [],
        "enum": DriverStatus.PLAUSIBLE_NOT_VERIFIED,
        "path": Path("cache/finnhub/BA.json"),
    }
    product._write_outputs(result, tmp_path)
    loaded = json.loads((tmp_path / "evaluation" / "market_driver_multiday.json").read_text(encoding="utf-8"))
    assert loaded["days"][0]["nested"]["candidate"]["title"] == "FAA delays Boeing certification"
    assert loaded["days"][0]["nested"]["candidate"]["raw_metadata"]["observed_at"] == "2026-09-28T12:00:00+00:00"
    assert loaded["enum"] == "PLAUSIBLE_NOT_VERIFIED"
    assert loaded["path"].endswith("cache\\finnhub\\BA.json")
    assert "FAA delays Boeing certification" in (tmp_path / "evaluation" / "market_driver_multiday.md").read_text(encoding="utf-8")


def test_serialization_does_not_change_metrics():
    result = {"metrics": {"confirmed_drivers": 2, "plausible_drivers": 3}, "days": [], "review_queue": [], "warnings": []}
    safe = product.replay.to_json_safe(result)
    assert safe["metrics"] == result["metrics"]


def test_state_invariant_warning_is_only_for_real_breach():
    day = _day()
    bad = [{"symbol": "META", "final_driver_status": "PLAUSIBLE_NOT_VERIFIED", "causal_gate": "FAIL", "causal_reason": "NO_CONCRETE_EVENT", "event_time_relation": "FUTURE_EVENT", "candidate_count": 1, "selected_event": None}]
    assert "EVALUATOR_STATE_INVARIANT_BREACH" in product._quality_flags(day, [], bad)
    good = [{"symbol": "BA", "final_driver_status": "PLAUSIBLE_NOT_VERIFIED", "causal_gate": "PASS", "causal_reason": "EVENT_SIGNAL_PRESENT", "event_time_relation": "BEFORE_OR_DURING_SESSION", "candidate_count": 1, "selected_event": "FAA"}]
    assert "EVALUATOR_STATE_INVARIANT_BREACH" not in product._quality_flags(day, [], good)


def test_ready_and_partial_metrics_are_separate(monkeypatch, tmp_path):
    day = _day()
    day.update({
        "cache_coverage": {"market_data": "AVAILABLE", "movers": "AVAILABLE", "finnhub": "AVAILABLE", "verification": "AVAILABLE", "official": "MISSING"},
        "metrics": {"runtime_seconds": 1.0, "fmp_requests": 0},
        "market_values": {},
    })
    monkeypatch.setattr(product.replay, "evaluate_market_drivers", lambda *args, **kwargs: {"days": [day], "skipped_dates": []})
    result = product.evaluate_product("2026-09-28", "2026-09-28", output_dir=tmp_path)
    assert result["metrics"]["ready_days_evaluated"] == 1
    assert result["metrics"]["partial_days_evaluated"] == 0
    assert result["metrics"]["ready_only_metrics"]["total_meaningful_moves"] == result["metrics"]["all_available_metrics"]["total_meaningful_moves"]
