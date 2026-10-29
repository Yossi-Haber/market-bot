"""Optional Marketaux discovery provider; never verifies stories by itself."""

from __future__ import annotations

import json
import logging
import os
import hashlib
import re
import time as time_module
from datetime import datetime, time, timedelta, timezone
from datetime import date
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

from news.schema import NewsCandidate
from news.evidence_capture import observe
from news.performance import NewsBudgetExhausted, ProviderCircuitOpen
from cache_config import cache_path

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = cache_path("marketaux")
API_URL = "https://api.marketaux.com/v1/news/all"
REPORT_TZ = ZoneInfo("America/New_York")
ELIGIBLE_ARTICLE_TYPES = {"NEWS_EVENT", "MARKET_RECAP"}
LOW_QUALITY_DOMAINS = {"marketbeat.com", "stockmarketwatch.com", "investorplace.com", "fxstreet.com", "walletinvestor.com", "coincodex.com"}
ESTABLISHED_DOMAINS = {"reuters.com", "apnews.com", "cnbc.com", "bloomberg.com", "wsj.com", "ft.com", "marketwatch.com"}
FORECAST_TERMS = ("forecast", "price target", "may rise", "could reach", "expected to hit", "bull case", "bear case", "base case", "prediction", "predict", "outlook")
PREVIEW_TERMS = ("week ahead", "weekly outlook", "what to watch", "preview", "ahead of next week", "coming week", "next week")
OPINION_TERMS = ("opinion", "why i", "investors should", "best stocks", "top picks", "buy now")
AGGREGATION_TERMS = ("according to analysts", "stocks to watch", "market roundup", "daily roundup", "top stories")
EVENT_TERMS = ("announced", "announces", "earnings", "revenue", "guidance", "acquisition", "merger", "partnership", "launch", "approved", "regulator", "lawsuit", "files", "decision", "report released")
RECAP_TERMS = ("stocks ended", "market closed", "markets ended", "session recap", "market recap", "wall street today", "s&p 500 rose", "nasdaq rose")


def infer_category(title: str) -> str:
    """Infer only a broad event category from explicit headline language."""
    text = re.sub(r"\s+", " ", title.lower())
    if any(term in text for term in ("earnings", "revenue", "guidance", "profit", "quarter")):
        return "EARNINGS"
    if any(term in text for term in ("acquisition", "acquire", "merger", "merges", "takeover")):
        return "M&A"
    if any(term in text for term in ("regulator", "regulatory", "approved", "approval", "lawsuit", "investigation")):
        return "REGULATORY"
    if any(term in text for term in ("partnership", "product launch", "launched", "launches")):
        return "TECH_PRODUCT"
    if any(term in text for term in ("financing", "funding", "bankruptcy")):
        return "COMPANY"
    if any(term in text for term in ("federal reserve", "interest rate", "treasury")):
        return "CENTRAL_BANK"
    return "OTHER"


