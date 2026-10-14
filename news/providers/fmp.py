"""Financial Modeling Prep company-news and press-release adapter."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from news.schema import NewsCandidate

BASE_URL = "https://financialmodelingprep.com/api/v3"
NEWS_URL = f"{BASE_URL}/stock_news"
PRESS_RELEASE_URL = f"{BASE_URL}/press-releases"
PRESS_EVENT_TYPES = {"EARNINGS", "GUIDANCE", "M&A", "M_AND_A", "CONTRACT", "PARTNERSHIP", "PRODUCT", "TECH_PRODUCT", "EXECUTIVE_CHANGE", "RESTRUCTURING", "FINANCING", "OPERATIONAL_UPDATE"}


class FMPProvider:
    """Bounded FMP discovery/evidence client; never marks articles verified."""

    name = "FMP"

    def __init__(self, token: str | None = None, request_get: Callable[..., Any] | None = None, timeout: tuple[int, int] = (5, 12)) -> None:
        self.token = (token if token is not None else os.getenv("FMP_API_KEY", "")).strip()
        self.request_get = request_get or requests.get
        self.timeout = timeout
        self.request_count = 0
        self.news_requests = 0
        self.press_release_requests = 0
        self.news_candidates = 0
        self.press_release_candidates = 0
        self.evidence_matches = 0
        self.no_match = 0
        self.errors = 0
        self.fallback_activated = False
        self.available_for_run = True
        self.plan_restriction = False
        self.requests_skipped_plan_restriction = 0
        self.last_http_status: int | None = None
        self.last_news_raw_count = 0
        self.last_press_release_raw_count = 0
        self.status = "DISABLED_NOT_CONFIGURED" if not self.token else "READY"

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def search_company(self, symbol: str, start_date: str, end_date: str) -> list[NewsCandidate]:
        payload = self._request(NEWS_URL, {"tickers": symbol.upper(), "from": start_date, "to": end_date, "limit": 50}, "news")
        self.last_news_raw_count = len(payload)
        result = [self._normalize(item, symbol, "NEWS") for item in payload if isinstance(item, dict) and (item.get("title") or item.get("headline")) and item.get("url")]
        self.news_candidates += len(result)
        return result

    def search_press_releases(self, symbol: str, start_date: str, end_date: str) -> list[NewsCandidate]:
        payload = self._request(f"{PRESS_RELEASE_URL}/{symbol.upper()}", {"from": start_date, "to": end_date, "limit": 50}, "press_release")
        self.last_press_release_raw_count = len(payload)
        result = [self._normalize(item, symbol, "PRESS_RELEASE") for item in payload if isinstance(item, dict) and (item.get("title") or item.get("headline")) and item.get("url")]
        self.press_release_candidates += len(result)
        return result

    def search_company_news(self, symbol: str, start_date: str, end_date: str) -> list[NewsCandidate]:
        return self.search_company(symbol, start_date, end_date)

    def search_general(self, query: str, start_date: str, end_date: str) -> list[NewsCandidate]:
        return []

    def search_topic(self, topic: str, start_date: str, end_date: str) -> list[NewsCandidate]:
        return []

    def _request(self, url: str, params: dict[str, Any], request_type: str) -> list[dict[str, Any]]:
        if not self.enabled:
            self.status = "DISABLED_NOT_CONFIGURED"
            return []
        if not self.available_for_run:
            self.requests_skipped_plan_restriction += 1
            return []
        self.request_count += 1
        if request_type == "news":
            self.news_requests += 1
        else:
            self.press_release_requests += 1
        params = {**params, "apikey": self.token}
        try:
            response = self.request_get(url, params=params, timeout=self.timeout, headers={"User-Agent": "market_bot/1.0"})
            self.last_http_status = getattr(response, "status_code", None)
            if self.last_http_status == 402:
                self._mark_plan_restriction()
                self.errors += 1
                return []
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                if isinstance(payload, dict) and self._is_plan_restriction(payload):
                    self._mark_plan_restriction()
                raise ValueError("invalid_provider_payload")
            self.status = "OK"
            return payload
        except (requests.RequestException, OSError, ValueError, TypeError):
            self.errors += 1
            if not self.plan_restriction:
                self.status = "REQUEST_FAILED"
            return []

    def _mark_plan_restriction(self) -> None:
        """Disable all new FMP endpoints for this run after a confirmed 402."""
        self.plan_restriction = True
        self.available_for_run = False
        self.status = "FMP_PLAN_RESTRICTION"

    @staticmethod
    def _is_plan_restriction(payload: dict[str, Any]) -> bool:
        text = " ".join(str(payload.get(key) or "") for key in ("error", "message", "Error Message", "error_code")).lower()
        return "restricted endpoint" in text or "plan" in text and "restrict" in text or "402" in text

    @staticmethod
    def _normalize(item: dict[str, Any], symbol: str, content_type: str) -> NewsCandidate:
        title = str(item.get("title") or item.get("headline") or "").strip()
        summary = item.get("text") or item.get("summary") or item.get("content") or item.get("description")
        url = str(item.get("url") or item.get("link") or "")
        published = item.get("publishedDate") or item.get("published_at") or item.get("date")
        if isinstance(published, (int, float)):
            published = datetime.fromtimestamp(float(published), tz=timezone.utc).isoformat()
        elif published:
            published = str(published).replace("Z", "+00:00")
        source = str(item.get("site") or item.get("source") or "FMP")
        domain = urlparse(url).netloc.lower().removeprefix("www.")
        candidate = NewsCandidate(title, str(summary) if summary else None, published, source, domain, 3, url, "OTHER", [symbol.upper()], "fmp", False, "NOT_CONFIRMED", content_type, False, "NEWS_EVENT", 0.0, [], str(item.get("id") or item.get("symbol") or url), dict(item))
        candidate.raw_metadata.update({"provider": "FMP", "content_type": content_type})
        return candidate


def event_match_score(candidate: NewsCandidate, event_text: str, symbol: str) -> int:
    """Small deterministic local ranking helper for debug/evidence matching."""
    haystack = f"{candidate.title} {candidate.summary or ''}".lower()
    terms = [term for term in re.findall(r"[a-z0-9]+", event_text.lower()) if len(term) > 2]
    score = sum(10 for term in terms if term in haystack)
    score += 10 if symbol.upper() in {str(value).upper() for value in candidate.affected_symbols} else 0
    score += 5 if candidate.content_type == "PRESS_RELEASE" else 0
    return score
