import csv
import json
from pathlib import Path

import build_historical_evaluation_batch as batch
import evaluate_market_drivers as replay
import evaluate_market_drivers_product as product
from drivers.causal import evaluate_causal_eligibility, publication_time_relation
from news.schema import NewsCandidate


def _candidate(title, published):
    return NewsCandidate(
        title=title, summary=None, published_at=published, source_name="Finnhub",
        source_domain="finnhub.io", source_tier=3, source_url="https://finnhub.io/news",
        category="EARNINGS", affected_symbols=["META"], discovery_source="finnhub",
        article_type="NEWS_EVENT",
    )


def test_finnhub_as_of_filter_excludes_future_before_clustering(monkeypatch, tmp_path):
    cache = tmp_path / "finnhub" / "META" / "2026-09-24_2026-09-25.json"
    cache.parent.mkdir(parents=True)
    articles = [
        _candidate("Meta reports product update", "2026-09-24T20:00:00+00:00").__dict__,
        _candidate("Why Meta Stock Could Spike at This Price Target", "2026-09-25T17:13:00+00:00").__dict__,
    ]
    cache.write_text(json.dumps({"schema_version": "finnhub-discovery-v1", "provider": "FINNHUB", "symbol": "META", "requested_from_date": "2026-09-24", "requested_to_date": "2026-09-25", "normalized_candidates": articles}), encoding="utf-8")
    monkeypatch.setattr(replay, "CACHE_DIR", tmp_path)
    seen = []

    def cluster(items, symbol, company_name):
        seen.extend(items)
        return [{"canonical_event_title": items[0].title, "best_candidate": items[0], "articles": items}] if items else []

    monkeypatch.setattr(replay, "cluster_finnhub_candidates", cluster)
    result = replay._load_finnhub_cache("META", "2026-09-24")
    assert [item.title for item in seen] == ["Meta reports product update"]
    assert result[0]["canonical_event_title"] == "Meta reports product update"


def test_meta_sep21_same_session_articles_survive_as_of_filter(monkeypatch, tmp_path):
    cache = tmp_path / "finnhub" / "META" / "2026-09-21_2026-09-22.json"
    cache.parent.mkdir(parents=True)
    articles = [
        _candidate("Meta stock rockets 10% on target increase", "2026-09-21T18:04:23+00:00").__dict__,
        _candidate("Meta stock jumps as Wells Fargo raises target", "2026-09-21T19:52:00+00:00").__dict__,
        _candidate("Meta next-session follow-up", "2026-09-22T14:00:00+00:00").__dict__,
    ]
    cache.write_text(json.dumps({"schema_version": "finnhub-discovery-v1", "provider": "FINNHUB", "symbol": "META", "requested_from_date": "2026-09-21", "requested_to_date": "2026-09-22", "normalized_candidates": articles}), encoding="utf-8")
    monkeypatch.setattr(replay, "CACHE_DIR", tmp_path)
    seen = []
    monkeypatch.setattr(replay, "cluster_finnhub_candidates", lambda items, *args: seen.extend(items) or [])
    replay._load_finnhub_cache("META", "2026-09-21")
    assert [item.title for item in seen] == [articles[0]["title"], articles[1]["title"]]


def test_selected_future_article_cannot_inherit_prior_cluster_timing():
    result = evaluate_causal_eligibility({
        "title": "Why Meta Stock Could Spike if It Crosses This Price Target",
        "event_type": "EARNINGS", "published_at": "2026-09-25T17:13:00+00:00",
        "cluster_article_dates": ["2026-09-24T20:00:00+00:00"], "subject_score": 80,
    }, "2026-09-24")
    assert result["event_time_relation"] == "FUTURE_EVENT"
    assert result["causal_reason"] == "TIMING_INELIGIBLE"
    assert result["causal_eligible"] is False


def test_session_cutoff_uses_new_york_not_utc_or_timestamp_calendar_date():
    assert publication_time_relation("2026-09-21T18:04:00+00:00", "2026-09-21") == "BEFORE_OR_DURING_SESSION"
    assert publication_time_relation("2026-09-21T19:52:00Z", "2026-09-21") == "BEFORE_OR_DURING_SESSION"
    # An extreme but valid offset can put the encoded date on Sep 22 while
    # the publication was still on Sep 21 in New York.
    assert publication_time_relation("2026-09-22T03:00:00+14:00", "2026-09-21") == "BEFORE_OR_DURING_SESSION"
    assert publication_time_relation("2026-09-21T20:30:00Z", "2026-09-21") == "FUTURE_EVENT"
    assert publication_time_relation("2026-09-25T12:00:00Z", "2026-09-24") == "FUTURE_EVENT"


def test_only_future_cluster_is_timing_ineligible():
    result = evaluate_causal_eligibility({
        "title": "Company reports earnings results", "event_type": "EARNINGS",
        "published_at": "2026-09-25T09:00:00Z", "subject_score": 90,
    }, "2026-09-24")
    assert result["event_time_relation"] == "FUTURE_EVENT"
    assert result["causal_reason"] == "TIMING_INELIGIBLE"


