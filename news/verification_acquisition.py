"""Bounded live evidence acquisition for Verification V2.

Only metadata is retained.  Article bodies and raw HTML never leave this module.
"""

from __future__ import annotations
from news.evidence_capture import observe, active as capture_active, page_excerpt

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests

from news.verification_v2 import VerificationDiagnostics
from cache_config import cache_path
from news.providers.fmp import FMPProvider, PRESS_EVENT_TYPES

BASE_DIR = Path(__file__).resolve().parent.parent
COMPANY_SOURCES = BASE_DIR / "data" / "company_sources.json"
REGULATOR_SOURCES = BASE_DIR / "data" / "regulator_sources.json"
CACHE_DIR = cache_path("verification_v2")
OFFICIAL_DISCOVERY_CACHE_DIR = cache_path("verification_official_domains")
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
TIER2_DOMAINS = {"reuters.com", "apnews.com", "cnbc.com", "bloomberg.com", "wsj.com", "ft.com", "marketwatch.com"}
OFFICIAL_DOMAIN_HINTS = {"newsroom", "investor", "investors", "ir", "corporate", "company"}
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
REGULATOR_ACTOR_URLS = {
    "FAA": ("https://www.faa.gov/newsroom",),
    "FDA": ("https://www.fda.gov/news-events/press-announcements",),
    "FTC": ("https://www.ftc.gov/news-events/news/press-releases",),
    "DOJ": ("https://www.justice.gov/news",),
    "SEC": ("https://www.sec.gov/news/pressreleases",),
}
REGULATORY_ROUTE_MISSING = "REGULATORY_ROUTE_MISSING_REGULATOR"


def _event_type(candidate: dict[str, Any]) -> str:
    return str(candidate.get("event_type") or candidate.get("category") or "OTHER").upper().replace("_RESULTS", "").replace("_CHANGE", "")


def _actors(candidate: dict[str, Any]) -> list[str]:
    values = [str(value).upper() for value in candidate.get("actors", []) if value]
    text = " ".join(str(candidate.get(key) or "") for key in ("title", "summary", "canonical_event"))
    for actor in REGULATOR_ACTOR_URLS:
        if re.search(rf"\b{re.escape(actor)}\b", text, re.IGNORECASE) and actor not in values:
            values.append(actor)
    return values


def resolve_verification_route(candidate: dict[str, Any]) -> list[str]:
    """Resolve one deterministic route shared by live and cache-only replay."""
    event_type = _event_type(candidate)
    if event_type == "REGULATORY":
        return ["REGULATOR_OFFICIAL", "OFFICIAL_IR_DISCOVERY", "TIER2_SEARCH"]
    if event_type == "ANALYST_ACTION":
        return ["TIER2_SEARCH"]
    if event_type in {"EARNINGS", "GUIDANCE"}:
        return ["COMPANY_IR", "SEC_EDGAR", "TIER2_SEARCH"]
    if event_type in {"M_AND_A", "CONTRACT", "PARTNERSHIP", "PRODUCT", "TECH_PRODUCT"}:
        return ["COMPANY_IR", "SEC_EDGAR", "TIER2_SEARCH"]
    return ["COMPANY_IR", "SEC_EDGAR", "OFFICIAL_IR_DISCOVERY", "TIER2_SEARCH"]


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def _filing_metadata(html: str) -> tuple[list[str], str, list[str]]:
    """Extract exhibit labels and a bounded plain-text snippet from a filing."""
    labels = sorted(set(re.findall(r"EX[- ]?99\.\d+", html, re.I)))
    text = _clean_text(re.sub(r"<[^>]+>", " ", html))
    links = re.findall(r"href=[\"']([^\"']*(?:ex[-_ ]?99\.\d+|exhibit)[^\"']*)[\"']", html, re.I)
    return labels, text[:12_000], links[:3]


def _metadata_from_html(html: str, url: str, fallback_title: str, fallback_date: str | None) -> tuple[str, str | None]:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    heading_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
    title = _clean_text(re.sub(r"<[^>]+>", " ", (heading_match or title_match).group(1))) if (heading_match or title_match) else fallback_title
    date_match = re.search(r'(?:article:published_time|datePublished|pubdate)[^>"\']*[=:]\s*["\']([^"\']+)', html, re.I)
    published = _parse_date(date_match.group(1)) if date_match else _parse_date(fallback_date)
    return title or fallback_title, published.isoformat() if published else fallback_date


