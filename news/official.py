"""Official Tier-1 news collectors and pure parsers for SEC, Fed, BLS, and BEA."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from news.schema import NewsCandidate
from news.evidence_capture import observe, active as capture_active, publication_iso
from cache_config import cache_path
from news.sources import SecSource

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = cache_path("official_news")
OFFICIAL_FEEDS = {
    "fed": ("Federal Reserve", "federalreserve.gov", "https://www.federalreserve.gov/feeds/press_all.xml"),
    "bls": ("BLS", "bls.gov", "https://www.bls.gov/feed/bls_latest.rss"),
    "bea": ("BEA", "bea.gov", "https://www.bea.gov/news/blog/feed"),
}


def _content_class(source_name: str, title: str, link: str) -> tuple[str, bool]:
    text = f"{title} {link}".lower()
    if source_name == "BLS":
        releases = (("cpi", "CPI"), ("empsit", "EMPLOYMENT_SITUATION"), ("ppi", "PPI"), ("jolts", "JOLTS"), ("eci", "ECI"), ("imp", "IMPORT_EXPORT_PRICES"), ("prod", "PRODUCTIVITY"))
        return ("RELEASE", True) if any(token in text for token, _ in releases) else ("INDEX_PAGE", False)
    if source_name == "BEA":
        if "/news/blog/" in link.lower():
            return "BLOG", False
        release_terms = ("gdp", "personal income", "pce", "personal consumption", "international trade", "corporate profits", "state gdp", "state personal income")
        return ("RELEASE", True) if any(term in text for term in release_terms) else ("OTHER", False)
    if source_name == "Federal Reserve":
        return ("RELEASE", True) if any(term in text for term in ("fomc", "monetary policy", "interest rate", "press release")) else ("INDEX_PAGE", False)
    return "OTHER", False


def _rss_candidates(xml_text: str, source_name: str, domain: str, url: str, category: str, market_date: str) -> list[NewsCandidate]:
    """Parse RSS/Atom metadata without assuming article-body facts."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    results: list[NewsCandidate] = []
    for node in root.findall(".//item") + root.findall(".//{http://www.w3.org/2005/Atom}entry"):
        def text(name: str) -> str:
            value = node.findtext(name) or node.findtext(f"{{http://www.w3.org/2005/Atom}}{name}") or ""
            return " ".join(value.split())
        title, published, link = text("title"), text("pubDate") or text("published") or text("updated"), text("link") or url
        if capture_active():
            original_date = publication_iso(published)
            atom_link = node.find("{http://www.w3.org/2005/Atom}link")
            original_link = atom_link.get("href", link) if atom_link is not None else link
            observe("official_rss", [{"title": title, "source": source_name, "url": original_link,
                "published_at": original_date, "publication_timestamp_original": published,
                "text": node.findtext("description") or node.findtext("{http://www.w3.org/2005/Atom}summary") or "",
                "provider_limitations": ["RSS_EXCERPT_NOT_FULL_ARTICLE"]}], scope="MACRO_OFFICIAL")
        if not title:
            continue
        parsed = _parse_date(published)
        content_type, eligible_type = _content_class(source_name, title, link)
        eligible = eligible_type and bool(parsed) and parsed.date() >= date.fromisoformat(market_date) - timedelta(days=1)
        results.append(NewsCandidate(title, text("description") or None, published or None, source_name, domain, 1, link, category, [], "official", True, "VERIFIED", content_type, eligible))
    return results


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S GMT"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc) if fmt.endswith("GMT") else datetime.strptime(value, fmt).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def map_macro_category(title: str, source: str) -> str:
    text = title.lower()
    if source == "fed" or any(term in text for term in ("fomc", "monetary policy", "interest rate")):
        return "CENTRAL_BANK"
    if any(term in text for term in ("cpi", "employment", "payroll", "jobs", "jolts", "ppi")):
        return "MACRO_RELEASE"
    if any(term in text for term in ("gdp", "personal income", "pce", "trade")):
        return "MACRO_RELEASE"
    return "OTHER"


