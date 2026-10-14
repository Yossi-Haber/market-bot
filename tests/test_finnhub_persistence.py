import json

from news.providers.finnhub import CACHE_SCHEMA_VERSION, FinnhubProvider


class Response:
    status_code = 200

    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http_error")

    def json(self):
        return self.payload


def _payload():
    return [{
        "id": "ba-1",
        "headline": "FAA delays Boeing 737 Max 10 certification over software glitch",
        "summary": "FAA review delays certification.",
        "url": "https://example.test/ba-1",
        "source": "Reuters",
        "datetime": 1790611200,
    }]


def test_successful_discovery_writes_normalized_cache(tmp_path):
    provider = FinnhubProvider(token="secret", request_get=lambda *a, **k: Response(_payload()), cache_dir=tmp_path)
    result = provider.search_company("BA", "2026-09-28", "2026-09-29")
    path = tmp_path / "BA" / "2026-09-28_2026-09-29.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert result and path.exists()
    assert payload["schema_version"] == CACHE_SCHEMA_VERSION
    assert payload["provider"] == "FINNHUB"
    assert payload["normalized_candidates"][0]["title"].startswith("FAA delays")
    assert provider.cache_writes == 1


def test_exact_window_reuses_cache_without_network(tmp_path):
    calls = []
    first = FinnhubProvider(token="secret", request_get=lambda *a, **k: (calls.append(1) or Response(_payload())), cache_dir=tmp_path)
    first.search_company("BA", "2026-09-28", "2026-09-29")
    second = FinnhubProvider(token="secret", request_get=lambda *a, **k: (calls.append(2) or Response([])), cache_dir=tmp_path)
    result = second.search_company("BA", "2026-09-28", "2026-09-29")
    assert len(result) == 1
    assert calls == [1]
    assert second.cache_hits == 1
    assert second.live_requests == 0


def test_different_window_is_cache_miss_and_live_request(tmp_path):
    calls = []
    provider = FinnhubProvider(token="secret", request_get=lambda *a, **k: (calls.append(k["params"]) or Response(_payload())), cache_dir=tmp_path)
    provider.search_company("BA", "2026-09-28", "2026-09-29")
    provider.search_company("BA", "2026-09-29", "2026-09-30")
    assert len(calls) == 2
    assert provider.cache_misses == 2


def test_incompatible_schema_is_safe_cache_miss(tmp_path):
    path = tmp_path / "BA" / "2026-09-28_2026-09-29.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": "old", "provider": "FINNHUB"}), encoding="utf-8")
    provider = FinnhubProvider(token="secret", request_get=lambda *a, **k: Response([]), cache_dir=tmp_path)
    assert provider.search_company("BA", "2026-09-28", "2026-09-29") == []
    assert provider.cache_incompatible == 1
    assert provider.last_cache_status == "WRITTEN"


def test_failed_request_does_not_write_valid_cache(tmp_path):
    def fail(*args, **kwargs):
        raise OSError("offline")

    provider = FinnhubProvider(token="secret", request_get=fail, cache_dir=tmp_path)
    assert provider.search_company("BA", "2026-09-28", "2026-09-29") == []
    assert not (tmp_path / "BA" / "2026-09-28_2026-09-29.json").exists()
    assert provider.cache_writes == 0


def test_refresh_cache_bypasses_existing_cache(tmp_path):
    calls = []
    request = lambda *a, **k: (calls.append(1) or Response(_payload()))
    FinnhubProvider(token="secret", request_get=request, cache_dir=tmp_path).search_company("BA", "2026-09-28", "2026-09-29")
    refreshed = FinnhubProvider(token="secret", request_get=request, cache_dir=tmp_path, refresh_cache=True)
    refreshed.search_company("BA", "2026-09-28", "2026-09-29")
    assert len(calls) == 2
    assert refreshed.live_requests == 1