def _safe_cache_name(candidate: dict[str, Any], source: str) -> str:
    normalized = " ".join(str(candidate.get("title") or "").lower().split())
    return hashlib.sha256(f"{normalized}|{source}".encode("utf-8")).hexdigest()[:20] + ".json"


def _candidate_names(candidate: dict[str, Any]) -> list[str]:
    names = [str(item).strip() for item in candidate.get("entity_names", []) if str(item).strip()]
    text = " ".join(str(candidate.get(key) or "") for key in ("title", "summary"))
    names.extend(re.findall(r"\b[A-Z][A-Za-z0-9&.-]{2,}\b", text))
    return list(dict.fromkeys(name for name in names if name.lower() not in {"raises", "proposes", "acquisition", "merger", "company", "announces", "advances", "guidance"}))


def _attempt(diagnostics: VerificationDiagnostics, source_type: str, status: str = "CHECKED") -> None:
    diagnostics.attempted_sources.append({"type": source_type, "status": status})


def _regulator_error_code(status: int | None, exc: BaseException) -> str:
    if isinstance(exc, requests.Timeout):
        return "REGULATOR_TIMEOUT"
    if isinstance(exc, requests.exceptions.SSLError):
        return "REGULATOR_TLS_ERROR"
    if status == 403:
        return "REGULATOR_HTTP_403"
    if status == 404:
        return "REGULATOR_HTTP_404"
    if status == 429:
        return "REGULATOR_HTTP_429"
    if isinstance(exc, requests.HTTPError):
        return "REGULATOR_OTHER_HTTP_ERROR"
    return "REGULATOR_OTHER_HTTP_ERROR"


