from __future__ import annotations

import hashlib
from pathlib import Path

from news.providers.finnhub import FinnhubProvider


def test_default_finnhub_cache_is_redirected_to_test_temp_path(tmp_path):
    provider = FinnhubProvider(token="test")
    assert provider.cache_dir.is_relative_to(tmp_path / "cache")


def test_repository_finnhub_sentinel_is_unchanged_after_test_cache_use(tmp_path):
    sentinel = Path(__file__).resolve().parents[1] / "cache" / "finnhub" / "BA" / "2026-09-28_2026-09-29.json"
    if not sentinel.exists():
        return
    before = hashlib.sha256(sentinel.read_bytes()).digest()
    provider = FinnhubProvider(token="test")
    assert provider.cache_dir.is_relative_to(tmp_path / "cache")
    assert hashlib.sha256(sentinel.read_bytes()).digest() == before
