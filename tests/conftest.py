"""Keep every test's persistent cache state inside its pytest temp directory."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def isolate_project_caches(tmp_path, monkeypatch):
    """Redirect all known cache writers away from the repository cache."""
    root = tmp_path / "cache"
    targets = {
        "evaluate_market_drivers": {"CACHE_DIR": root},
        "news.alpha_vantage": {"CACHE_DIR": root / "alpha_vantage"},
        "news.catalyst": {"CACHE_DIR": root / "news"},
        "news.discovery": {"CACHE_DIR": root / "news_discovery"},
        "news.marketaux": {"CACHE_DIR": root / "marketaux"},
        "news.official": {"CACHE_DIR": root / "official_news"},
        "news.targeted_search": {"CACHE_DIR": root / "targeted_search"},
        "news.verification_acquisition": {
            "CACHE_DIR": root / "verification_v2",
            "OFFICIAL_DISCOVERY_CACHE_DIR": root / "verification_official_domains",
        },
        "news.verify": {"CACHE_DIR": root / "verification"},
        "news.providers.finnhub": {"CACHE_DIR": root / "finnhub"},
    }
    for module_name, values in targets.items():
        module = importlib.import_module(module_name)
        for name, value in values.items():
            monkeypatch.setattr(module, name, value)
    monkeypatch.setenv("MARKET_BOT_CACHE_DIR", str(root))
