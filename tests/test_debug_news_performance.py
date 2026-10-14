import json
import logging

import debug_news_performance as debug


def _snapshot(tmp_path):
    payload = {
        "top_gainers": [{"symbol": symbol} for symbol in ("MCHP", "CVS", "PYPL", "HUM", "BKNG")],
        "top_losers": [{"symbol": symbol} for symbol in ("PANW", "META", "EQIX", "OXY", "CRM")],
    }
    path = tmp_path / "movers_snapshot.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_debug_mover_loader_keeps_all_ten(tmp_path):
    symbols = debug.load_debug_movers(_snapshot(tmp_path), logging.getLogger("debug-test"))
    assert symbols == ["MCHP", "CVS", "PYPL", "HUM", "BKNG", "PANW", "META", "EQIX", "OXY", "CRM"]


class FakeCoverage:
    instances = []
    budget = {
        "official_requests": 1,
        "marketaux_requests": 1,
        "verification_requests": 1,
        "slow_fallback_requests": 0,
        "timeouts": 0,
        "circuit_breakers_opened": 0,
        "budget_exhausted": False,
        "total_news_seconds": 0.1,
        "live_requests_total": 3,
        "marketaux_enabled": False,
        "marketaux_expected": False,
        "marketaux_requests_attempted": 0,
    }

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.__class__.instances.append(self)

    def collect(self, **kwargs):
        self.collect_kwargs = kwargs
        return [], dict(self.budget)


def test_default_debug_keeps_slow_fallback_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(debug, "BASE_DIR", tmp_path)
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "movers_snapshot.json").write_text(json.dumps({"top_gainers": [], "top_losers": []}), encoding="utf-8")
    monkeypatch.delenv("NEWS_DISABLE_SLOW_FALLBACK", raising=False)
    FakeCoverage.instances.clear()
    monkeypatch.setattr(debug, "LiveNewsCoverage", FakeCoverage)
    assert debug.main(["--market-date", "2026-09-25"]) == 0
    assert "NEWS_DISABLE_SLOW_FALLBACK" not in __import__("os").environ
    assert FakeCoverage.instances[0].kwargs["force_live"] is False


def test_disable_slow_fallback_is_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr(debug, "BASE_DIR", tmp_path)
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "movers_snapshot.json").write_text(json.dumps({"top_gainers": [], "top_losers": []}), encoding="utf-8")
    FakeCoverage.instances.clear()
    monkeypatch.setattr(debug, "LiveNewsCoverage", FakeCoverage)
    assert debug.main(["--market-date", "2026-09-25", "--disable-slow-fallback"]) == 0
    assert __import__("os").environ["NEWS_DISABLE_SLOW_FALLBACK"] == "true"


def test_force_live_passes_cache_bypass_and_zero_requests_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(debug, "BASE_DIR", tmp_path)
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "movers_snapshot.json").write_text(json.dumps({"top_gainers": [], "top_losers": []}), encoding="utf-8")
    class ZeroCoverage(FakeCoverage):
        budget = {**FakeCoverage.budget, "live_requests_total": 0}
    ZeroCoverage.instances.clear()
    monkeypatch.setattr(debug, "LiveNewsCoverage", ZeroCoverage)
    assert debug.main(["--market-date", "2026-09-25", "--force-live"]) == 1
    assert ZeroCoverage.instances[0].kwargs["force_live"] is True


def test_configured_marketaux_without_attempt_fails_debug_validation(monkeypatch, tmp_path):
    monkeypatch.setattr(debug, "BASE_DIR", tmp_path)
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "movers_snapshot.json").write_text(json.dumps({"top_gainers": [], "top_losers": []}), encoding="utf-8")

    class SkippedCoverage(FakeCoverage):
        budget = {**FakeCoverage.budget, "marketaux_enabled": True, "marketaux_expected": True, "marketaux_requests_attempted": 0}

    monkeypatch.setattr(debug, "LiveNewsCoverage", SkippedCoverage)
    assert debug.main(["--market-date", "2026-09-25", "--force-live"]) == 1
