"""Synthetic V2 fixtures; no production cache or output dependencies."""
import copy
import json
import socket
import time

import pytest

from research_v2.context import peer_context
from research_v2.contracts import validate_packet, validate_research
from research_v2.evidence import build_packet, session_bounds, temporal_tag
from research_v2.research import FakeProvider, ResearchRunner
from research_v2.replay import replay


def packet(rows=None, provider="finnhub", **kwargs):
    rows = rows if rows is not None else [{"headline": "Synthetic company announces contract", "source": "Synthetic Publisher", "url": "https://example.com/article", "published_at": "2026-10-28T15:00:00Z", "summary": "Synthetic company announced a contract."}]
    return build_packet("2026-10-28", [{"provider": provider, "payload": {"data": rows} if provider == "marketaux" else rows, "coverage_state": "AVAILABLE"}], **kwargs)


def research(p):
    eid = p["evidence"][0]["evidence_id"]
    return {"research_version": 2, "proposal_only": True, "events": [{"event_id": "event1", "scope": "company", "event_type": "CONTRACT", "securities": ["TEST"], "sectors": [], "factual_summary": "Synthetic company announced a contract.", "evidence_refs": [eid], "supporting_quotations": [{"evidence_id": eid, "quote": "Synthetic company announced a contract."}], "event_verification": "SINGLE_SOURCE_REPORTED", "unresolved_questions": ["Price impact not established."]}], "movement_assessments": [{"subject": "TEST", "linked_event_ids": ["event1"], "attribution": "UNEXPLAINED", "rationale": "No causal evidence supplied.", "alternative_interpretations": [], "evidence_refs": [eid], "abstention_reason": "Causal relationship unverified."}]}


def test_packet_roundtrip_and_provenance():
    p = packet()
    assert validate_packet(json.loads(json.dumps(p))) == p
    assert p["evidence"][0]["source_text"] == "Synthetic company announced a contract."
    assert p["evidence"][0]["content_completeness"] == "EXCERPT"
    assert p["evidence"][0]["evidence_id"] == packet()["evidence"][0]["evidence_id"]


def test_evidence_identity_detects_mutated_source_text():
    p = packet()
    p["evidence"][0]["source_text"] = "Changed text"
    with pytest.raises(ValueError, match="EVIDENCE_CONTENT_CHANGED"):
        validate_packet(p)


@pytest.mark.parametrize("stamp,tag", [("2026-10-27T21:00:00Z", "PREVIOUS_AFTER_HOURS"), ("2026-10-28T12:00:00Z", "PRE_MARKET"), ("2026-10-28T15:00:00Z", "REGULAR_SESSION"), ("2026-10-28T20:00:00Z", "POST_CLOSE"), ("2026-10-29T15:00:00Z", "OUTSIDE_WINDOW")])
def test_temporal_coverage(stamp, tag):
    assert temporal_tag(stamp, session_bounds("2026-10-28")) == tag


def test_early_close_and_previous_trading_session():
    bounds = session_bounds("2026-11-27")
    assert bounds["regular_close_utc"] == "2026-11-27T18:00:00+00:00"
    assert bounds["previous_session_close_utc"] == "2026-11-25T21:00:00+00:00"
    assert temporal_tag("2026-11-27T18:01:00Z", bounds) == "POST_CLOSE"


def test_missing_body_dedup_and_no_semantic_filter():
    row = {"title": "Synthetic forecast preview", "source": "Synthetic", "url": "https://example.com/a", "published_at": "2026-10-28T15:00:00Z"}
    p = packet([row, row], "marketaux")
    assert len(p["evidence"]) == 1
    assert p["evidence"][0]["source_text"] == ""
    assert p["evidence"][0]["content_completeness"] == "TITLE_ONLY"
    assert p["diagnostics"]["hygiene_rejections"]["DUPLICATE"] == 1


@pytest.mark.parametrize("state", ["NO_EVIDENCE", "UNAVAILABLE", "QUOTA_EXHAUSTED", "INCOMPLETE_CONTENT", "HYGIENE_EXCLUDED", "BUDGET_NOT_COLLECTED"])
def test_explicit_coverage(state):
    p = build_packet("2026-10-28", [{"provider": "marketaux", "coverage_state": state}])
    assert p["coverage"][0]["state"] == state


@pytest.mark.parametrize("stamp", [None, "not-a-date", "2026-10-28T15:00:00"])
def test_invalid_timestamps_excluded(stamp):
    assert not packet([{"title": "Synthetic", "source": "Synthetic", "url": "https://example.com/a", "published_at": stamp}])["evidence"]


def test_sec_metadata_preserved():
    p = packet([{"title": "Synthetic filing", "source": "SEC EDGAR", "url": "https://www.sec.gov/Archives/example", "published_at": "2026-10-28T15:00:00Z", "filing_type": "8-K", "sec_items": ["2.02"], "exhibit_text": "Synthetic exhibit"}], "sec")
    assert p["evidence"][0]["filing_metadata"]["exhibit_text"] == "Synthetic exhibit"