class LiveEvidenceAcquirer:
    """Acquire at most ``max_requests`` bounded metadata requests per run."""

    def __init__(
        self,
        logger: logging.Logger,
        market_date: str,
        request_get: Callable[..., requests.Response] | None = None,
        max_requests: int = 10,
        refresh_cache: bool = False,
        fmp_provider: FMPProvider | None = None,
        cache_only: bool = False,
    ) -> None:
        self.logger = logger
        self.market_date = market_date
        self.request_get = request_get or requests.get
        self.max_requests = max_requests
        self.request_count = 0
        self.cache_hits = 0
        self.refresh_cache = refresh_cache
        self.cache_only = cache_only
        self.fmp_provider = fmp_provider or FMPProvider(request_get=self.request_get)
        self._sec_cik_by_symbol: dict[str, str] | None = None
        try:
            self.company_sources = json.loads(COMPANY_SOURCES.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.company_sources = {}
        try:
            self.regulator_sources = json.loads(REGULATOR_SOURCES.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.regulator_sources = {}

    def acquire(self, candidates: list[dict[str, Any]]) -> dict[str, tuple[list[dict[str, Any]], VerificationDiagnostics]]:
        """Acquire evidence for no more than five candidates, preserving failures."""
        output: dict[str, tuple[list[dict[str, Any]], VerificationDiagnostics]] = {}
        for candidate in candidates[:5]:
            title = str(candidate.get("title") or "")
            evidence: list[dict[str, Any]] = []
            diagnostics = VerificationDiagnostics()
            diagnostics.event_type = _event_type(candidate)
            diagnostics.actors = _actors(candidate)
            diagnostics.verification_route = resolve_verification_route(candidate)
            for route_step in diagnostics.verification_route:
                if route_step == "REGULATOR_OFFICIAL":
                    evidence.extend(self._regulator_official(candidate, diagnostics))
                elif route_step == "COMPANY_IR":
                    evidence.extend(self._company_ir(candidate, diagnostics))
                    if not evidence and diagnostics.event_type in PRESS_EVENT_TYPES:
                        evidence.extend(self._fmp_press_release_for_candidate(candidate, diagnostics))
                elif route_step == "SEC_EDGAR":
                    evidence.extend(self._sec_for_candidate(candidate, diagnostics))
                elif route_step == "OFFICIAL_IR_DISCOVERY":
                    evidence.extend(self._official_discovery(candidate, diagnostics))
                elif route_step == "TIER2_SEARCH":
                    evidence.extend(self._tier2(candidate, diagnostics))
                if evidence:
                    break
            if diagnostics.event_type == "REGULATORY" and not any(item.get("type", "").startswith("REGULATOR_OFFICIAL") for item in diagnostics.attempted_sources):
                diagnostics.reason = REGULATORY_ROUTE_MISSING
            output[title] = (evidence, diagnostics)
        return output

    def _request(self, url: str, **kwargs: Any) -> requests.Response | None:
        if self.cache_only:
            return None
        if self.request_count >= self.max_requests:
            return None
        self.request_count += 1
        try:
            response = self.request_get(url, timeout=(5, 15), headers={"User-Agent": "market_bot/1.0", **kwargs.pop("headers", {})}, **kwargs)
            response.raise_for_status()
            return response
        except requests.Timeout:
            self.logger.warning("Verification source request failed: domain=%s error_code=TIER2_TIMEOUT", _domain(url))
            return None
        except (requests.RequestException, OSError, UnicodeError) as exc:
            self.logger.warning("Verification source request failed: domain=%s error=%s", _domain(url), type(exc).__name__)
            return None

    def _cache_path(self, candidate: dict[str, Any], source: str) -> Path:
        return CACHE_DIR / self.market_date / _safe_cache_name(candidate, source)

    def _load_cache(self, candidate: dict[str, Any], source: str) -> list[dict[str, Any]] | None:
        if self.refresh_cache:
            return None
        try:
            payload = json.loads(self._cache_path(candidate, source).read_text(encoding="utf-8"))
            if isinstance(payload, list):
                observe("verification", payload, scope="OFFICIAL_OR_TIER2_CACHE", limitations=["CACHED_METADATA_MAY_NOT_HAVE_ORIGINAL_BODIES"])
                self.cache_hits += 1
                return payload
            return None
        except (OSError, ValueError):
            return None

    def _save_cache(self, candidate: dict[str, Any], source: str, evidence: list[dict[str, Any]]) -> None:
        observe("verification", evidence, scope="OFFICIAL_OR_TIER2")
        path = self._cache_path(candidate, source)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _company_ir(self, candidate: dict[str, Any], diagnostics: VerificationDiagnostics) -> list[dict[str, Any]]:
        """Acquire configured company IR/press metadata only."""
        results: list[dict[str, Any]] = []
        for symbol in [str(value).upper() for value in candidate.get("affected_symbols", []) if value]:
            config = self.company_sources.get(symbol, {})
            for source_key in ("press_release_url", "ir_url"):
                url = config.get(source_key)
                if not url:
                    continue
                source = f"official_{symbol}_{source_key}"
                cached = self._load_cache(candidate, source)
                _attempt(diagnostics, f"OFFICIAL_IR:{_domain(url)}", "CACHE_HIT" if cached is not None else ("CACHE_MISS" if self.cache_only else "CHECKED"))
                if cached is not None:
                    results.extend(cached)
                    continue
                response = self._request(url)
                if response is None:
                    continue
                title, published = _metadata_from_html(response.text[:2_000_000], url, str(candidate.get("title") or ""), candidate.get("published_at"))
                item = {"source_type": "OFFICIAL", "source_domain": _domain(url), "source_name": "Company IR", "title": title, "published_at": published, "affected_symbols": [symbol], "source_url": url}
                if capture_active():
                    original_title, original_date, original_stamp, original_text = page_excerpt(response.text)
                    observe("official_page", [{**item, "title": original_title, "published_at": original_date, "publication_timestamp_original": original_stamp, "text": original_text,
                        "provider_limitations": ["HTML_TEXT_EXCERPT_PUBLICATION_MUST_BE_FROM_PAGE"]}], scope="COMPANY_IR")
                self._save_cache(candidate, source, [item])
                results.append(item)
                break
        return results

    def _fmp_press_release_for_candidate(self, candidate: dict[str, Any], diagnostics: VerificationDiagnostics) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for symbol in [str(value).upper() for value in candidate.get("affected_symbols", []) if value]:
            output.extend(self._fmp_press_release(symbol, candidate, diagnostics))
        return output

    def _sec_for_candidate(self, candidate: dict[str, Any], diagnostics: VerificationDiagnostics) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for symbol in [str(value).upper() for value in candidate.get("affected_symbols", []) if value]:
            output.extend(self._sec(symbol, candidate, diagnostics))
        return output

    def _regulator_official(self, candidate: dict[str, Any], diagnostics: VerificationDiagnostics) -> list[dict[str, Any]]:
        """Acquire regulator metadata using actor-aware, cacheable endpoints."""
        actors = diagnostics.actors or _actors(candidate)
        urls: list[tuple[str, str]] = []
        for actor in actors:
            urls.extend((actor, url) for url in REGULATOR_ACTOR_URLS.get(actor, ()))
        if not urls:
            for symbol in [str(value).upper() for value in candidate.get("affected_symbols", []) if value]:
                urls.extend(("SYMBOL", url) for url in self.regulator_sources.get(symbol, []))
        results: list[dict[str, Any]] = []
        for actor, url in urls:
            domain = _domain(url)
            diagnostics.regulator_domain = domain
            diagnostics.regulator_requested_url = url
            source = f"regulator_{actor}_{_domain(url)}"
            cached = self._load_cache(candidate, source)
            status = "CACHE_HIT" if cached is not None else ("CACHE_MISS" if self.cache_only else "CHECKED")
            _attempt(diagnostics, f"REGULATOR_OFFICIAL:{domain}", status)
            if cached is not None:
                results.extend(cached)
                continue
            response = self._regulator_request(url, diagnostics)
            if response is None:
                if not self.cache_only:
                    fallback = self._regulator_domain_fallback(candidate, actor, domain, diagnostics)
                    if fallback:
                        results.extend(fallback)
                continue
            title, published = _metadata_from_html(response.text[:2_000_000], url, str(candidate.get("title") or ""), candidate.get("published_at"))
            item = {"source_type": "OFFICIAL", "source_domain": _domain(url), "source_name": f"{actor} Official", "title": title, "published_at": published, "affected_symbols": candidate.get("affected_symbols", []), "source_url": url, "regulator": actor}
            if capture_active():
                original_title, original_date, original_stamp, original_text = page_excerpt(response.text)
                observe("official_page", [{**item, "title": original_title, "published_at": original_date, "publication_timestamp_original": original_stamp, "text": original_text,
                    "provider_limitations": ["HTML_TEXT_EXCERPT_PUBLICATION_MUST_BE_FROM_PAGE"]}], scope="REGULATOR")
            self._save_cache(candidate, source, [item])
            results.append(item)
        return results

    def _regulator_request(self, url: str, diagnostics: VerificationDiagnostics) -> requests.Response | None:
        """Perform one bounded regulator request with safe HTTP diagnostics."""
        if self.cache_only:
            return None
        if self.request_count >= self.max_requests:
            diagnostics.regulator_error_code = "REGULATOR_REQUEST_BUDGET_EXHAUSTED"
            return None
        self.request_count += 1
        headers = {
            "User-Agent": "market_bot/1.0 (market research; contact=admin@example.invalid)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        response: requests.Response | None = None
        try:
            response = self.request_get(url, timeout=(5, 15), headers=headers, allow_redirects=True)
            response.raise_for_status()
            diagnostics.regulator_http_status = getattr(response, "status_code", None)
            diagnostics.regulator_final_url = str(getattr(response, "url", None) or url)
            diagnostics.regulator_content_type = str(getattr(response, "headers", {}).get("Content-Type", ""))[:120]
            diagnostics.regulator_error_code = None
            return response
        except requests.RequestException as exc:
            status = getattr(response, "status_code", None)
            diagnostics.regulator_http_status = status
            diagnostics.regulator_final_url = str(getattr(response, "url", None) or url)
            diagnostics.regulator_content_type = str(getattr(response, "headers", {}).get("Content-Type", ""))[:120]
            diagnostics.regulator_error_code = _regulator_error_code(status, exc)
            self.logger.warning(
                "Regulator request failed: domain=%s requested_url=%s http_status=%s final_url=%s content_type=%s error_class=%s error_code=%s error_message=%s",
                diagnostics.regulator_domain or _domain(url), url, status, diagnostics.regulator_final_url,
                diagnostics.regulator_content_type or "unknown", type(exc).__name__, diagnostics.regulator_error_code,
                re.sub(r"\s+", " ", str(exc))[:240],
            )
            return None
        except (OSError, UnicodeError) as exc:
            diagnostics.regulator_error_code = "REGULATOR_OTHER_HTTP_ERROR"
            self.logger.warning(
                "Regulator request failed: domain=%s requested_url=%s http_status=%s final_url=%s content_type=%s error_class=%s error_code=%s error_message=%s",
                diagnostics.regulator_domain or _domain(url), url, diagnostics.regulator_http_status, diagnostics.regulator_final_url or url,
                diagnostics.regulator_content_type or "unknown", type(exc).__name__, diagnostics.regulator_error_code,
                re.sub(r"\s+", " ", str(exc))[:240],
            )
            return None

    def _regulator_domain_fallback(self, candidate: dict[str, Any], actor: str, domain: str, diagnostics: VerificationDiagnostics) -> list[dict[str, Any]]:
        """Search only official links on the failed regulator domain."""
        _attempt(diagnostics, f"REGULATOR_OFFICIAL_FALLBACK:{domain}", "CHECKED")
        diagnostics.regulator_fallback_used = True
        query = f'site:{domain} "{str(candidate.get("title") or "")}"'
        response = self._request(GOOGLE_NEWS_RSS, params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
        if response is None:
            return []
        try:
            root = ElementTree.fromstring(response.content)
        except (ElementTree.ParseError, AttributeError):
            diagnostics.regulator_error_code = "REGULATOR_PARSE_ERROR"
            return []
        for node in root.findall("./channel/item"):
            link = (node.findtext("link") or "").strip()
            link_domain = _domain(link)
            if not link or not (link_domain == domain or link_domain.endswith("." + domain)):
                continue
            title = _clean_text(node.findtext("title") or candidate.get("title") or "")
            published = _parse_date(node.findtext("pubDate"))
            if not title:
                continue
            source = f"regulator_{actor}_{domain}"
            evidence = [{"source_type": "OFFICIAL", "source_name": f"{actor} Official", "source_domain": link_domain, "title": title, "published_at": published.isoformat() if published else candidate.get("published_at"), "affected_symbols": candidate.get("affected_symbols", []), "source_url": link, "regulator": actor, "official_regulator_fallback": True}]
            self._save_cache(candidate, source, evidence)
            return evidence
        return []

    def _official(self, candidate: dict[str, Any], diagnostics: VerificationDiagnostics) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for symbol in [str(value).upper() for value in candidate.get("affected_symbols", []) if value]:
            before_symbol = len(results)
            config = self.company_sources.get(symbol, {})
            for source_key in ("press_release_url", "ir_url"):
                url = config.get(source_key)
                if not url:
                    continue
                _attempt(diagnostics, f"OFFICIAL_IR:{_domain(url)}")
                cached = self._load_cache(candidate, f"official_{symbol}_{source_key}")
                if cached is not None:
                    results.extend(cached)
                    continue
                response = self._request(url)
                if response is None:
                    continue
                title, published = _metadata_from_html(response.text[:2_000_000], url, str(candidate.get("title") or ""), candidate.get("published_at"))
                item = {"source_type": "OFFICIAL", "source_domain": _domain(url), "title": title, "published_at": published, "affected_symbols": [symbol], "source_url": url}
                self._save_cache(candidate, f"official_{symbol}_{source_key}", [item])
                results.append(item)
                # One configured official page per symbol is enough for this bounded pass.
                break
            event_type = str(candidate.get("category") or candidate.get("event_type") or "").upper()
            if len(results) == before_symbol and event_type in PRESS_EVENT_TYPES:
                results.extend(self._fmp_press_release(symbol, candidate, diagnostics))
            if len(results) == before_symbol:
                results.extend(self._sec(symbol, candidate, diagnostics))
            if len(results) == before_symbol and event_type == "REGULATORY":
                results.extend(self._fmp_press_release(symbol, candidate, diagnostics))
        return results

    def _fmp_press_release(self, symbol: str, candidate: dict[str, Any], diagnostics: VerificationDiagnostics) -> list[dict[str, Any]]:
        """Acquire company-side release evidence without granting verification."""
        if not self.fmp_provider.enabled:
            return []
        _attempt(diagnostics, "FMP_PRESS_RELEASE")
        releases = self.fmp_provider.search_press_releases(symbol, self.market_date, self.market_date)
        output: list[dict[str, Any]] = []
        candidate_text = " ".join(str(candidate.get(key) or "") for key in ("title", "summary", "category")).lower()
        event_words = {word for word in re.findall(r"[a-z0-9]{4,}", candidate_text) if word not in {"company", "announces", "reported"}}
        for release in releases:
            release_text = f"{release.title} {release.summary or ''}".lower()
            matched_company = symbol.upper() in {str(value).upper() for value in release.affected_symbols} or symbol.lower() in release_text
            claim_match = bool(event_words & set(re.findall(r"[a-z0-9]{4,}", release_text)))
            if not matched_company or not claim_match:
                continue
            output.append({"source_type": "OFFICIAL", "source_domain": release.source_domain, "source_name": "FMP Press Release", "title": release.title, "summary": release.summary, "published_at": release.published_at, "affected_symbols": [symbol], "source_url": release.source_url, "provider": "FMP", "content_type": "PRESS_RELEASE", "company_symbol": symbol, "matched_company": matched_company, "event_type": candidate.get("category"), "claim_match": claim_match})
        self.fmp_provider.evidence_matches += len(output)
        self.fmp_provider.no_match += int(not output)
        return output

    def _official_discovery(self, candidate: dict[str, Any], diagnostics: VerificationDiagnostics) -> list[dict[str, Any]]:
        """Find a likely official domain through bounded search, then verify ownership by domain/name."""
        names = _candidate_names(candidate)
        if not names:
            return []
        source = "official_discovery"
        cached = self._load_cache(candidate, source)
        _attempt(diagnostics, "OFFICIAL_IR_DISCOVERY", "CACHE_HIT" if cached is not None else ("CACHE_MISS" if self.cache_only else "CHECKED"))
        if cached is not None:
            return cached
        query = '"' + '" "'.join(names[:3]) + '" official newsroom'
        response = self._request(GOOGLE_NEWS_RSS, params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
        if response is None:
            return []
        try:
            root = ElementTree.fromstring(response.content)
        except (ElementTree.ParseError, AttributeError):
            return []
        for node in root.findall("./channel/item"):
            link = (node.findtext("link") or "").strip()
            domain = _domain(link)
            domain_text = domain.replace(".", " ").replace("-", " ")
            if not domain or not any(name.lower() in domain_text for name in names):
                continue
            direct = self._request(link)
            title = _clean_text(node.findtext("title") or candidate.get("title") or "")
            published = _parse_date(node.findtext("pubDate"))
            if direct is not None:
                title, published_text = _metadata_from_html(direct.text[:2_000_000], link, title, published.isoformat() if published else candidate.get("published_at"))
            else:
                published_text = published.isoformat() if published else candidate.get("published_at")
            result = {"source_type": "OFFICIAL", "source_domain": domain, "title": title, "published_at": published_text, "affected_symbols": candidate.get("affected_symbols", []), "entity_names": names, "source_url": link, "official_discovery": True}
            self._save_cache(candidate, source, [result])
            mapping = OFFICIAL_DISCOVERY_CACHE_DIR / self.market_date / f"{domain.replace('.', '_')}.json"
            mapping.parent.mkdir(parents=True, exist_ok=True)
            mapping.write_text(json.dumps({"domain": domain, "url": link, "entity_names": names}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return [result]
        self._save_cache(candidate, source, [])
        return []

    def _sec(self, symbol: str, candidate: dict[str, Any], diagnostics: VerificationDiagnostics) -> list[dict[str, Any]]:
        """Read recent SEC filing metadata without downloading filing bodies."""
        cached = self._load_cache(candidate, f"sec_{symbol}")
        _attempt(diagnostics, "SEC_EDGAR", "CACHE_HIT" if cached is not None else ("CACHE_MISS" if self.cache_only else "CHECKED"))
        if cached is not None:
            return cached
        if self._sec_cik_by_symbol is None and not (candidate.get("sec_cik") or candidate.get("cik")):
            response = self._request(SEC_TICKERS_URL)
            if response is None:
                self._sec_cik_by_symbol = {}
            else:
                try:
                    payload = response.json()
                    self._sec_cik_by_symbol = {str(item.get("ticker", "")).upper(): str(item.get("cik_str", "")).zfill(10) for item in payload.values() if isinstance(item, dict) and item.get("ticker") and item.get("cik_str")}
                except (ValueError, AttributeError, TypeError):
                    self._sec_cik_by_symbol = {}
        raw_cik = candidate.get("sec_cik") or candidate.get("cik") or (self._sec_cik_by_symbol or {}).get(symbol.upper())
        cik = str(raw_cik).zfill(10) if raw_cik else None
        if not cik:
            return []
        response = self._request(SEC_SUBMISSIONS_URL.format(cik=cik))
        if response is None:
            return []
        try:
            recent = response.json().get("filings", {}).get("recent", {})
        except (ValueError, AttributeError, TypeError):
            return []
        output: list[dict[str, Any]] = []
        forms = recent.get("form", [])
        for index, form in enumerate(forms):
            if form not in {"8-K", "6-K"}:
                continue
            filing_date = str(recent.get("filingDate", [""])[index]) if index < len(recent.get("filingDate", [])) else ""
            if filing_date != self.market_date and filing_date != (datetime.fromisoformat(self.market_date) - timedelta(days=1)).date().isoformat():
                continue
            items = recent.get("items", [])[index] if index < len(recent.get("items", [])) else []
            item_text = " ".join(str(value) for value in items)
            doc = str(recent.get("primaryDocument", [""])[index]) if index < len(recent.get("primaryDocument", [])) else ""
            accession = str(recent.get("accessionNumber", [""])[index]).replace("-", "") if index < len(recent.get("accessionNumber", [])) else ""
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{doc}" if doc and accession else "https://www.sec.gov/edgar.shtml"
            evidence = {"source_type": "OFFICIAL", "source_domain": "sec.gov", "title": f"SEC {form} filing for {symbol}", "summary": item_text, "published_at": f"{filing_date}T00:00:00+00:00", "affected_symbols": [symbol], "source_url": url, "filing_form": form, "filing_items": item_text}
            # A fresh 8-K/6-K filing is metadata-only until its linked document is inspected.
            if form in {"8-K", "6-K"} and url != "https://www.sec.gov/edgar.shtml":
                filing_response = self._request(url)
                if filing_response is not None:
                    exhibit_titles, exhibit_text, exhibit_links = _filing_metadata(filing_response.text[:2_000_000])
                    if exhibit_links:
                        exhibit_url = exhibit_links[0]
                        if not exhibit_url.startswith("http"):
                            exhibit_url = "https://www.sec.gov/Archives/edgar/data/" + str(int(cik)) + "/" + accession + "/" + exhibit_url.lstrip("/")
                        exhibit_response = self._request(exhibit_url)
                        if exhibit_response is not None:
                            _, exhibit_detail = _filing_metadata(exhibit_response.text[:2_000_000])[:2]
                            exhibit_text = f"{exhibit_text} {exhibit_detail}"[:12_000]
                    evidence.update({"exhibit_titles": exhibit_titles, "exhibit_text": exhibit_text, "sec_exhibit_match": bool(exhibit_titles or exhibit_text)})
            output.append(evidence)
        self._save_cache(candidate, f"sec_{symbol}", output)
        return output

    def _tier2(self, candidate: dict[str, Any], diagnostics: VerificationDiagnostics) -> list[dict[str, Any]]:
        symbols = [str(value).upper() for value in candidate.get("affected_symbols", []) if value]
        query = str(candidate.get("title") or "")
        if symbols:
            query = f'"{symbols[0]}" "{query}"'
        source = "tier2_search"
        cached = self._load_cache(candidate, source)
        if cached is not None:
            _attempt(diagnostics, "TIER2_SEARCH", "CACHE")
            return [item for item in cached if item.get("source_domain") != _domain(str(candidate.get("source_url") or ""))]
        _attempt(diagnostics, "TIER2_SEARCH", "CACHE_MISS" if self.cache_only else "CHECKED")
        response = self._request(GOOGLE_NEWS_RSS, params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
        if response is None:
            return []
        try:
            root = ElementTree.fromstring(response.content)
        except (ElementTree.ParseError, AttributeError):
            return []
        candidate_domain = _domain(str(candidate.get("source_url") or ""))
        for node in root.findall("./channel/item"):
            link = (node.findtext("link") or "").strip()
            domain = _domain(link)
            if domain not in TIER2_DOMAINS or domain == candidate_domain:
                continue
            title = _clean_text(node.findtext("title") or "")
            published = _parse_date(node.findtext("pubDate"))
            if not title or not published or published.date().isoformat() < "0000-00-00":
                continue
            direct = self._request(link)
            direct_title, direct_published = title, published.isoformat()
            if direct is not None:
                direct_title, direct_published = _metadata_from_html(direct.text[:2_000_000], link, title, direct_published)
            item = {"source_type": "TIER2", "source_domain": domain, "title": direct_title, "published_at": direct_published, "affected_symbols": symbols, "source_url": link}
            self._save_cache(candidate, source, [item])
            return [item]
        self._save_cache(candidate, source, [])
        return []