def test_future_only_stock_evidence_cannot_retain_plausible_status():
    day = {
        "market_date": "2026-09-24",
        "news_context": {"candidate_stories": [], "timing_diagnostics": {"META": {"future_article_count": 4}}, "verification": {"records": [], "verified_stories": []}},
        "stock_catalysts": [{"driver_id": "STOCK_META", "related_symbols": ["META"], "movement_evidence": [{"value": 4.5}]}],
    }
    analysis = {"stock_catalysts": [{"driver_id": "STOCK_META", "verification_status": "PLAUSIBLE_NOT_VERIFIED"}]}
    row = product._stock_records(day, [], analysis)[0]
    assert row["selected_event"] is None
    assert row["causal_reason"] == "TIMING_INELIGIBLE"
    assert row["event_time_relation"] == "FUTURE_EVENT"
    assert row["final_driver_status"] == "NO_CATALYST_FOUND"


def test_stock_candidate_scope_blocks_oxy_only_items_and_shared_list_contamination():
    import evaluate_market_drivers_product as product

    shared_candidates = [
        {"title": "OXY-only production update", "affected_symbols": ["OXY"], "target_symbol": "OXY", "causal_eligible": True, "event_type": "OPERATIONAL_UPDATE"},
        {"title": "ORCL-only product event", "affected_symbols": ["ORCL"], "target_symbol": "ORCL", "causal_eligible": True, "event_type": "PRODUCT"},
        {"title": "SNPS-only contract", "affected_symbols": ["SNPS"], "target_symbol": "SNPS", "causal_eligible": True, "event_type": "CONTRACT"},
    ]
    analysis = {"stock_catalysts": [
        {"driver_id": f"STOCK_{symbol}", "driver_type": "STOCK_CATALYST", "scope": "STOCK", "direction": "up", "strength": 4.0, "related_symbols": [symbol], "movement_evidence": [{"value": 4.0}]}
        for symbol in ("ORCL", "SNPS")
    ], "sector_driver_analysis": [], "market_driver_analysis": {}}
    result = product._scope_stock_analysis(analysis, shared_candidates, [], {})
    by_symbol = {item["related_symbols"][0]: item["candidate_stories"] for item in result["stock_catalysts"]}
    assert [item["target_symbol"] for item in by_symbol["ORCL"]] == ["ORCL"]
    assert [item["target_symbol"] for item in by_symbol["SNPS"]] == ["SNPS"]
    assert all(item["target_symbol"] != "OXY" for items in by_symbol.values() for item in items)
    assert len(shared_candidates) == 3


def test_explicit_multi_symbol_candidate_needs_direct_subject_mapping():
    import evaluate_market_drivers_product as product

    ambiguous = {"affected_symbols": ["OXY", "ORCL"]}
    assert not product._matches_symbol(ambiguous, "ORCL")
    explicit = {**ambiguous, "direct_subject_symbols": ["ORCL"]}
    assert product._matches_symbol(explicit, "ORCL")


def test_canonical_stock_result_and_review_row_have_exact_parity():
    candidate = {"title": "Meta launches an AI product", "canonical_event_title": "Meta launches an AI product", "target_symbol": "META", "affected_symbols": ["META"], "published_at": "2026-09-21T19:52:00Z", "causal_eligible": True, "causal_gate": "PASS", "causal_reason": "EVENT_SIGNAL_PRESENT", "event_time_relation": "BEFORE_OR_DURING_SESSION", "event_type": "PRODUCT", "subject_gate": "PASS"}
    driver = {"driver_id": "STOCK_META", "related_symbols": ["META"], "movement_evidence": [{"value": 11.3}]}
    analysis = {"stock_catalysts": [{**driver, "verification_status": "PLAUSIBLE_NOT_VERIFIED"}], "stock_explanations": [{"symbol": "META", "stock_specific_catalyst_status": "FOUND", "overall_explanation_status": "STOCK_SPECIFIC"}]}
    day = {"market_date": "2026-09-21", "news_context": {"candidate_stories": [candidate], "timing_diagnostics": {"META": {"future_article_count": 170}}, "verification": {"records": [], "verified_stories": []}}, "stock_catalysts": [driver]}
    records = product._stock_records(day, [candidate], analysis)
    analysis = product._synchronize_canonical_stock_results(analysis, records)
    day["stock_records"] = records
    day["stock_catalysts"] = analysis["stock_catalysts"]
    day["stock_explanations"] = analysis["stock_explanations"]
    queue_row = {key: records[0].get(key) for key in ("symbol", "selected_event", "event_time_relation", "causal_gate", "causal_reason", "verification_status", "candidate_count", "future_article_count", "stock_specific_catalyst_status", "overall_explanation_status")}
    audit = product.audit_stock_consistency(day, [queue_row])
    assert audit["mismatch_count"] == 0
    assert audit["leak_count"] == 0
    assert records[0]["candidate_count"] == len(analysis["stock_catalysts"][0]["candidate_stories"]) == 1
    bad_row = {**queue_row, "overall_explanation_status": "UNEXPLAINED"}
    assert product.audit_stock_consistency(day, [bad_row])["mismatch_count"] == 1


