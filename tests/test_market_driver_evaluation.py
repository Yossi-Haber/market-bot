import json
from datetime import datetime, timezone
from pathlib import Path

import evaluate_market_drivers as evaluation
from news.providers.fmp import FMPProvider
from news.schema import NewsCandidate
from news.verification_acquisition import _safe_cache_name


def _write_snapshot(path, name, market_date, payload):
    value = {"market_date": market_date, **payload}
    (path / name).write_text(json.dumps(value), encoding="utf-8")


def _minimal_cache(path, market_date="2026-09-28"):
    market = {"market": {"core": {symbol: {"close": 100, "change_pct": 0.1} for symbol in ("SPY", "QQQ", "DIA", "IWM")}}}
    movers = {"top_gainers": [{"symbol": "TEST", "change_pct": 5.0}], "top_losers": []}
    _write_snapshot(path, "market_snapshot.json", market_date, market)
    _write_snapshot(path, "movers_snapshot.json", market_date, movers)
    _write_snapshot(path, "daily_report_data.json", market_date, {"verified_stories": [], "candidate_stories": []})


def test_multi_day_cached_evaluation_and_missing_date(tmp_path):
    _minimal_cache(tmp_path, "2026-09-25")
    _write_snapshot(tmp_path, "market_snapshot_2026-09-28.json", "2026-09-28", {"market": {"core": {}}})
    _write_snapshot(tmp_path, "movers_snapshot_2026-09-28.json", "2026-09-28", {"top_gainers": [], "top_losers": []})
    result = evaluation.evaluate_market_drivers("2026-09-25", "2026-09-28", tmp_path)
    assert result["cached_dates_detected"] == ["2026-09-25", "2026-09-28"]
    assert result["days_evaluated"] == 2
    assert result["days_skipped_missing_cache"] == 0


def test_missing_cache_is_explicitly_skipped(tmp_path):
    _minimal_cache(tmp_path)
    result = evaluation.evaluate_market_drivers("2026-09-25", "2026-09-29", tmp_path)
    assert {item["status"] for item in result["skipped_dates"]} == {"SKIPPED_MISSING_CACHE"}
    assert "2026-09-29" in {item["market_date"] for item in result["skipped_dates"]}


def test_evaluation_has_zero_network_guard(tmp_path, monkeypatch):
    _minimal_cache(tmp_path)
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network")))
    result = evaluation.evaluate_market_drivers("2026-09-28", "2026-09-28", tmp_path)
    assert result["days_evaluated"] == 1


def test_fmp_402_disables_provider_and_skips_second_endpoint():
    calls = []

    class Response:
        status_code = 402

        def raise_for_status(self):
            return None

        def json(self):
            return {"Error Message": "Restricted Endpoint"}

    provider = FMPProvider(token="secret", request_get=lambda *args, **kwargs: (calls.append(args[0]) or Response()))
    assert provider.search_company("BA", "2026-09-28", "2026-09-29") == []
    assert provider.status == "FMP_PLAN_RESTRICTION"
    assert provider.search_press_releases("BA", "2026-09-28", "2026-09-29") == []
    assert calls == ["https://financialmodelingprep.com/api/v3/stock_news"]
    assert provider.requests_skipped_plan_restriction == 1


def test_unexplained_large_move_and_duplicate_driver_not_counted_twice(tmp_path, monkeypatch):
    _minimal_cache(tmp_path)
    duplicate = {"driver_id": "STOCK_TEST", "verification_status": "NO_CATALYST_FOUND", "related_symbols": ["TEST"], "movement_evidence": [{"value": 5}]}
    monkeypatch.setattr(evaluation, "build_driver_analysis", lambda _: {"main_market_driver": duplicate, "sector_drivers": [], "stock_catalysts": [duplicate], "metrics": {}})
    result = evaluation.evaluate_market_drivers("2026-09-28", "2026-09-28", tmp_path)
    day = result["days"][0]
    assert day["unexplained_moves"] == 1
    assert "UNEXPLAINED_LARGE_MOVE" in day["warnings"]
    assert "DUPLICATE_DRIVER_EVENT" in day["warnings"]


