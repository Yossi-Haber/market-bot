"""Free-access, metadata-only evidence sources for catalyst collection."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
NY = ZoneInfo("America/New_York")
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
YAHOO_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
DATE_RE = re.compile(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}")


@dataclass
class Evidence:
    source_name: str
    source_tier: int
    title: str
    published_at: str | None
    url: str
    retrieved_at: str
    event_relevance: str
    supports_catalyst: bool
    catalyst_type: str = "OTHER"
    event_datetime: str | None = None
    event_window_match: bool = False
    evidence_score: int = 0
    explicit_market_link: bool = False
    source_group: str = "other"
    retrieval_status: str = "FULL_METADATA"
    url_resolution: str = "resolved"
    is_discovery: bool = False
    is_direct: bool = False
    explicit_market_link_text: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceResult:
    evidence: list[Evidence]
    status: str


class SourceClient:
    """Shared request accounting and market-date window logic."""

    def __init__(self, logger: logging.Logger, market_date: str) -> None:
        self.logger = logger
        self.market_date = date.fromisoformat(market_date)
        self.window_start = self.market_date - timedelta(days=1)
        self.request_count = 0
        self.access_failures: list[str] = []

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        self.request_count += 1
        return requests.get(url, timeout=(5, 15), **kwargs)

    def _in_window(self, event_date: date) -> bool:
        return self.window_start <= event_date <= self.market_date

    @staticmethod
    def _retrieved_at() -> str:
        return datetime.now(timezone.utc).isoformat()


class SecSource(SourceClient):
    """SEC EDGAR submissions, without downloading filing bodies."""

    def __init__(self, logger: logging.Logger, market_date: str) -> None:
        super().__init__(logger, market_date)
        self.user_agent = os.getenv("SEC_USER_AGENT", "market_bot/1.0 contact-unconfigured")
        self.cik_by_symbol: dict[str, str] | None = None

    def search(self, symbol: str) -> SourceResult:
        try:
            cik = self._cik_for_symbol(symbol)
            if not cik:
                return SourceResult([], "not_applicable")
            response = self._get(SEC_SUBMISSIONS_URL.format(cik=cik), headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"})
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout:
            self.access_failures.append(f"SEC_TIMEOUT:{symbol}")
            self.logger.warning("SEC search failed for %s: SEC_TIMEOUT", symbol)
            return SourceResult([], "failed")
        except (requests.RequestException, ValueError, KeyError) as exc:
            self.access_failures.append(f"SEC:{symbol}")
            self.logger.warning("SEC search failed for %s: %s", symbol, str(exc).replace(self.user_agent, "[REDACTED]"))
            return SourceResult([], "failed")
        recent = payload.get("filings", {}).get("recent", {})
        results: list[Evidence] = []
        for index, filing_date_raw in enumerate(recent.get("filingDate", [])):
            try:
                filing_date = date.fromisoformat(filing_date_raw)
                form = str(recent.get("form", [""])[index])
                if not self._in_window(filing_date) or (form not in {"8-K", "10-Q", "10-K", "S-3"} and not form.startswith("424B")):
                    continue
                accession = str(recent.get("accessionNumber", [""])[index]).replace("-", "")
                document = str(recent.get("primaryDocument", [""])[index])
                url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{document}"
                accepted = recent.get("acceptanceDateTime", [None])[index]
                event_datetime = self._parse_sec_datetime(accepted, filing_date)
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            results.append(Evidence("SEC EDGAR", 1, f"SEC {form} filing for {symbol}", event_datetime, url, self._retrieved_at(), f"{form} filing dated {filing_date.isoformat()}", False, "SEC_FILING", event_datetime, True, source_group="sec", is_direct=True))
        return SourceResult(results, "searched")

    def _cik_for_symbol(self, symbol: str) -> str | None:
        if self.cik_by_symbol is None:
            try:
                response = self._get(SEC_TICKERS_URL, headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"})
                response.raise_for_status()
                payload = response.json()
                self.cik_by_symbol = {str(item["ticker"]).upper(): str(item["cik_str"]).zfill(10) for item in payload.values() if item.get("ticker") and item.get("cik_str")}
            except requests.Timeout:
                self.access_failures.append("SEC_TIMEOUT:company_tickers")
                self.logger.warning("SEC ticker map failed: SEC_TIMEOUT")
                self.cik_by_symbol = {}
            except (requests.RequestException, ValueError, KeyError) as exc:
                self.access_failures.append("SEC:company_tickers")
                self.logger.warning("SEC ticker map failed: %s", str(exc).replace(self.user_agent, "[REDACTED]"))
                self.cik_by_symbol = {}
        return self.cik_by_symbol.get(symbol.upper())

    @staticmethod
    def _parse_sec_datetime(value: Any, filing_date: date) -> str:
        if value:
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(NY).isoformat()
            except ValueError:
                pass
        return datetime.combine(filing_date, datetime.min.time(), tzinfo=NY).isoformat()


class CompanySource(SourceClient):
    """Configured investor-relations and press-release pages, metadata only."""

    def __init__(self, logger: logging.Logger, market_date: str) -> None:
        super().__init__(logger, market_date)
        self.mapping = json.loads((DATA_DIR / "company_sources.json").read_text(encoding="utf-8"))

    def search(self, symbol: str) -> tuple[SourceResult, SourceResult]:
        config = self.mapping.get(symbol, {})
        return (self._search_page(symbol, config.get("ir_url"), "Company Investor Relations", "company_ir"), self._search_page(symbol, config.get("press_release_url"), "Company Press Release", "company_press_release"))

    def _search_page(self, symbol: str, url: str | None, source_name: str, source_key: str) -> SourceResult:
        if not url:
            return SourceResult([], "not_found")
        try:
            response = self._get(url, headers={"User-Agent": "market_bot/1.0", "Accept": "text/html,application/xhtml+xml"})
            response.raise_for_status()
            html = response.text[:2_000_000]
        except requests.Timeout:
            self.access_failures.append(f"OFFICIAL_IR_TIMEOUT:{symbol}")
            self.logger.warning("%s search failed for %s: OFFICIAL_IR_TIMEOUT", source_name, symbol)
            return SourceResult([], "failed")
        except (requests.RequestException, UnicodeError) as exc:
            self.access_failures.append(f"{source_key}:{symbol}")
            self.logger.warning("%s search failed for %s: %s", source_name, symbol, exc)
            return SourceResult([], "failed")
        candidates: list[Evidence] = []
        for raw in re.findall(r"<title[^>]*>(.*?)</title>|<h[12][^>]*>(.*?)</h[12]>", html, flags=re.I | re.S):
            title = re.sub(r"<[^>]+>", " ", " ".join(raw))
            title = re.sub(r"\s+", " ", title).strip()
            event_date = self._date_near_title(html)
            if not title or not event_date or not self._in_window(event_date):
                continue
            candidates.append(Evidence(source_name, 1, title, event_date.isoformat(), url, self._retrieved_at(), f"{source_name} metadata for {symbol}", False, classify_title(title), datetime.combine(event_date, datetime.min.time(), tzinfo=NY).isoformat(), True, explicit_market_link=has_explicit_market_link(title), source_group="company", is_direct=True))
        return SourceResult(_dedupe_evidence(candidates), "searched" if candidates else "not_found")

    def _date_near_title(self, html: str) -> date | None:
        for value in DATE_RE.findall(html):
            try:
                return date.fromisoformat(value.replace("/", "-"))
            except ValueError:
                continue
        return None


class RegulatorSource(SourceClient):
    """Small conditional set of official regulator/newsroom pages."""

    def __init__(self, logger: logging.Logger, market_date: str) -> None:
        super().__init__(logger, market_date)
        self.mapping = json.loads((DATA_DIR / "regulator_sources.json").read_text(encoding="utf-8"))

    def search(self, symbol: str) -> SourceResult:
        urls = self.mapping.get(symbol)
        if not urls:
            return SourceResult([], "not_applicable")
        all_results: list[Evidence] = []
        failed = False
        for url in urls:
            try:
                response = self._get(url, headers={"User-Agent": "market_bot/1.0"})
                response.raise_for_status()
                html = response.text[:1_000_000]
            except requests.Timeout:
                failed = True
                self.access_failures.append(f"OFFICIAL_REGULATOR_TIMEOUT:{symbol}")
                self.logger.warning("Regulator search failed for %s: OFFICIAL_REGULATOR_TIMEOUT", symbol)
                continue
            except (requests.RequestException, UnicodeError) as exc:
                failed = True
                self.access_failures.append(f"regulator:{symbol}")
                self.logger.warning("Regulator search failed for %s: %s", symbol, exc)
                continue
            for raw in re.findall(r"<title[^>]*>(.*?)</title>|<h[12][^>]*>(.*?)</h[12]>", html, flags=re.I | re.S):
                title = re.sub(r"<[^>]+>", " ", " ".join(raw))
                title = re.sub(r"\s+", " ", title).strip()
                event_date = self._date_from_html(html)
                normalized_title = title.lower().strip(" |-")
                generic = ("newsroom homepage", "newsroom", "advice and guidance", "business guidance", "press releases", "home")
                if title and not any(item in normalized_title for item in generic) and event_date and self._in_window(event_date):
                    all_results.append(Evidence("Official Regulator", 1, title, event_date.isoformat(), url, self._retrieved_at(), "Targeted regulator metadata", False, classify_title(title), event_date.isoformat(), True, source_group="regulator", is_direct=True))
        return SourceResult(_dedupe_evidence(all_results), "failed" if failed else "searched")

    def _date_from_html(self, html: str) -> date | None:
        for value in DATE_RE.findall(html):
            try:
                parsed = date.fromisoformat(value.replace("/", "-"))
                if self._in_window(parsed):
                    return parsed
            except ValueError:
                pass
        return None


class YahooNewsSource(SourceClient):
    """Public news metadata search used as the major-news discovery layer."""

    def search(self, symbol: str, company_name: str) -> SourceResult:
        try:
            response = self._get(YAHOO_SEARCH_URL, params={"q": f"{symbol} {company_name}", "newsCount": 10, "quotesCount": 0}, headers={"User-Agent": "market_bot/1.0"})
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout:
            self.access_failures.append(f"MAJOR_NEWS_TIMEOUT:{symbol}")
            self.logger.warning("Major news search failed for %s: MAJOR_NEWS_TIMEOUT", symbol)
            return SourceResult([], "failed")
        except (requests.RequestException, ValueError) as exc:
            self.access_failures.append(f"major_news:{symbol}")
            self.logger.warning("Major news search failed for %s: %s", symbol, exc)
            return SourceResult([], "failed")
        results: list[Evidence] = []
        for item in payload.get("news", []):
            try:
                event_dt = datetime.fromtimestamp(int(item["providerPublishTime"]), timezone.utc).astimezone(NY)
                title, url = str(item.get("title") or "").strip(), str(item.get("link") or "").strip()
                if not title or not url or not self._in_window(event_dt.date()):
                    continue
            except (KeyError, TypeError, ValueError, OSError):
                continue
            publisher = str(item.get("publisher") or "news metadata")
            results.append(Evidence(f"{publisher} (via Yahoo Finance)", 3, title, event_dt.isoformat(), url, self._retrieved_at(), "Public major-news metadata result in the market-date window", any(term in title.lower() for term in ("after", "shares", "stock", "earnings", "guidance", "upgrade", "downgrade", "deal", "approval")), classify_title(title), event_dt.isoformat(), True, explicit_market_link=has_explicit_market_link(title), source_group="major_news"))
        return SourceResult(_dedupe_evidence(results), "searched")


def has_explicit_market_link(title: str) -> bool:
    """Return true only for explicit causal language in a headline."""
    text = title.lower()
    return any(phrase in text for phrase in ("shares rose after", "shares fell after", "stock gained following", "stock dropped as", "shares gain after", "shares drop after"))


def _dedupe_evidence(items: list[Evidence]) -> list[Evidence]:
    seen: set[tuple[str, str, str]] = set()
    output: list[Evidence] = []
    for item in items:
        key = (re.sub(r"[^a-z0-9]+", " ", item.title.lower()).strip(), (item.event_datetime or "")[:10], item.catalyst_type)
        if key not in seen:
            seen.add(key)
            output.append(item)
    return output


def classify_title(title: str) -> str:
    """Deterministically classify a headline without semantic generation."""
    text = title.lower()
    terms = (("EARNINGS", ("earnings", "quarter", "revenue", "profit")), ("GUIDANCE", ("guidance", "outlook")), ("ANALYST_ACTION", ("upgrade", "downgrade", "price target", "analyst")), ("M_AND_A", ("acquire", "acquisition", "merger", "buyout", "deal")), ("FDA", ("fda", "drug approval")), ("LEGAL", ("lawsuit", "court", "legal")), ("REGULATORY", ("regulator", "regulatory", "doj", "ftc")), ("CONTRACT", ("contract", "award")), ("PARTNERSHIP", ("partnership", "partner")), ("MANAGEMENT_CHANGE", ("ceo", "executive", "resign")))
    for catalyst_type, keywords in terms:
        if any(keyword in text for keyword in keywords):
            return catalyst_type
    return "OTHER"
