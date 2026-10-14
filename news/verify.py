"""Safe direct-source verification for discovery candidates."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from news.discovery import DiscoveryCandidate
from cache_config import cache_path

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = cache_path("verification")
GOOGLE_HOSTS = {"news.google.com", "www.news.google.com"}


@dataclass
class VerificationResult:
    source_name: str
    source_tier: int
    canonical_url: str
    published_at: str | None
    retrieval_status: str
    url_resolution: str
    title: str
    explicit_market_link: bool
    explicit_market_link_text: str | None
    direct_retrieval: bool
    publisher_domain: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class DirectVerifier:
    """Resolve redirects and inspect safe HTML metadata, never article bodies."""

    def __init__(self, logger: logging.Logger, market_date: str, company_mapping: dict[str, Any]) -> None:
        self.logger = logger
        self.market_date = date.fromisoformat(market_date)
        self.company_mapping = company_mapping
        self.request_count = 0
        self.cache_hits = 0
        self.cache_writes = 0
        self.access_failures: list[str] = []

    def verify(self, candidate: DiscoveryCandidate) -> VerificationResult:
        path = self._cache_path(candidate)
        cached = self._load(path)
        if cached:
            self.cache_hits += 1
            return VerificationResult(**cached)
        parsed = urlparse(candidate.url)
        original_domain = parsed.netloc.lower()
        if original_domain in GOOGLE_HOSTS:
            result = self._verify_google_redirect(candidate)
        else:
            result = self._fetch_direct(candidate.url, candidate)
        self._write(path, result)
        return result

    def _verify_google_redirect(self, candidate: DiscoveryCandidate) -> VerificationResult:
        try:
            self.request_count += 1
            response = requests.get(candidate.url, timeout=(5, 10), allow_redirects=True, headers={"User-Agent": "market_bot/1.0"})
            final_url = response.url
            final_domain = urlparse(final_url).netloc.lower()
            if final_domain in GOOGLE_HOSTS or not final_domain:
                return VerificationResult(candidate.source_domain, 4, candidate.url, candidate.published_at, "REDIRECT_ONLY", "unresolved", candidate.title, False, None, False, candidate.source_domain)
            return self._fetch_direct(final_url, candidate, initial_response=response)
        except requests.Timeout:
            self.access_failures.append("TIER2_TIMEOUT")
            self.logger.warning("Direct verification failed: TIER2_TIMEOUT")
            return VerificationResult(candidate.source_domain, 4, candidate.url, candidate.published_at, "TIER2_TIMEOUT", "unresolved", candidate.title, False, None, False, candidate.source_domain)
        except requests.RequestException as exc:
            self.access_failures.append(candidate.source_domain or "google-news")
            self.logger.info("Direct redirect unresolved for %s: %s", candidate.source_domain, exc)
            return VerificationResult(candidate.source_domain, 4, candidate.url, candidate.published_at, "INACCESSIBLE", "unresolved", candidate.title, False, None, False, candidate.source_domain)

    def _fetch_direct(self, url: str, candidate: DiscoveryCandidate, initial_response: requests.Response | None = None) -> VerificationResult:
        response = initial_response
        try:
            if response is None:
                self.request_count += 1
                response = requests.get(url, timeout=(5, 10), allow_redirects=True, headers={"User-Agent": "market_bot/1.0", "Accept": "text/html,application/xhtml+xml"})
            response.raise_for_status()
            final_url = response.url or url
            html = response.text[:1_000_000]
            title = _metadata_title(html) or candidate.title
            canonical = _canonical_url(html) or final_url
            domain = urlparse(canonical).netloc.lower() or candidate.source_domain.lower()
            tier = publisher_tier(domain, self.company_mapping)
            causal_text = _explicit_market_link_text(title)
            return VerificationResult(_publisher_name(domain), tier, canonical, candidate.published_at, "DIRECT_FULL" if _metadata_title(html) else "DIRECT_METADATA", "resolved", title, bool(causal_text), causal_text, True, domain)
        except requests.Timeout:
            self.access_failures.append("TIER2_TIMEOUT")
            self.logger.warning("Direct verification failed: TIER2_TIMEOUT")
            return VerificationResult(candidate.source_domain, 4, url, candidate.published_at, "TIER2_TIMEOUT", "resolved", candidate.title, False, None, False, candidate.source_domain)
        except (requests.RequestException, UnicodeError) as exc:
            self.access_failures.append(candidate.source_domain or urlparse(url).netloc)
            self.logger.info("Direct source inaccessible for %s: %s", candidate.source_domain, exc)
            return VerificationResult(candidate.source_domain, publisher_tier(candidate.source_domain, self.company_mapping), url, candidate.published_at, "INACCESSIBLE", "unresolved" if urlparse(url).netloc in GOOGLE_HOSTS else "resolved", candidate.title, False, None, False, candidate.source_domain)

    def _cache_path(self, candidate: DiscoveryCandidate) -> Path:
        key = hashlib.sha256(f"{candidate.url}|{candidate.title}|{candidate.published_at}".encode("utf-8")).hexdigest()[:20]
        return CACHE_DIR / self.market_date.isoformat() / f"{key}.json"

    @staticmethod
    def _load(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != 1:
                return None
            payload.pop("schema_version", None)
            return payload
        except (OSError, ValueError):
            return None

    def _write(self, path: Path, result: VerificationResult) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, **result.as_dict()}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.cache_writes += 1


def publisher_tier(domain: str, company_mapping: dict[str, Any]) -> int:
    domain = domain.lower().removeprefix("www.")
    if domain == "reuters.com" or domain.endswith(".reuters.com") or domain == "apnews.com" or domain.endswith(".apnews.com"):
        return 2
    if domain in {"cnbc.com", "marketwatch.com", "barrons.com", "finance.yahoo.com"} or domain.endswith(".cnbc.com"):
        return 3
    for config in company_mapping.values():
        if any(domain in str(config.get(key, "")).lower() for key in ("ir_url", "press_release_url")):
            return 1
    return 4


def _publisher_name(domain: str) -> str:
    domain = domain.lower().removeprefix("www.")
    return {"reuters.com": "Reuters", "apnews.com": "Associated Press", "cnbc.com": "CNBC", "marketwatch.com": "MarketWatch", "barrons.com": "Barron's", "finance.yahoo.com": "Yahoo Finance"}.get(domain, domain)


def _metadata_title(html: str) -> str | None:
    match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']|<title[^>]*>(.*?)</title>', html, re.I | re.S)
    if not match:
        return None
    return re.sub(r"\s+", " ", next((value for value in match.groups() if value), "")).strip()


def _canonical_url(html: str) -> str | None:
    match = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\'](.*?)["\']|<meta[^>]+property=["\']og:url["\'][^>]+content=["\'](.*?)["\']', html, re.I | re.S)
    return next((value.strip() for value in match.groups() if value and value.startswith("http")), None) if match else None


def _explicit_market_link_text(title: str) -> str | None:
    patterns = (r"[^.]{0,80}shares (?:rose|fell|gain|drop|rise|slide)[^.]{0,100}", r"[^.]{0,80}stock (?:jumped|dropped|gained)[^.]{0,100}")
    for pattern in patterns:
        match = re.search(pattern, title, re.I)
        if match:
            return match.group(0).strip()[:180]
    return None