def test_output_files_are_written(tmp_path):
    _minimal_cache(tmp_path)
    result = evaluation.evaluate_market_drivers("2026-09-28", "2026-09-28", tmp_path)
    (tmp_path / "market_driver_evaluation.json").write_text(json.dumps(result), encoding="utf-8")
    (tmp_path / "market_driver_evaluation.md").write_text(evaluation._markdown(result), encoding="utf-8")
    assert (tmp_path / "market_driver_evaluation.json").exists()
    assert "TOP REVIEW ITEMS" in (tmp_path / "market_driver_evaluation.md").read_text(encoding="utf-8")


def test_json_serializer_handles_nested_finnhub_candidate_trace():
    candidate = NewsCandidate(
        title="FAA delays Boeing 737 Max 10 certification",
        summary="Software issue delays certification.",
        published_at="2026-09-28T12:00:00+00:00",
        source_name="Reuters",
        source_domain="reuters.com",
        source_tier=3,
        source_url="https://example.test/ba",
        category="REGULATORY",
        affected_symbols=["BA"],
        discovery_source="finnhub",
        raw_metadata={"observed_at": datetime(2026, 9, 28, tzinfo=timezone.utc)},
    )
    value = {"trace": {"top_cluster": {"canonical_event": "FAA / MAX certification", "event_type": "REGULATORY", "articles": [candidate]}, "cache_path": Path("cache/finnhub/BA/window.json")}}
    safe = evaluation.to_json_safe(value)
    encoded = json.dumps(safe, ensure_ascii=False)
    assert "FAA / MAX certification" in encoded
    assert safe["trace"]["top_cluster"]["articles"][0]["title"].startswith("FAA delays")
    assert Path(safe["trace"]["cache_path"]).as_posix() == "cache/finnhub/BA/window.json"


def _verification_candidate():
    return {"title": "FAA regulatory review delays Boeing 737 Max 10 certification", "summary": "Software issue delays certification.", "published_at": "2026-09-28T12:00:00+00:00", "category": "REGULATORY", "event_type": "REGULATORY", "affected_symbols": ["BA"], "source_url": "https://finnhub.test/ba", "discovery_source": "finnhub"}


def test_cached_official_evidence_is_replayed_and_confirms(tmp_path, monkeypatch):
    import news.verification_acquisition as acquisition

    monkeypatch.setattr(acquisition, "CACHE_DIR", tmp_path)
    candidate = _verification_candidate()
    path = tmp_path / "2026-09-28" / _safe_cache_name(candidate, "official_discovery")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([{"source_type": "OFFICIAL", "source_domain": "faa.gov", "title": candidate["title"], "summary": candidate["summary"], "published_at": candidate["published_at"], "affected_symbols": ["BA"], "source_url": "https://faa.gov/notice"}]), encoding="utf-8")
    replay = evaluation._replay_cached_verification("2026-09-28", [candidate])
    assert replay["verification_cache_hits"] == 1
    assert replay["verification_confirmed"] == 1
    assert replay["official_evidence_hits"] == 1
    assert replay["verification_evidence_attempts"] > 0


def test_cached_tier2_evidence_is_replayed(tmp_path, monkeypatch):
    import news.verification_acquisition as acquisition

    monkeypatch.setattr(acquisition, "CACHE_DIR", tmp_path)
    candidate = _verification_candidate()
    path = tmp_path / "2026-09-28" / _safe_cache_name(candidate, "tier2_search")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([{"source_type": "TIER2", "source_domain": "reuters.com", "title": candidate["title"], "summary": candidate["summary"], "published_at": candidate["published_at"], "affected_symbols": ["BA"], "source_url": "https://reuters.com/ba"}]), encoding="utf-8")
    replay = evaluation._replay_cached_verification("2026-09-28", [candidate])
    assert replay["tier2_evidence_hits"] == 1
    assert replay["verification_confirmed"] == 1


def test_missing_cached_evidence_is_plausible_but_has_attempt_diagnostics(tmp_path, monkeypatch):
    import news.verification_acquisition as acquisition

    monkeypatch.setattr(acquisition, "CACHE_DIR", tmp_path)
    replay = evaluation._replay_cached_verification("2026-09-28", [_verification_candidate()])
    assert replay["verification_confirmed"] == 0
    assert replay["verification_not_confirmed"] == 1
    assert replay["verification_evidence_attempts"] > 0
    assert replay["offline_network_attempts_blocked"] == 0