def test_existing_sec_cache_shape_does_not_imply_precise_publication():
    p = packet([{"title": "SEC 8-K filing", "source_domain": "sec.gov", "source_url": "https://www.sec.gov/Archives/example", "published_at": "2026-10-28T00:00:00+00:00", "filing_form": "8-K", "filing_items": "2.02", "exhibit_text": "Synthetic exhibit"}], "sec")
    item = validate_packet(p)["evidence"][0]
    assert item["temporal_tag"] == "TIME_UNCERTAIN"
    assert item["source_text"] == "Synthetic exhibit"
    assert item["publication_precision"] == "DATE_ONLY"


def test_budget_reports_exclusion():
    rows = [{"title": "Synthetic", "source": "Synthetic", "url": f"https://example.com/{i}", "published_at": "2026-10-28T15:00:00Z"} for i in range(2)]
    assert packet(rows, max_items=1)["coverage"][0]["budget_excluded"] == 1


def test_peer_context_and_unavailable_history():
    result = peer_context([{"symbol": s, "daily_return": r, "sector_etf": "SOXX"} for s, r in [("MU", 5.86), ("LRCX", 5.39), ("AMAT", 5.07)]], {"semiconductors": ["MU", "LRCX", "AMAT"]}, {"SOXX": 4})
    assert len(result["co_movement"]) == 1
    assert result["co_movement"][0]["causal_claim"] is False
    assert result["securities"][0]["relative_volume"] is None
    assert result["securities"][0]["return_anomaly"] is None
    assert result["securities"][0]["relative_return"] == pytest.approx(1.86)


def test_historical_metrics():
    result = peer_context([{"symbol": "TEST", "daily_return": 4, "volume": 200, "historical_volumes": [100]*20, "historical_returns": [1, -1]*10}], {}, {})
    assert result["securities"][0]["relative_volume"] == 2
    assert result["securities"][0]["rolling_volatility"] == 1


def test_research_contract_and_fake_usage():
    p = packet()
    result = ResearchRunner(FakeProvider(research(p)), model="synthetic").run(p)
    assert result["status"] == "PROPOSAL"
    assert result["diagnostics"]["simulated"] is True


@pytest.mark.parametrize("mutation", ["missing", "unknown_evidence", "unknown_event", "quote"])
def test_invalid_research_contract(mutation):
    p = packet()
    r = research(p)
    if mutation == "missing": del r["events"][0]["scope"]
    elif mutation == "unknown_evidence": r["events"][0]["evidence_refs"] = ["invented"]
    elif mutation == "unknown_event": r["movement_assessments"][0]["linked_event_ids"] = ["invented"]
    else: r["events"][0]["supporting_quotations"][0]["evidence_id"] = "invented"
    with pytest.raises(ValueError): validate_research(r, p)


@pytest.mark.parametrize("response,status", [("{", "completed"), ({}, "incomplete"), (TimeoutError(), "completed")])
def test_malformed_truncated_timeout(response, status):
    result = ResearchRunner(FakeProvider(response, status=status), model="synthetic").run(packet())
    assert result["status"] in {"INVALID_OR_FAILED", "TIMEOUT"}


def test_requests_tokens_and_missing_credentials():
    p = packet()
    fake = FakeProvider(research(p))
    runner = ResearchRunner(fake, model="synthetic")
    assert runner.run(p)["status"] == "PROPOSAL"
    assert runner.run(p)["status"] == "BUDGET_EXHAUSTED"
    assert fake.calls == 1
    assert ResearchRunner(fake, model="synthetic", max_total_tokens=1).run(p)["status"] == "BUDGET_EXHAUSTED"
    class Unavailable:
        def complete(self, **kwargs): raise AssertionError("must not call")
    assert ResearchRunner(Unavailable(), model="configured").run(p)["status"] == "UNAVAILABLE"


def test_hanging_provider_bounded():
    class Slow(FakeProvider):
        def complete(self, **kwargs):
            time.sleep(.1)
            return super().complete(**kwargs)
    runner = ResearchRunner(Slow({}), model="synthetic", timeout_seconds=.01)
    assert runner.run(packet())["status"] == "TIMEOUT"
    assert runner.run(packet())["status"] == "BUDGET_EXHAUSTED"


def test_offline_replay(tmp_path, monkeypatch):
    p = packet()
    (tmp_path / "packet.json").write_text(json.dumps(p))
    (tmp_path / "response.json").write_text(json.dumps(research(p)))
    import research_v2.replay as module
    original = module.ResearchRunner.run
    def checked(self, value):
        with pytest.raises(RuntimeError, match="V2_REPLAY_NETWORK_BLOCKED"):
            socket.create_connection(("example.com", 443))
        return original(self, value)
    monkeypatch.setattr(module.ResearchRunner, "run", checked)
    assert replay(tmp_path / "packet.json", tmp_path / "response.json", tmp_path / "out")["status"] == "PROPOSAL"
    assert json.loads((tmp_path / "out" / "diagnostics.json").read_text())["live_requests"] == 0
