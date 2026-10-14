import json
from pathlib import Path

import build_historical_market_cache as builder
from drivers.market_driver_discovery import load_market_discovery_cache


class _FakeMarketaux:
    enabled = True
    available_for_run = True
    request_count = 0

    def __init__(self, *args, **kwargs):
        self.request_count = 0

    def search(self, **kwargs):
        self.request_count += 1
        return []


class _FakeProvider:
    request_count = 0

    def __init__(self, *args, **kwargs):
        self.request_count = 0

    def search(self, query, max_records):
        self.request_count += 1
        return {"status": "searched", "candidates": []}


def _market_snapshot():
    return {
        "market_date": "2026-09-28",
        "market": {"core": {"SPY": {"change_pct": -2}, "QQQ": {"change_pct": -2}, "DIA": {"change_pct": -1}, "IWM": {"change_pct": -2}}, "macro": {"VIX": {"change_pct": 12}}, "sectors": {"all": []}},
    }


def test_zero_result_market_discovery_is_valid_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(builder, "_snapshot_path", lambda prefix, market_date: tmp_path / "market.json")
    monkeypatch.setattr(builder, "_load", lambda path: _market_snapshot())
    monkeypatch.setattr(builder, "MarketauxDiscovery", _FakeMarketaux)
    monkeypatch.setattr(builder, "GdeltClient", _FakeProvider)
    monkeypatch.setattr(builder, "GoogleNewsClient", _FakeProvider)
    result = builder._build_market_news("2026-09-28", refresh=True, logger=builder.logging.getLogger("test"))
    assert result["status"] == "READY"
    assert result["payload"]["offline_ready"] is True
    assert result["payload"]["eligible_candidate_count"] == 0
    assert Path(result["cache_path"]).exists()
    assert load_market_discovery_cache(tmp_path / "cache", "2026-09-28")["market_date"] == "2026-09-28"


def test_future_market_story_is_rejected_and_not_eligible(tmp_path, monkeypatch):
    class FutureProvider(_FakeProvider):
        def search(self, query, max_records):
            self.request_count += 1
            return {"status": "searched", "candidates": [{"title": "Nasdaq falls as Treasury yields jump", "url": "https://example.test/future", "source_domain": "example.test", "published_at": "2026-09-29T15:00:00+00:00", "discovery_source": "GDELT"}]}

    monkeypatch.setattr(builder, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(builder, "_snapshot_path", lambda prefix, market_date: tmp_path / "market.json")
    monkeypatch.setattr(builder, "_load", lambda path: _market_snapshot())
    monkeypatch.setattr(builder, "MarketauxDiscovery", _FakeMarketaux)
    monkeypatch.setattr(builder, "GdeltClient", FutureProvider)
    monkeypatch.setattr(builder, "GoogleNewsClient", _FakeProvider)
    result = builder._build_market_news("2026-09-28", refresh=True, logger=builder.logging.getLogger("test"))
    assert result["payload"]["eligible_candidate_count"] == 0
    assert any(item.get("rejection_reason") == "FUTURE_EVENT" for item in result["payload"]["rejections"])


def test_refresh_market_news_does_not_call_market_or_movers_collection(monkeypatch, tmp_path):
    monkeypatch.setattr(builder, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(builder, "MANIFEST_PATH", tmp_path / "manifest.json")
    monkeypatch.setattr(builder, "_build_market_news", lambda *args, **kwargs: {"market_date": "2026-09-28", "status": "READY", "market_news": "HIT", "cache_path": "cache/market_driver_news/2026-09-28.json", "payload": {"hypotheses": [], "eligible_candidate_count": 0, "fmp_requests": 0}})
    monkeypatch.setattr(builder.app, "main", lambda **kwargs: (_ for _ in ()).throw(AssertionError("market/movers collection called")))
    result = builder.build(["2026-09-28"], refresh_market_news=True)
    assert result["dates"][0]["market_news"] == "HIT"
