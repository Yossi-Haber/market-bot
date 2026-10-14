"""Small, keyless targeted search layer for the selected daily movers."""

from __future__ import annotations

import email.utils
import hashlib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests

from config import TARGETED_SEARCH_MAX_QUERIES_PER_SYMBOL, TARGETED_SEARCH_MAX_REQUESTS_PER_RUN
from cache_config import cache_path
from news.discovery import DiscoveryCandidate

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = cache_path("targeted_search")
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"


class TargetedSearchClient:
    """Use focused Google News RSS queries without treating RSS as evidence."""

    def __init__(self, logger: logging.Logger, market_date: str) -> None:
        self.logger = logger
        self.market_date = date.fromisoformat(market_date)
        self.start = datetime.combine(self.market_date - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        self.end = datetime.combine(self.market_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        self.request_count = 0
        self.candidate_count = 0
        self.cache_hits = 0
        self.cache_writes = 0
        self.access_failures: list[str] = []

    def queries_for(self, company_name: str, symbol: str, direction: str) -> list[str]:
        date_text = self.market_date.strftime("%B %d %Y").replace(" 0", " ")
        move_term = "shares rose" if direction == "up" else "shares fell"
        return [
            f'"{company_name}" "{date_text}"',
            f'"{company_name}" "{move_term}"',
            f'"{company_name}" (earnings OR guidance OR acquisition OR contract OR regulatory OR analyst)',
            f'"{company_name}" (Reuters OR AP OR "company press release")',
        ][:TARGETED_SEARCH_MAX_QUERIES_PER_SYMBOL]

    def search(self, symbol: str, company_name: str, direction: str) -> tuple[list[DiscoveryCandidate], list[dict[str, str]], list[str], str]:
        queries = self.queries_for(company_name, symbol, direction)
        candidates: list[DiscoveryCandidate] = []
        rejected: list[dict[str, str]] = []
        status = "no_results"
        for index, query in enumerate(queries, start=1):
            if self.request_count >= TARGETED_SEARCH_MAX_REQUESTS_PER_RUN:
                break
            self.logger.info("Targeted search started: %s; Query %d/%d: %s", symbol, index, len(queries), query)
            cached = self._load(symbol, query)
            if cached is not None:
                self.cache_hits += 1
                payload = cached
            else:
                payload = self._request(query)
                self._write(symbol, query, payload)
            if payload.get("status") == "failed":
                status = "failed"
                rejected.extend(payload.get("rejected", []))
                continue
            status = "searched"
            for item in payload.get("candidates", []):
                candidate = DiscoveryCandidate(**item)
                if _relevant(candidate.title, symbol, company_name):
                    candidates.append(candidate)
                else:
                    rejected.append({"title": candidate.title, "reason": "IRRELEVANT_OR_TICKER_COLLISION"})
            rejected.extend(payload.get("rejected", []))
            self.logger.info("Targeted search completed: %s; candidates=%d", symbol, len(payload.get("candidates", [])))
            if index < len(queries):
                time.sleep(0.1)
        self.candidate_count += len(candidates)
        return candidates, rejected, queries, status

    def _request(self, query: str) -> dict[str, Any]:
        self.request_count += 1
        try:
            response = requests.get(GOOGLE_NEWS_RSS, params={"q": f"{query} after:{self.start.date().isoformat()} before:{self.end.date().isoformat()}", "hl": "en-US", "gl": "US", "ceid": "US:en"}, timeout=(5, 15), headers={"User-Agent": "market_bot/1.0"})
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
        except requests.Timeout:
            self.access_failures.append("TARGETED_SEARCH_TIMEOUT")
            self.logger.warning("Targeted search provider failed: TARGETED_SEARCH_TIMEOUT")
            return {"status": "failed", "candidates": [], "rejected": [{"title": query, "reason": "TARGETED_SEARCH_TIMEOUT"}]}
        except (requests.RequestException, ElementTree.ParseError) as exc:
            self.access_failures.append("targeted_search")
            self.logger.warning("Targeted search provider failed: %s", exc)
            return {"status": "failed", "candidates": [], "rejected": [{"title": query, "reason": "TARGETED_SEARCH_PROVIDER_FAILED"}]}
        candidates: list[dict[str, Any]] = []
        rejected: list[dict[str, str]] = []
        for item in root.findall("./channel/item"):
            title = _text(item.find("title"))
            url = _text(item.find("link"))
            published = _parse_date(_text(item.find("pubDate")))
            source_node = item.find("source")
            publisher = (source_node.text or "").strip() if source_node is not None else "Google News"
            source_url = source_node.attrib.get("url", "") if source_node is not None else ""
            if not title or not url or not published or not (self.start <= published <= self.end):
                rejected.append({"title": title or query, "reason": "OUTSIDE_EVENT_WINDOW"})
                continue
            candidates.append({"title": title, "url": url, "source_domain": urlparse(source_url or url).netloc.lower(), "published_at": published.isoformat(), "language": "en", "source_country": "US", "discovery_source": "targeted_search", "url_resolution": "unresolved", "retrieval_status": "METADATA_ONLY", "snippet": None, "publisher_hint": publisher})
        return {"status": "searched", "candidates": candidates, "rejected": rejected}

    def _path(self, symbol: str, query: str) -> Path:
        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
        return CACHE_DIR / self.market_date.isoformat() / symbol / f"google_news_{digest}.json"

    def _load(self, symbol: str, query: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(self._path(symbol, query).read_text(encoding="utf-8"))
            return payload if payload.get("schema_version") == 1 else None
        except (OSError, ValueError):
            return None

    def _write(self, symbol: str, query: str, payload: dict[str, Any]) -> None:
        path = self._path(symbol, query)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "query": query, **payload}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.cache_writes += 1


def _relevant(title: str, symbol: str, company_name: str) -> bool:
    text = title.lower()
    return symbol.lower() in text.split() or any(token.lower().strip(".,") in text for token in company_name.split() if len(token) >= 5)


def _text(node: Any) -> str:
    return (node.text or "").strip() if node is not None else ""


def _parse_date(value: str) -> datetime | None:
    try:
        return email.utils.parsedate_to_datetime(value).astimezone(timezone.utc) if value else None
    except (TypeError, ValueError, OverflowError):
        return None