class MarketauxDiscovery:
    """Small-budget, cached discovery client with safe disabled behavior."""

    def __init__(self, logger: logging.Logger, market_date: str, token: str | None = None, max_requests: int = 6, request_get: Callable[..., requests.Response] | None = None, refresh_cache: bool = False) -> None:
        self.logger, self.market_date = logger, date.fromisoformat(market_date)
        self.token = (token if token is not None else os.getenv("MARKETAUX_API_TOKEN", "")).strip()
        self.max_requests, self.request_get, self.refresh_cache = max_requests, request_get or requests.get, refresh_cache
        self.request_count = 0
        self.requests_attempted = 0
        self.requests_succeeded = 0
        self.requests_failed = 0
        self.cache_hits = 0
        # Targeted coverage has its own four-request allowance so the general
        # discovery cap cannot silently consume the escalation capacity.
        self.targeted_requests_attempted = 0
        self.targeted_max_requests = 4
        self.available_for_run = True
        self.usage_limit_reached = False
        self.requests_skipped_usage_limit = 0
        self.diagnostics = {"marketaux_candidates_total": 0, "marketaux_in_window": 0, "marketaux_filtered_future": 0, "marketaux_filtered_stale": 0, "marketaux_filtered_low_quality": 0, "marketaux_filtered_non_news": 0, "marketaux_filtered_forecast": 0}
        self.query_diagnostics: list[dict[str, Any]] = []

    @property
    def enabled(self) -> bool:
        return bool(self.token) and self.max_requests > 0

    def search(self, query: str = "", query_type: str = "GENERAL", symbols: list[str] | None = None) -> list[NewsCandidate]:
        if not self.enabled:
            observe("marketaux", [], scope=query_type, symbols=symbols or (), state="UNAVAILABLE", limitations=["DISABLED_OR_UNCONFIGURED"])
            return []
        if query_type == "TARGETED_MOVER":
            if self.targeted_requests_attempted >= self.targeted_max_requests:
                observe("marketaux", [], scope=query_type, symbols=symbols or (), state="BUDGET_NOT_COLLECTED")
                return []
        elif self.request_count >= self.max_requests:
            observe("marketaux", [], scope=query_type, symbols=symbols or (), state="BUDGET_NOT_COLLECTED")
            return []
        symbol_text = ",".join(symbols or [])
        targeted_symbol = str((symbols or [""])[0]).upper() if query_type == "TARGETED_MOVER" else ""
        digest = hashlib.sha256(f"{query}|{symbol_text}".encode("utf-8")).hexdigest()[:16]
        cache_path = CACHE_DIR / self.market_date.isoformat() / f"{query_type.lower()}_{digest}.json"
        query_stats = {"query_type": query_type, "raw_candidates": 0, "in_window": 0, "quality_pass": 0, "non_news_filtered": 0, "forecast_filtered": 0, "final_candidates": 0, "entity_matches": 0, "entity_missing": 0, "found": 0, "returned": 0, "status": None, "target_symbol": targeted_symbol}
        if not self.available_for_run and (self.refresh_cache or not cache_path.exists()):
            observe("marketaux", [], scope=query_type, symbols=symbols or (), state="QUOTA_EXHAUSTED")
            self.requests_skipped_usage_limit += 1
            query_stats.update({"error_code": "MARKETAUX_USAGE_LIMIT_REACHED", "error_message": "request_skipped_usage_limit"})
            self.query_diagnostics.append(query_stats)
            self.logger.info("[NEWS] Marketaux request skipped: query_type=%s reason=MARKETAUX_USAGE_LIMIT_REACHED", query_type)
            return []
        try:
            if cache_path.exists() and not self.refresh_cache:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                self.requests_succeeded += 1
                self.cache_hits += 1
            else:
                self.request_count += 1
                self.requests_attempted += 1
                if query_type == "TARGETED_MOVER":
                    self.targeted_requests_attempted += 1
                start, end = self._report_window()
                params = {"api_token": self.token, "language": "en", "published_after": _provider_datetime(start), "published_before": _provider_datetime(end)}
                if query:
                    if query_type != "TARGETED_MOVER":
                        params["search"] = query
                if symbols:
                    params["symbols"] = ",".join(symbols)
                if query_type == "TARGETED_MOVER":
                    params["filter_entities"] = "true"
                    params["must_have_entities"] = "true"
                    self.logger.info(
                        "Marketaux targeted params: symbol=%s published_after=%s published_before=%s search=%s filter_entities=%s must_have_entities=%s",
                        params.get("symbols", ""), params.get("published_after", ""), params.get("published_before", ""),
                        params.get("search", ""), params.get("filter_entities"), params.get("must_have_entities"),
                    )
                request_timeout = (5, 15) if query_type == "TARGETED_MOVER" else (5, 6)
                request_started = time_module.monotonic()
                response = self.request_get(API_URL, params=params, timeout=request_timeout, headers={"User-Agent": "market_bot/1.0"})
                query_stats["status"] = getattr(response, "status_code", None)
                response.raise_for_status()
                payload = response.json()
                meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
                if isinstance(meta, dict):
                    query_stats["found"] = meta.get("found", 0)
                    query_stats["returned"] = meta.get("returned", 0)
                error = payload.get("error") if isinstance(payload, dict) else None
                if isinstance(error, dict):
                    observe("marketaux", [], scope=query_type, symbols=symbols or (), state="QUOTA_EXHAUSTED" if error.get("code") == "usage_limit_reached" else "UNAVAILABLE")
                    self.requests_failed += 1
                    query_stats.update({"error_code": str(error.get("code") or "MARKETAUX_API_ERROR")[:100], "error_message": _sanitize_provider_error_message(error.get("message"), self.token)})
                    if query_stats["error_code"] == "usage_limit_reached":
                        self._mark_usage_limit_reached()
                    self.query_diagnostics.append(query_stats)
                    self.logger.warning("Marketaux query failed: query_type=%s status=%s error_code=%s message=%s", query_type, query_stats["status"], query_stats["error_code"], query_stats["error_message"])
                    return []
                self.requests_succeeded += 1
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except requests.HTTPError as exc:
            self.requests_failed += 1
            status, code, message = _safe_provider_error(exc, sensitive_value=self.token)
            observe("marketaux", [], scope=query_type, symbols=symbols or (), state="QUOTA_EXHAUSTED" if status == 402 or code == "usage_limit_reached" else "UNAVAILABLE")
            query_stats.update({"status": status, "error_code": code, "error_message": message})
            if status == 402 or code == "usage_limit_reached":
                self._mark_usage_limit_reached()
            self.query_diagnostics.append(query_stats)
            self.logger.warning("Marketaux query failed: query_type=%s status=%s error_code=%s message=%s", query_type, status, code, message)
            return []
        except requests.Timeout:
            observe("marketaux", [], scope=query_type, symbols=symbols or (), state="UNAVAILABLE", limitations=["PROVIDER_TIMEOUT"])
            self.requests_failed += 1
            query_stats.update({"status": query_stats.get("status"), "error_code": "MARKETAUX_TIMEOUT", "error_message": "request_timeout", "timeout_seconds": round(time_module.monotonic() - request_started, 3) if "request_started" in locals() else 0.0})
            self.query_diagnostics.append(query_stats)
            self.logger.warning("Marketaux query failed: query_type=%s error_code=MARKETAUX_TIMEOUT", query_type)
            return []
        except (requests.RequestException, OSError, ValueError) as exc:
            observe("marketaux", [], scope=query_type, symbols=symbols or (), state="UNAVAILABLE", limitations=["PROVIDER_REQUEST_OR_BUDGET_FAILURE"])
            # The guarded transport can reject a call before any HTTP request.
            # Such calls belong in diagnostics, but not provider usage totals.
            blocked_before_http = isinstance(exc, (ProviderCircuitOpen, NewsBudgetExhausted))
            if blocked_before_http:
                self.request_count -= 1
                self.requests_attempted -= 1
                if query_type == "TARGETED_MOVER":
                    self.targeted_requests_attempted -= 1
                query_stats.update({"status": "skipped", "error_code": type(exc).__name__, "error_message": "request_skipped_before_http"})
                self.query_diagnostics.append(query_stats)
                self.logger.info("[NEWS] Marketaux request skipped: query_type=%s reason=%s", query_type, type(exc).__name__)
                return []
            else:
                self.requests_failed += 1
            query_stats.update({"status": None, "error_code": type(exc).__name__, "error_message": "request_failed"})
            self.query_diagnostics.append(query_stats)
            self.logger.warning("Marketaux query failed: query_type=%s status=network error_code=%s message=request_failed", query_type, type(exc).__name__)
            return []
        observe("marketaux", payload, scope=query_type, symbols=symbols or ())
        results: list[NewsCandidate] = []
        for item in payload.get("data", []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict) or not item.get("title"):
                continue
            url = str(item.get("url") or "")
            title = str(item["title"]).strip()
            domain = urlparse(url).netloc.lower().removeprefix("www.")
            published_at = item.get("published_at")
            self.diagnostics["marketaux_candidates_total"] += 1
            query_stats["raw_candidates"] += 1
            in_window, window_state = self._in_report_window(published_at)
            if not in_window:
                self.diagnostics["marketaux_filtered_future" if window_state == "future" else "marketaux_filtered_stale"] += 1
                continue
            self.diagnostics["marketaux_in_window"] += 1
            query_stats["in_window"] += 1
            quality = self._source_quality(domain)
            if quality >= 4:
                self.diagnostics["marketaux_filtered_low_quality"] += 1
                continue
            symbols = [entity.get("symbol") for entity in item.get("entities", []) if isinstance(entity, dict) and entity.get("symbol")]
            entity_names = [str(entity.get("name") or entity.get("company_name")) for entity in item.get("entities", []) if isinstance(entity, dict) and (entity.get("name") or entity.get("company_name"))]
            if query_type == "TARGETED_MOVER":
                requested_symbol = str((query_stats.get("target_symbol") or "")).upper()
                if requested_symbol not in {str(value).upper() for value in symbols}:
                    query_stats["entity_missing"] += 1
                    continue
                query_stats["entity_matches"] += 1
            article_type = classify_article_type(title)
            # Targeted queries are intentionally handed to the targeted
            # relevance check in news.live. General article-type heuristics
            # must not discard a company-specific result before that check.
            targeted_relevant = query_type == "TARGETED_MOVER"
            if article_type in {"PREVIEW", "FORECAST"}:
                if not targeted_relevant:
                    self.diagnostics["marketaux_filtered_forecast"] += 1
                    query_stats["forecast_filtered"] += 1
                    continue
            if article_type not in ELIGIBLE_ARTICLE_TYPES:
                if not targeted_relevant:
                    self.diagnostics["marketaux_filtered_non_news"] += 1
                    query_stats["non_news_filtered"] += 1
                    continue
            score = discovery_score(title, domain, symbols, published_at, self.market_date)
            query_stats["quality_pass"] += 1
            results.append(NewsCandidate(title, item.get("description"), published_at, str(item.get("source") or domain or "Marketaux discovery"), domain, quality, url, infer_category(title), symbols, "marketaux", False, "NOT_CONFIRMED", "OTHER", False, article_type, score, entity_names))
        query_stats["final_candidates"] = len(results)
        self.query_diagnostics.append(query_stats)
        return sorted(results, key=lambda candidate: candidate.discovery_score, reverse=True)

    def _mark_usage_limit_reached(self) -> None:
        """Disable new Marketaux requests for this client instance only."""
        if not self.usage_limit_reached:
            self.logger.warning("[NEWS] Marketaux disabled for current run: reason=MARKETAUX_USAGE_LIMIT_REACHED")
        self.usage_limit_reached = True
        self.available_for_run = False

    def diagnose(self, production_query: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """Run up to five raw API probes without cache or local quality filtering."""
        probes = [
            ("DATE_ONLY", {"language": "en", "limit": 3}),
            ("DATE_PLUS_MUST_HAVE_ENTITIES", {"language": "en", "limit": 3, "must_have_entities": "true"}),
            ("DATE_PLUS_SYMBOL_NVDA", {"language": "en", "limit": 3, "symbols": "NVDA"}),
            ("DATE_PLUS_SIMPLE_SEARCH", {"language": "en", "limit": 3, "search": "Nvidia"}),
            ("PRODUCTION_QUERY", {"language": "en", "limit": 3, **(production_query or {"search": "Nvidia"})}),
        ]
        output: list[dict[str, Any]] = []
        for name, extras in probes[:5]:
            if self.request_count >= 5:
                break
            start, end = self._report_window()
            params = {"api_token": self.token, "published_after": _provider_datetime(start), "published_before": _provider_datetime(end), **extras}
            if isinstance(params.get("symbols"), list):
                params["symbols"] = ",".join(params["symbols"])
            safe_params = {key: value for key, value in params.items() if key != "api_token"}
            result: dict[str, Any] = {"name": name, "params": safe_params, "found": 0, "returned": 0, "limit": extras.get("limit", 3), "data_length": 0, "parsed_count": 0}
            try:
                self.request_count += 1
                self.requests_attempted += 1
                response = self.request_get(API_URL, params=params, timeout=(5, 15), headers={"User-Agent": "market_bot/1.0"})
                response.raise_for_status()
                payload = response.json()
                self.requests_succeeded += 1
                meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
                data = payload.get("data", []) if isinstance(payload, dict) else []
                result.update({"found": meta.get("found", 0), "returned": meta.get("returned", 0), "limit": meta.get("limit", result["limit"]), "data_length": len(data) if isinstance(data, list) else 0, "parsed_count": len(parse_raw_articles(payload))})
            except requests.HTTPError as exc:
                self.requests_failed += 1
                status, code, message = _safe_provider_error(exc)
                result.update({"status": status, "error_code": code, "error_message": message})
            except (requests.RequestException, OSError, ValueError) as exc:
                self.requests_failed += 1
                result.update({"status": None, "error_code": type(exc).__name__, "error_message": "request_failed"})
            self.logger.info("Marketaux diagnostic probe: name=%s params=%s found=%s returned=%s", name, safe_params, result.get("found"), result.get("returned"))
            output.append(result)
        return output

    def _report_window(self) -> tuple[datetime, datetime]:
        start = datetime.combine(self.market_date, time.min, tzinfo=REPORT_TZ)
        end = datetime.combine(self.market_date, time.max, tzinfo=REPORT_TZ) + timedelta(hours=float(os.getenv("MARKETAUX_AFTER_CLOSE_GRACE_HOURS", "2")))
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    def _in_report_window(self, published_at: str | None) -> tuple[bool, str]:
        if not published_at:
            self.diagnostics["marketaux_filtered_stale"] += 1
            return False, "stale"
        try:
            value = datetime.fromisoformat(str(published_at).replace("Z", "+00:00")).astimezone(REPORT_TZ)
            start, end = self._report_window()
            start = start.astimezone(REPORT_TZ)
            end = end.astimezone(REPORT_TZ)
        except (ValueError, TypeError):
            return False, "stale"
        if value < start:
            return False, "stale"
        if value > end:
            return False, "future"
        return True, "in_window"

    @staticmethod
    def _source_quality(domain: str) -> int:
        if domain in LOW_QUALITY_DOMAINS or any(domain.endswith("." + suffix) for suffix in LOW_QUALITY_DOMAINS):
            return 4
        if domain in ESTABLISHED_DOMAINS or any(domain.endswith("." + suffix) for suffix in ESTABLISHED_DOMAINS):
            return 2
        return 3


def _provider_datetime(value: datetime) -> str:
    """Serialize UTC timestamps in Marketaux's timezone-naive format."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def parse_raw_articles(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse the provider's raw article list without applying production filters."""
    data = payload.get("data", []) if isinstance(payload, dict) else []
    return [item for item in data if isinstance(item, dict) and item.get("title")]


def _safe_provider_error(
    exc: requests.HTTPError, *, sensitive_value: str = ""
) -> tuple[int | None, str, str]:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    code, message = "HTTP_ERROR", "provider_request_failed"
    try:
        payload = response.json() if response is not None else {}
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        if isinstance(error, dict):
            code = str(error.get("code") or code)[:100]
            message = _sanitize_provider_error_message(error.get("message") or message, sensitive_value)
    except (ValueError, AttributeError, TypeError):
        pass
    return status, code, message


def _sanitize_provider_error_message(value: Any, sensitive_value: str = "") -> str:
    """Bound provider diagnostics and redact credentials before logs/snapshots."""
    message = re.sub(r"\s+", " ", str(value or "provider_request_failed"))
    if sensitive_value:
        message = message.replace(sensitive_value, "[REDACTED]")
    message = re.sub(
        r"(?i)\bapi[_-]?token\s*[=:]\s*[^,\s&\"']+",
        "api_token=[REDACTED]",
        message,
    )
    return message[:200]


def classify_article_type(title: str) -> str:
    """Classify discovery headlines for the Daily Recap product."""
    text = re.sub(r"\s+", " ", title.lower()).strip()
    if any(term in text for term in PREVIEW_TERMS):
        return "PREVIEW"
    if any(term in text for term in FORECAST_TERMS):
        return "FORECAST"
    if any(term in text for term in OPINION_TERMS):
        return "OPINION"
    if any(term in text for term in AGGREGATION_TERMS):
        return "AGGREGATION"
    if any(term in text for term in RECAP_TERMS):
        return "MARKET_RECAP"
    if any(term in text for term in EVENT_TERMS):
        return "NEWS_EVENT"
    return "OTHER"


def discovery_score(title: str, domain: str, symbols: list[str], published_at: str | None, market_date: date) -> float:
    """Prioritize date-matched, specific, relevant, higher-quality discovery hints."""
    score = 0.0
    score += 4 if domain in ESTABLISHED_DOMAINS else 1 if domain not in LOW_QUALITY_DOMAINS else -4
    score += min(3, len(symbols))
    score += 3 if any(term in title.lower() for term in EVENT_TERMS) else 1
    try:
        published = datetime.fromisoformat(str(published_at).replace("Z", "+00:00")).astimezone(REPORT_TZ).date()
        score += 4 if published == market_date else 0
    except (TypeError, ValueError):
        score -= 3
    return score