def parse_sec_filings(payload: dict[str, Any], symbol: str, market_date: str) -> list[NewsCandidate]:
    """Map only potentially material 8-K/6-K SEC filings into verified candidates."""
    recent = payload.get("filings", {}).get("recent", {}) if isinstance(payload, dict) else {}
    forms, dates = recent.get("form", []), recent.get("filingDate", [])
    docs, accessions, items = recent.get("primaryDocument", []), recent.get("accessionNumber", []), recent.get("items", [])
    results: list[NewsCandidate] = []
    for index, form in enumerate(forms):
        if form not in {"8-K", "6-K"}:
            continue
        try:
            item_text = " ".join(str(value) for value in (items[index] if index < len(items) and isinstance(items[index], list) else [items[index]] if index < len(items) else [])).lower()
            relevant_items = ("1.01", "2.01", "2.02", "2.05", "3.02", "5.02", "5.03", "8.01", "acquisition", "merger", "earnings", "guidance", "executive", "financing", "bankruptcy")
            if items and not any(term in item_text for term in relevant_items):
                continue
            filing_date = date.fromisoformat(str(dates[index]))
            if filing_date < date.fromisoformat(market_date) - timedelta(days=1):
                continue
            doc = str(docs[index])
            accession = str(accessions[index]).replace("-", "")
        except (IndexError, TypeError, ValueError):
            continue
        cik = str(payload.get("cik", "")).lstrip("0")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}" if cik else "https://www.sec.gov/edgar.shtml"
        title = f"SEC {form} filing for {symbol}"
        category = "EARNINGS" if "2.02" in item_text or "earnings" in item_text else "M&A" if any(term in item_text for term in ("acquisition", "merger")) else "REGULATORY" if "bankruptcy" in item_text else "COMPANY"
        results.append(NewsCandidate(title, f"{form} filing dated {filing_date.isoformat()}", filing_date.isoformat(), "SEC EDGAR", "sec.gov", 1, url, category, [symbol], "official", True, "VERIFIED", "FILING", True))
    return results


class OfficialNewsCollector:
    """Fetch and cache official feed metadata with isolated failure handling."""

    def __init__(self, logger: logging.Logger, market_date: str, request_get: Callable[..., requests.Response] | None = None, refresh_cache: bool = False) -> None:
        self.logger, self.market_date = logger, market_date
        self.request_get = request_get or requests.get
        self.request_count = 0
        self.cache_hits = 0
        self.refresh_cache = refresh_cache
        self.sec = SecSource(logger, market_date)

    def collect(self, symbols: list[str] | None = None) -> list[NewsCandidate]:
        candidates: list[NewsCandidate] = []
        for symbol in symbols or []:
            result = self.sec.search(symbol)
            for item in result.evidence:
                category = "EARNINGS" if item.catalyst_type == "SEC_FILING" else "COMPANY"
                candidates.append(NewsCandidate(item.title, item.event_relevance, item.published_at, item.source_name, "sec.gov", 1, item.url, category, [symbol], "official", True, "VERIFIED"))
        self.request_count += self.sec.request_count
        for key, (name, domain, url) in OFFICIAL_FEEDS.items():
            payload = self._load_or_fetch(key, url)
            if payload is None:
                observe("official_rss", [], scope=key, state="UNAVAILABLE")
                continue
            category = "CENTRAL_BANK" if key == "fed" else "MACRO_RELEASE"
            candidates.extend(_rss_candidates(payload, name, domain, url, category, self.market_date))
        return candidates

    def _load_or_fetch(self, source: str, url: str) -> str | None:
        path = CACHE_DIR / self.market_date / f"{source}.xml"
        if path.exists() and not self.refresh_cache:
            try:
                self.cache_hits += 1
                return path.read_text(encoding="utf-8")
            except OSError:
                pass
        try:
            self.request_count += 1
            response = self.request_get(url, timeout=(5, 15), headers={"User-Agent": "market_bot/1.0"})
            response.raise_for_status()
            text = response.text[:2_000_000]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return text
        except requests.Timeout:
            self.logger.warning("Official news source failed: %s error_code=OFFICIAL_IR_TIMEOUT", source)
            return None
        except (requests.RequestException, OSError, UnicodeError) as exc:
            self.logger.warning("Official news source failed: %s", source)
            return None
