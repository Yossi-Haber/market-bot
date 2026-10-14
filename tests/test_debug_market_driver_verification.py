from __future__ import annotations

import json

from debug_market_driver_verification import run_debug
from news.schema import NewsCandidate


def _write_finnhub_cache(tmp_path):
    candidate = NewsCandidate(
        "FAA delays Boeing 737 Max 10 certification over software glitch",
        "FAA review of Boeing software delayed certification.",
        "2026-09-28T12:00:00+00:00",
        "Reuters",
        "reuters.com",
        3,
        "https://reuters.example/ba",
        "REGULATORY",
        ["BA"],
        "finnhub",
        raw_metadata={"datetime": 1790596800},
    )
    path = tmp_path / "BA" / "2026-09-28_2026-09-29.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema_version": "finnhub-discovery-v1",
        "provider": "FINNHUB",
        "symbol": "BA",
        "requested_from_date": "2026-09-28",
        "requested_to_date": "2026-09-29",
        "normalized_candidates": [candidate.as_dict()],
    }), encoding="utf-8")


def test_harness_preserves_ba_event_metadata_and_routes_faa_cache_only(tmp_path):
    _write_finnhub_cache(tmp_path)
    trace = run_debug("BA", "2026-09-28", cache_dir=tmp_path)
    assert trace["event_type"] == "REGULATORY"
    assert {"BOEING", "FAA"}.issubset(set(trace["actors"]))
    assert trace["topic_family"] == "737_MAX"
    assert trace["verification_route"][0] == "REGULATOR_OFFICIAL"
    assert trace["source_trace"][0]["status"] == "CACHE_MISS"
    assert trace["http_requests"] == 0
    assert trace["fmp_requests"] == 0


def test_harness_uses_verifier_and_rejects_wrong_symbol_evidence(tmp_path):
    _write_finnhub_cache(tmp_path)
    trace = run_debug("BA", "2026-09-28", cache_dir=tmp_path)
    assert trace["verification_status"] == "NOT_CONFIRMED"
    assert trace["final_driver_status"] == "NOT_CONFIRMED"
