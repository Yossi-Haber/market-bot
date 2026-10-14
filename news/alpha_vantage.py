"""Alpha Vantage NEWS_SENTIMENT discovery client."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from config import ALPHA_VANTAGE_MAX_DAILY_REQUESTS, ENABLE_ALPHA_VANTAGE
from cache_config import cache_path
from news.discovery import DiscoveryCandidate

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = cache_path("alpha_vantage")
API_URL = "https://www.alphavantage.co/query"


class AlphaVantageClient:
    """Use at most one cached/API request per selected mover."""

    def __init__(self, logger: logging.Logger, market_date: str) -> None:
        self.logger = logger
        self.market_date = date.fromisoformat(market_date)
        self.api_key = os.getenv("ALPHA_VANTAGE_API_KEY", "").strip()
        self.request_count = 0
        self.candidate_count = 0
        self.cache_hits = 0
        self.cache_writes = 0
        self.access_failures: list[str] = []
        self.rejected_count = 0
        self.enabled = ENABLE_ALPHA_VANTAGE
        self.status = "disabled" if not self.enabled else "skipped_missing_key" if not self.api_key else "ready"

    def search(self, symbol: str) -> tuple[list[DiscoveryCandidate], list[dict[str, str]]]:
        if not self.enabled or not self.api_key:
            return [], []
        path = self._cache_path(symbol)
        payload = self._load(path)
        if payload is not None:
            self.cache_hits += 1
        else:
            if self.request_count >= ALPHA_VANTAGE_MAX_DAILY_REQUESTS:
                self.status = "budget_exhausted"
                return [], [{"title": symbol, "reason": "ALPHA_VANTAGE_DAILY_BUDGET_EXHAUSTED"}]
            payload = self._request(symbol)
            self._write(path, payload)
        if payload.get("status") == "failed":
            self.status = "failed"
            return [], payload.get("rejected", [])
        raw_feed = payload.get("feed", [])
        candidates: list[DiscoveryCandidate] = []
        rejected: list[dict[str, str]] = list(payload.get("rejected", []))
        start, end = self._window()
        for item in raw_feed:
            ticker_match = any(str(t.get("ticker", "")).upper() == symbol.upper() for t in item.get("ticker_sentiment", []) if isinstance(t, dict))
            published = _parse_published(item.get("time_published"))
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            if not ticker_match:
                rejected.append({"title": title or symbol, "reason": "TARGET_TICKER_NOT_IN_METADATA"})
                continue
            if not published or not (start <= published <= end):
                rejected.append({"title": title or symbol, "reason": "OUTSIDE_EVENT_WINDOW"})
                continue
            if not title or not url:
                rejected.append({"title": symbol, "reason": "MISSING_ARTICLE_METADATA"})
                continue
            domain = urlparse(url).netloc.lower()
            candidates.append(DiscoveryCandidate(title=title, url=url, source_domain=domain, published_at=published.isoformat(), language=item.get("language"), source_country=None, discovery_source="Alpha Vantage", url_resolution="resolved", retrieval_status="METADATA_ONLY", snippet=None, publisher_hint=str(item.get("source") or "").strip() or None))
        self.candidate_count += len(candidates)
        self.rejected_count += len(rejected)
        self.status = "searched" if candidates else "no_results"
        return candidates, rejected

    def _request(self, symbol: str) -> dict[str, Any]:
        start, end = self._window()
        params = {"function": "NEWS_SENTIMENT", "tickers": symbol, "time_from": start.strftime("%Y%m%dT%H%M"), "time_to": end.strftime("%Y%m%dT%H%M"), "limit": 50, "apikey": self.api_key}
        self.request_count += 1
        try:
            response = requests.get(API_URL, params=params, timeout=(5, 15), headers={"User-Agent": "market_bot/1.0"})
            response.raise_for_status()
            payload = response.json()
            if any(key in payload for key in ("Note", "Information", "Error Message")):
                self.access_failures.append("Alpha Vantage API response")
                return {"status": "failed", "rejected": [{"title": symbol, "reason": "ALPHA_VANTAGE_API_ERROR"}]}
            return payload
        except requests.Timeout:
            self.access_failures.append("ALPHA_VANTAGE_TIMEOUT")
            self.logger.warning("Alpha Vantage discovery failed for %s: ALPHA_VANTAGE_TIMEOUT", symbol)
            return {"status": "failed", "rejected": [{"title": symbol, "reason": "ALPHA_VANTAGE_TIMEOUT"}]}
        except (requests.RequestException, ValueError, TypeError) as exc:
            self.access_failures.append("Alpha Vantage")
            self.logger.warning("Alpha Vantage discovery failed for %s: %s", symbol, exc)
            return {"status": "failed", "rejected": [{"title": symbol, "reason": "ALPHA_VANTAGE_REQUEST_FAILED"}]}

    def _window(self) -> tuple[datetime, datetime]:
        start = datetime.combine(self.market_date - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        end = datetime.combine(self.market_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        return start, end

    def _cache_path(self, symbol: str) -> Path:
        return CACHE_DIR / self.market_date.isoformat() / f"{symbol}.json"

    @staticmethod
    def _load(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if payload.get("schema_version") == 1 else None
        except (OSError, ValueError):
            return None

    def _write(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, **payload}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.cache_writes += 1


def _parse_published(value: Any) -> datetime | None:
    text = str(value or "")
    if len(text) < 15:
        return None
    try:
        return datetime.strptime(text[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
