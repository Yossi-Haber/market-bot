from __future__ import annotations

import logging

import pytest
import requests

from news.verification_acquisition import LiveEvidenceAcquirer, _safe_cache_name
from news.verification_v2 import verify_candidate


class Response:
    def __init__(self, status=200, text="<title>FAA regulatory review delays Boeing certification</title>", url="https://www.faa.gov/newsroom", content_type="text/html"):
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.url = url
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


def candidate():
    return {"title": "FAA regulatory review delays Boeing certification", "summary": "FAA review", "published_at": "2026-09-25T12:00:00Z", "category": "REGULATORY", "event_type": "REGULATORY", "actors": ["BOEING", "FAA"], "affected_symbols": ["BA"], "source_domain": "finnhub.example", "source_url": "https://finnhub.example/story"}


def test_faa_200_records_safe_http_diagnostics_and_headers(tmp_path, monkeypatch):
    monkeypatch.setattr("news.verification_acquisition.CACHE_DIR", tmp_path)
    calls = []

    def request(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    acquirer = LiveEvidenceAcquirer(logging.getLogger("test"), "2026-09-25", request_get=request)
    evidence, diagnostics = acquirer.acquire([candidate()])[candidate()["title"]]
    assert evidence
    assert diagnostics.regulator_http_status == 200
    assert diagnostics.regulator_error_code is None
    assert diagnostics.regulator_final_url == "https://www.faa.gov/newsroom"
    assert calls[0][1]["allow_redirects"] is True
    assert "User-Agent" in calls[0][1]["headers"]
    assert calls[0][1]["timeout"] == (5, 15)


@pytest.mark.parametrize(("status", "code"), [(403, "REGULATOR_HTTP_403"), (404, "REGULATOR_HTTP_404"), (429, "REGULATOR_HTTP_429")])
def test_faa_http_statuses_are_classified(tmp_path, monkeypatch, status, code):
    monkeypatch.setattr("news.verification_acquisition.CACHE_DIR", tmp_path)

    def request(url, **kwargs):
        if "faa.gov" in url:
            return Response(status=status)
        return Response(text="<rss><channel></channel></rss>", url=url, content_type="application/rss+xml")

    acquirer = LiveEvidenceAcquirer(logging.getLogger("test"), "2026-09-25", request_get=request)
    _, diagnostics = acquirer.acquire([candidate()])[candidate()["title"]]
    assert diagnostics.regulator_error_code == code
    assert diagnostics.regulator_http_status == status
    assert diagnostics.regulator_fallback_used is True


def test_regulator_timeout_is_classified(tmp_path, monkeypatch):
    monkeypatch.setattr("news.verification_acquisition.CACHE_DIR", tmp_path)

    def request(url, **kwargs):
        if "faa.gov" in url:
            raise requests.Timeout("read timeout")
        return Response(text="<rss><channel></channel></rss>", url=url, content_type="application/rss+xml")

    acquirer = LiveEvidenceAcquirer(logging.getLogger("test"), "2026-09-25", request_get=request)
    _, diagnostics = acquirer.acquire([candidate()])[candidate()["title"]]
    assert diagnostics.regulator_error_code == "REGULATOR_TIMEOUT"


def test_redirect_final_url_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr("news.verification_acquisition.CACHE_DIR", tmp_path)
    redirected = Response(url="https://www.faa.gov/newsroom/boeing-max-notice")
    acquirer = LiveEvidenceAcquirer(logging.getLogger("test"), "2026-09-25", request_get=lambda *a, **k: redirected)
    _, diagnostics = acquirer.acquire([candidate()])[candidate()["title"]]
    assert diagnostics.regulator_final_url.endswith("boeing-max-notice")


def test_same_domain_official_fallback_is_used(tmp_path, monkeypatch):
    monkeypatch.setattr("news.verification_acquisition.CACHE_DIR", tmp_path)
    rss = b"<rss><channel><item><title>FAA regulatory review delays Boeing certification</title><link>https://www.faa.gov/newsroom/notice</link><pubDate>Thu, 25 Sep 2026 12:05:00 GMT</pubDate></item></channel></rss>"

    def request(url, **kwargs):
        if url == "https://www.faa.gov/newsroom":
            return Response(status=500)
        return Response(text=rss.decode(), url="https://news.google.com/rss", content_type="application/rss+xml")

    acquirer = LiveEvidenceAcquirer(logging.getLogger("test"), "2026-09-25", request_get=request)
    evidence, diagnostics = acquirer.acquire([candidate()])[candidate()["title"]]
    assert diagnostics.regulator_fallback_used is True
    assert evidence[0]["source_domain"] == "faa.gov"


def test_failed_refresh_preserves_valid_regulator_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("news.verification_acquisition.CACHE_DIR", tmp_path)
    item = {"source_type": "OFFICIAL", "source_domain": "faa.gov", "title": candidate()["title"], "published_at": candidate()["published_at"], "affected_symbols": ["BA"], "source_url": "https://www.faa.gov/notice"}
    path = tmp_path / "2026-09-25" / _safe_cache_name(candidate(), "regulator_FAA_faa.gov")
    path.parent.mkdir(parents=True)
    path.write_text(__import__("json").dumps([item]), encoding="utf-8")

    def failed(url, **kwargs):
        if "faa.gov" in url:
            return Response(status=403)
        return Response(text="<rss><channel></channel></rss>", url=url, content_type="application/rss+xml")

    LiveEvidenceAcquirer(logging.getLogger("test"), "2026-09-25", request_get=failed, refresh_cache=True).acquire([candidate()])
    replay = LiveEvidenceAcquirer(logging.getLogger("test"), "2026-09-25", cache_only=True).acquire([candidate()])
    evidence, _ = replay[candidate()["title"]]
    assert evidence and evidence[0]["source_domain"] == "faa.gov"
