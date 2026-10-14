"""GDELT DOC 2.0 metadata discovery client."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable
from urllib.parse import quote_plus, urlparse

import requests

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


class GdeltClient:
    """Fetch ArticleList-style metadata only, bounded to the requested window."""

    def __init__(self, logger: logging.Logger, start: datetime, end: datetime, request_get: Callable[..., Any] | None = None) -> None:
        self.logger = logger
        self.start = start
        self.end = end
        self.request_count = 0
        self.access_failures: list[str] = []
        self.status = "READY"
        self.request_get = request_get or requests.get
        self.consecutive_failures = 0

    def search(self, query: str, max_records: int) -> dict[str, Any]:
        params = {"query": query, "mode": "artlist", "format": "json", "maxrecords": max_records, "sort": "datedesc", "startdatetime": self.start.strftime("%Y%m%d%H%M%S"), "enddatetime": self.end.strftime("%Y%m%d%H%M%S")}
        self.request_count += 1
        try:
            response = self.request_get(GDELT_URL, params=params, timeout=(5, 15), headers={"User-Agent": "market_bot/1.0"})
            response.raise_for_status()
            self.consecutive_failures = 0
            payload = response.json()
            articles = payload.get("articles", [])
            candidates = []
            for item in articles:
                url = str(item.get("url") or item.get("url_mobile") or "").strip()
                title = str(item.get("title") or "").strip()
                if not url or not title:
                    continue
                candidates.append({"title": title, "url": url, "source_domain": str(item.get("domain") or urlparse(url).netloc).lower(), "published_at": _gdelt_datetime(item.get("seendate")), "language": item.get("language"), "source_country": item.get("sourcecountry"), "discovery_source": "GDELT", "url_resolution": "resolved", "retrieval_status": "METADATA_ONLY", "snippet": None})
            return {"status": "searched", "candidates": candidates}
        except requests.Timeout:
            self.access_failures.append("GDELT_TIMEOUT")
            self.consecutive_failures += 1
            self.status = "CIRCUIT_OPEN" if self.consecutive_failures >= 2 else "FAILED"
            self.logger.warning("GDELT discovery failed: GDELT_TIMEOUT")
            return {"status": "failed", "candidates": [], "error_code": "GDELT_TIMEOUT"}
        except (requests.RequestException, ValueError, TypeError) as exc:
            self.access_failures.append("GDELT")
            self.consecutive_failures += 1
            if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code == 429:
                self.status = "RATE_LIMITED"
            else:
                self.status = "CIRCUIT_OPEN" if self.consecutive_failures >= 2 else "FAILED"
            self.logger.warning("GDELT discovery failed: %s", exc)
            return {"status": "failed", "candidates": []}


def _gdelt_datetime(value: Any) -> str | None:
    text = str(value or "")
    if len(text) >= 14 and text[:14].isdigit():
        try:
            return datetime.strptime(text[:14], "%Y%m%d%H%M%S").isoformat() + "+00:00"
        except ValueError:
            pass
    return None
