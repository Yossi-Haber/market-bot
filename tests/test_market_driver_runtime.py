from __future__ import annotations

import json
import logging

from drivers import runtime
from news.schema import NewsCandidate
from news.verification_acquisition import resolve_verification_route


def _snapshots(tmp_path):
    market = {
        "market_date": "2026-09-28",
        "market": {
            "core": {symbol: {"change_pct": -1.0} for symbol in ("SPY", "QQQ", "DIA", "IWM")},
            "macro": {},
            "sectors": {"all": []},
        },
    }
    movers = {"market_date": "2026-09-28", "top_gainers": [], "top_losers": [{"symbol": "BA", "company_name": "Boeing", "change_pct": -6.9}]}
    (tmp_path / "market_snapshot.json").write_text(json.dumps(market), encoding="utf-8")
    (tmp_path / "movers_snapshot.json").write_text(json.dumps(movers), encoding="utf-8")


def test_production_driver_stage_is_symbol_scoped_and_fmp_free(tmp_path, monkeypatch):
    _snapshots(tmp_path)
    article = NewsCandidate("FAA delays Boeing 737 Max 10 certification over software glitch", "FAA review", "2026-09-28T12:00:00+00:00", "Reuters", "reuters.com", 3, "https://reuters.example/ba", "REGULATORY", ["BA"], "finnhub")

    class FakeFinnhub:
        live_requests = 0
        cache_hits = 1
        cache_misses = 0
        calls = []

        def __init__(self, **kwargs):
            pass

        def search_company(self, symbol, start, end):
            assert symbol == "BA"
            self.calls.append((symbol, start, end))
            return [article]

    class Diagnostics:
        verification_route = resolve_verification_route({"event_type": "REGULATORY", "title": article.title, "actors": ["BOEING", "FAA"]})
        attempted_sources = [{"type": "REGULATOR_OFFICIAL:faa.gov", "status": "CACHE_MISS"}]

    class FakeAcquirer:
        def __init__(self, logger, market_date, **kwargs):
            assert kwargs["fmp_provider"].enabled is False

        def acquire(self, candidates):
            return {candidates[0]["title"]: ([], Diagnostics())}

    monkeypatch.setattr(runtime, "FinnhubProvider", FakeFinnhub)
    monkeypatch.setattr(runtime, "LiveEvidenceAcquirer", FakeAcquirer)
    path = runtime.run_market_driver_pipeline("2026-09-28", tmp_path, logger=logging.getLogger("test"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    ba = next(item for item in payload["driver_analysis"]["stock_catalysts"] if item["driver_id"] == "STOCK_BA")
    assert payload["market_drivers_source_of_truth"] is True
    assert payload["legacy_catalyst_used_for_driver"] is False
    assert ba["verification_status"] == "PLAUSIBLE_NOT_VERIFIED"
    assert payload["metrics"]["fmp_requests"] == 0
    assert payload["verification_records"][0]["route"][0] == "REGULATOR_OFFICIAL"
    assert FakeFinnhub.calls == [("BA", "2026-09-28", "2026-09-29")]