def test_market_candidate_metrics_use_canonical_known_values_only():
    days = [
        {"market_candidate_metrics": {"raw_candidate_count": 24, "filtered_candidate_count": 6, "eligible_candidate_count": 4, "selected_candidate_count": 1}, "integrity": {"integrity_status": "PASS"}},
        {"market_candidate_metrics": {"raw_candidate_count": None, "filtered_candidate_count": None, "eligible_candidate_count": None, "selected_candidate_count": 0}, "integrity": {"integrity_status": "WARN"}},
    ]
    metrics = batch._aggregate(days, [{"status": "READY"}, {"status": "PARTIAL"}])
    assert metrics["avg_market_candidates_raw"] == 24
    assert metrics["market_candidates_raw_known_days"] == 1
    assert metrics["avg_market_candidates_filtered"] == 6
    assert metrics["avg_market_candidates_eligible"] == 4
    assert metrics["avg_market_candidates_selected"] == 0.5


def test_integrity_failure_downgrades_readiness_and_matches_component(monkeypatch):
    monkeypatch.setattr(batch, "evaluate_integrity", lambda day, output_dir: {"integrity_status": "FAIL", "integrity_reasons": ["FUTURE_EVENT_SELECTED"]})
    days = [{"market_date": "2026-09-24", "status": "READY"}]
    components = [{"date": "2026-09-24", "status": "READY"}]
    batch.apply_integrity_results(days, components)
    assert days[0]["integrity"] == components[0]["integrity"]
    assert days[0]["status"] == components[0]["status"] == "PARTIAL"
    assert days[0]["integrity_downgraded_readiness"] is True


def test_integrity_metadata_missing_is_warning_but_invalid_movers_fails(monkeypatch):
    snapshots = {
        "market_snapshot": {"market_date": "2026-09-24", "market": {"core": {}}},
        "movers_snapshot": {"market_date": "2026-09-24", "collection_status": "FAILED", "symbols_valid": 56},
    }
    monkeypatch.setattr(batch.replay, "_dated_snapshot", lambda output_dir, prefix, market_date: snapshots[prefix])
    integrity = batch.evaluate_integrity({"market_date": "2026-09-24", "market_discovery_cache_state": "MISS"})
    assert integrity["integrity_status"] == "FAIL"
    assert "MARKET_INTEGRITY_METADATA_MISSING" in integrity["integrity_reasons"]
    assert "INVALID_REQUIRED_MOVER_ARTIFACT" in integrity["integrity_reasons"]


def test_manual_label_preserved_and_marked_stale_after_integrity_failure(tmp_path):
    path = tmp_path / "market.csv"
    path.write_text("date,market_manual_label,review_notes\n2026-09-24,KEEP,reviewed\n", encoding="utf-8")
    fields = ["date", "market_manual_label", "manual_label_eligible", "manual_label_stale", "review_notes"]
    batch._write_csv(path, [{"date": "2026-09-24", "market_manual_label": "", "integrity_status": "FAIL"}], fields, ("date",))
    with path.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["market_manual_label"] == "KEEP"
    assert row["review_notes"] == "reviewed"
    assert row["manual_label_eligible"] == "False"
    assert row["manual_label_stale"] == "True"


def test_integrity_detects_future_and_cross_symbol_selected_evidence(monkeypatch):
    snapshots = {
        "market_snapshot": {"market_date": "2026-09-24", "historical_integrity": {"requested_market_date": "2026-09-24", "future_data_leak_detected": False, "exact_price_series": []}, "market": {"core": {}}},
        "movers_snapshot": {"market_date": "2026-09-24", "collection_status": "SUCCESS", "historical_integrity": {"integrity_status": "PASS", "max_source_date": "2026-09-24"}},
    }
    monkeypatch.setattr(batch.replay, "_dated_snapshot", lambda output_dir, prefix, market_date: snapshots[prefix])
    day = {"market_date": "2026-09-24", "market_discovery_cache_state": "HIT", "stock_records": [{"symbol": "META", "selected_event": "future", "selected_published_at": "2026-09-25T12:00:00Z", "selected_affected_symbols": ["AAPL"]}], "news_context": {"verification": {"offline_network_attempts_blocked": 0}}}
    integrity = batch.evaluate_integrity(day)
    assert integrity["integrity_status"] == "FAIL"
    assert "FUTURE_EVENT_SELECTED" in integrity["integrity_reasons"]
    assert "CROSS_SYMBOL_EVIDENCE_SELECTED" in integrity["integrity_reasons"]


def test_offline_guard_blocks_network_and_fmp_replay_stays_disabled(monkeypatch):
    with replay.offline_network_guard():
        try:
            replay.requests.get("https://example.invalid")
        except RuntimeError as exc:
            assert str(exc) == "OFFLINE_NETWORK_CALL_BLOCKED"
        else:
            raise AssertionError("offline guard did not block network")
    result = replay._replay_cached_verification("2026-09-24", [])
    assert result["offline_network_attempts_blocked"] == 0
    assert result["verification_evidence_attempts"] == 0
