"""Google News RSS metadata discovery client."""

from __future__ import annotations

import email.utils
import logging
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote_plus, urlparse
from xml.etree import ElementTree

import requests

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"


class GoogleNewsClient:
    """Parse RSS metadata without treating Google News as the factual source."""

    def __init__(self, logger: logging.Logger, start: datetime, end: datetime, request_get: Callable[..., Any] | None = None) -> None:
        self.logger = logger
        self.start = start
        self.end = end
        self.request_count = 0
        self.access_failures: list[str] = []
        self.request_get = request_get or requests.get

    def search(self, query: str, max_records: int) -> dict[str, Any]:
        self.request_count += 1
        try:
            response = self.request_get(GOOGLE_NEWS_RSS, params={"q": f"{query} after:{self.start.date().isoformat()} before:{self.end.date().isoformat()}", "hl": "en-US", "gl": "US", "ceid": "US:en"}, timeout=(5, 15), headers={"User-Agent": "market_bot/1.0"})
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
        except requests.Timeout:
            self.access_failures.append("GOOGLE_NEWS_TIMEOUT")
            self.logger.warning("Google News discovery failed: GOOGLE_NEWS_TIMEOUT")
            return {"status": "failed", "candidates": [], "error_code": "GOOGLE_NEWS_TIMEOUT"}
        except (requests.RequestException, ElementTree.ParseError) as exc:
            self.access_failures.append("Google News")
            self.logger.warning("Google News discovery failed: %s", exc)
            return {"status": "failed", "candidates": []}
        candidates: list[dict[str, Any]] = []
        for item in root.findall("./channel/item")[:max_records]:
            title = _text(item.find("title"))
            url = _text(item.find("link"))
            published = _parse_date(_text(item.find("pubDate")))
            source_node = item.find("source")
            source_name = (source_node.text or "").strip() if source_node is not None else "Google News"
            source_url = source_node.attrib.get("url", "") if source_node is not None else ""
            domain = urlparse(source_url or url).netloc.lower()
            if not title or not url or not published or not (self.start <= published <= self.end):
                continue
            candidates.append({"title": title, "url": url, "source_domain": domain, "published_at": published.isoformat(), "language": "en", "source_country": "US", "discovery_source": f"Google News ({source_name})", "url_resolution": "unresolved", "retrieval_status": "METADATA_ONLY", "snippet": None})
        return {"status": "searched", "candidates": candidates}


def _text(node: Any) -> str:
    return (node.text or "").strip() if node is not None else ""


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value).astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None
