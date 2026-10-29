"""Broad candidate discovery, deliberately separate from evidence authority."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from config import DISCOVERY_MAX_QUERIES_PER_SYMBOL, DISCOVERY_MAX_RESULTS_PER_QUERY, DISCOVERY_REQUEST_DELAY_SECONDS
from cache_config import cache_path
from news.gdelt import GdeltClient
from news.evidence_capture import observe, active as capture_active
from news.google_news import GoogleNewsClient


BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = cache_path("news_discovery")


@dataclass
class DiscoveryCandidate:
    title: str
    url: str
    source_domain: str
    published_at: str | None
    language: str | None
    source_country: str | None
    discovery_source: str
    url_resolution: str
    retrieval_status: str
    snippet: str | None = None
    publisher_hint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class BroadDiscovery:
    """Run narrow, deterministic GDELT and Google News searches with caching."""

    def __init__(self, logger: logging.Logger, market_date: str, *, gdelt_request_get: Callable[..., Any] | None = None, google_request_get: Callable[..., Any] | None = None, refresh_cache: bool = False) -> None:
        self.logger = logger
        self.market_date = date.fromisoformat(market_date)
        self.start_datetime = datetime.combine(self.market_date - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        self.end_datetime = datetime.combine(self.market_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        self.gdelt = GdeltClient(logger, self.start_datetime, self.end_datetime, request_get=gdelt_request_get)
        self.google_news = GoogleNewsClient(logger, self.start_datetime, self.end_datetime, request_get=google_request_get)
        self.cache_hits = 0
        self.cache_writes = 0
        self.refresh_cache = refresh_cache

    def queries_for(self, symbol: str, company_name: str) -> list[str]:
        base = company_name.strip()
        queries = [f'"{base}" OR "{symbol}"', f'"{base}" (earnings OR guidance OR analyst OR acquisition OR contract)', f'"{base}" (regulator OR lawsuit OR shares OR stock)']
        return queries[:DISCOVERY_MAX_QUERIES_PER_SYMBOL]

    def search(self, symbol: str, company_name: str) -> tuple[list[DiscoveryCandidate], dict[str, str], list[str]]:
        all_candidates: list[DiscoveryCandidate] = []
        coverage: dict[str, str] = {}
        queries = self.queries_for(symbol, company_name)
        for provider_name, client in (("gdelt", self.gdelt), ("google_news", self.google_news)):
            if provider_name == "gdelt" and getattr(client, "status", "READY") in {"CIRCUIT_OPEN", "RATE_LIMITED"}:
                coverage[provider_name] = "circuit_open"
                continue
            if provider_name == "google_news" and len(getattr(client, "access_failures", [])) >= 2:
                coverage[provider_name] = "circuit_open"
                continue
            provider_candidates: list[DiscoveryCandidate] = []
            provider_failed = False
            for query in queries:
                cached = self._load_cache(symbol, provider_name, query)
                if cached is not None:
                    self.cache_hits += 1
                    result = cached
                else:
                    if provider_candidates:
                        time.sleep(DISCOVERY_REQUEST_DELAY_SECONDS)
                    result = client.search(query, DISCOVERY_MAX_RESULTS_PER_QUERY)
                    self._write_cache(symbol, provider_name, query, result)
                if capture_active():
                    observe(provider_name, [{"title": row.get("title", ""), "source": row.get("publisher_hint") or row.get("source_domain", ""),
                        "url": row.get("url", ""), "published_at": row.get("published_at") if provider_name != "gdelt" else None,
                        "provider_limitations": ["METADATA_ONLY_UNRESOLVED_URL_OR_SEENDATE_NOT_PUBLICATION"]}
                        for row in result.get("candidates", [])], scope="COMPANY_FALLBACK", symbols=[symbol],
                        state="UNAVAILABLE" if result.get("status") == "failed" else "AVAILABLE")
                if result.get("status") == "failed":
                    provider_failed = True
                    if provider_name == "gdelt":
                        client.status = "RATE_LIMITED"
                    if provider_name not in client.access_failures:
                        client.access_failures.append(provider_name)
                    # A provider-level outage or rate limit should not trigger
                    # more requests for the remaining query variants.
                    break
                provider_candidates.extend(DiscoveryCandidate(**item) for item in result.get("candidates", []))
            if provider_failed and not provider_candidates:
                coverage[provider_name] = "failed"
            elif provider_candidates:
                coverage[provider_name] = "searched"
            else:
                coverage[provider_name] = "no_results"
            all_candidates.extend(item for item in provider_candidates if _is_relevant(item, symbol, company_name))
        return deduplicate_candidates(all_candidates), coverage, queries

    def _cache_path(self, symbol: str, provider: str, query: str) -> Path:
        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
        return CACHE_DIR / self.market_date.isoformat() / symbol / f"{provider}_{digest}.json"

    def _load_cache(self, symbol: str, provider: str, query: str) -> dict[str, Any] | None:
        path = self._cache_path(symbol, provider, query)
        if self.refresh_cache or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if payload.get("market_date") == self.market_date.isoformat() else None
        except (OSError, ValueError):
            return None

    def _write_cache(self, symbol: str, provider: str, query: str, result: dict[str, Any]) -> None:
        path = self._cache_path(symbol, provider, query)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"market_date": self.market_date.isoformat(), "query": query, **result}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.cache_writes += 1


def deduplicate_candidates(candidates: list[DiscoveryCandidate]) -> list[DiscoveryCandidate]:
    seen: set[tuple[str, str, str, str]] = set()
    output: list[DiscoveryCandidate] = []
    for candidate in candidates:
        parsed = urlparse(candidate.url)
        canonical_url = f"{parsed.netloc.lower()}{parsed.path.rstrip('/')}" if parsed.netloc else candidate.url.lower()
        title = " ".join(candidate.title.lower().split())
        key = (canonical_url, title, candidate.source_domain.lower(), (candidate.published_at or "")[:16])
        if key not in seen:
            seen.add(key)
            output.append(candidate)
    return output


def _is_relevant(candidate: DiscoveryCandidate, symbol: str, company_name: str) -> bool:
    """Reject obvious ticker/company mismatches before evidence ranking."""
    text = candidate.title.lower()
    ticker = symbol.lower()
    if ticker and ticker in text.split():
        return True
    significant = [token.lower().strip(".,") for token in company_name.split() if len(token) >= 5]
    return bool(significant and any(token in text for token in significant))