def test_regulatory_replay_keeps_official_route_before_tier2(tmp_path, monkeypatch):
    import news.verification_acquisition as acquisition

    monkeypatch.setattr(acquisition, "CACHE_DIR", tmp_path)
    replay = evaluation._replay_cached_verification("2026-09-28", [_verification_candidate()])
    attempts = replay["records"][0]["attempted_sources"]
    types = [item["type"] for item in attempts]
    assert types[0].startswith("REGULATOR_OFFICIAL")
    assert types.index("OFFICIAL_IR_DISCOVERY") < types.index("TIER2_SEARCH")


def test_cached_catalyst_cluster_reaches_evaluator_and_provider_counts(tmp_path, monkeypatch):
    _minimal_cache(tmp_path)
    movers = {"top_gainers": [], "top_losers": [{"symbol": "BA", "change_pct": -6.9}]}
    (tmp_path / "movers_snapshot.json").write_text(json.dumps({"market_date": "2026-09-28", **movers}), encoding="utf-8")
    cache_root = tmp_path / "cache"
    (cache_root / "news" / "2026-09-28").mkdir(parents=True)
    (cache_root / "news" / "2026-09-28" / "BA.json").write_text(json.dumps({
        "catalyst_status": "POSSIBLE",
        "catalyst_type": "REGULATORY",
        "headline": "FAA delays Boeing 737 Max 10 certification over software glitch",
        "published_at": "2026-09-28T12:00:00+00:00",
        "independent_source_count": 3,
        "discovery_audit": {"finnhub_candidates": 26, "marketaux_candidates": 0},
        "event_cluster": {"underlying_event_id": "BA|737_MAX|REGULATORY", "canonical_event_title": "FAA delays Boeing 737 Max 10 certification", "independent_source_count": 3, "article_count": 26}
    }), encoding="utf-8")
    monkeypatch.setattr(evaluation, "CACHE_DIR", cache_root)
    result = evaluation.evaluate_market_drivers("2026-09-28", "2026-09-28", tmp_path, trace_symbol="BA")
    assert result["metrics"]["finnhub_candidates_used"] == 26
    assert result["trace"]["cached_candidates_found"] == 1
    assert result["trace"]["top_cluster"]["event_type"] == "REGULATORY"


def test_finnhub_cache_is_clustered_offline(tmp_path, monkeypatch):
    _minimal_cache(tmp_path)
    (tmp_path / "movers_snapshot.json").write_text(json.dumps({"market_date": "2026-09-28", "top_gainers": [], "top_losers": [{"symbol": "BA", "change_pct": -6.9}]}), encoding="utf-8")
    cache_root = tmp_path / "cache"
    finnhub = cache_root / "finnhub" / "BA"
    finnhub.mkdir(parents=True)
    articles = [{"headline": "FAA delays Boeing 737 Max 10 certification over software glitch", "source": "Reuters", "datetime": 1790611200, "url": "https://example.test/ba"}]
    (finnhub / "2026-09-28_2026-09-29.json").write_text(json.dumps({"schema_version": "finnhub-discovery-v1", "provider": "FINNHUB", "symbol": "BA", "requested_from_date": "2026-09-28", "requested_to_date": "2026-09-29", "normalized_candidates": [{"title": articles[0]["headline"], "summary": None, "published_at": "2026-09-28T00:00:00+00:00", "source_name": "Reuters", "source_domain": "", "source_tier": 3, "source_url": articles[0]["url"], "category": "OTHER", "affected_symbols": ["BA"], "discovery_source": "finnhub", "official": False, "verification": "NOT_CONFIRMED", "content_type": "OTHER", "editorial_eligible": False, "article_type": "NEWS_EVENT", "discovery_score": 0.0, "entity_names": [], "provider_article_id": "ba-1", "raw_metadata": articles[0]}]}), encoding="utf-8")
    monkeypatch.setattr(evaluation, "CACHE_DIR", cache_root)
    result = evaluation.evaluate_market_drivers("2026-09-28", "2026-09-28", tmp_path)
    story = result["days"][0]["news_context"]["candidate_stories"][0]
    assert story["discovery_source"] == "finnhub"
    assert story["event_type"] == "REGULATORY"
