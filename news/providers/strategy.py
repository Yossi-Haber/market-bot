"""Budget-aware Marketaux-first, Finnhub-secondary discovery strategy."""

from __future__ import annotations

from typing import Any

from news.schema import NewsCandidate
from .finnhub import MIN_TARGETED_SUBJECT_RELEVANCE, cluster_finnhub_candidates


class CompanyNewsStrategy:
    def __init__(self, primary: Any, secondary: Any, tertiary: Any = None, max_requests: int = 4) -> None:
        if isinstance(tertiary, int) and max_requests == 4:
            max_requests, tertiary = tertiary, None
        self.primary, self.secondary, self.tertiary = primary, secondary, tertiary
        self.max_requests = max_requests
        self.requests = 0
        self.metrics = {"marketaux_requests": 0, "finnhub_requests": 0, "fmp_news_requests": 0, "marketaux_candidates": 0, "finnhub_candidates": 0, "fmp_news_candidates": 0, "cross_provider_duplicates_removed": 0, "finnhub_raw_articles": 0, "finnhub_subject_gate_passed": 0, "finnhub_subject_gate_rejected": 0, "finnhub_candidates_to_verification": 0, "finnhub_driver_candidates": 0, "finnhub_fallback_activated": False, "fmp_fallback_activated": False, "finnhub_cache_hits": 0, "finnhub_cache_misses": 0, "finnhub_live_requests": 0, "finnhub_cache_writes": 0}

    def search_company(self, symbol: str, start_date: str, end_date: str, company_name: str = "") -> list[NewsCandidate]:
        if self.requests >= self.max_requests:
            return []
        primary_available = bool(getattr(self.primary, "available", getattr(self.primary, "enabled", True)))
        primary = self.primary.search_company(symbol, start_date, end_date) if primary_available else []
        if primary_available:
            self.requests += 1
            self.metrics["marketaux_requests"] += 1
            self.metrics["marketaux_candidates"] += len(primary)
        if primary:
            return self._dedupe(primary)
        accepted: list[NewsCandidate] = []
        if self.requests < self.max_requests and getattr(self.secondary, "enabled", True):
            secondary = self.secondary.search_company(symbol, start_date, end_date)
            self.requests += 1
            self.metrics["finnhub_requests"] += 1
            self.metrics["finnhub_candidates"] += len(secondary)
            self.metrics["finnhub_raw_articles"] += int(getattr(self.secondary, "last_raw_count", len(secondary)))
            self.metrics["finnhub_fallback_activated"] = True
            self.metrics["finnhub_cache_hits"] += int(getattr(self.secondary, "cache_hits", 0))
            self.metrics["finnhub_cache_misses"] += int(getattr(self.secondary, "cache_misses", 0))
            self.metrics["finnhub_live_requests"] += int(getattr(self.secondary, "live_requests", 0))
            self.metrics["finnhub_cache_writes"] += int(getattr(self.secondary, "cache_writes", 0))
            clusters = cluster_finnhub_candidates(secondary, symbol, company_name)
            ranked = [candidate for cluster in clusters for candidate in cluster["ranked_candidates"]]
            accepted = [cluster["best_candidate"] for cluster in clusters if cluster["subject_score"] >= MIN_TARGETED_SUBJECT_RELEVANCE]
            self.metrics["finnhub_subject_gate_passed"] += sum(1 for item in ranked if item["subject_relevance_score"] >= MIN_TARGETED_SUBJECT_RELEVANCE)
            self.metrics["finnhub_subject_gate_rejected"] += sum(1 for item in ranked if item["subject_relevance_score"] < MIN_TARGETED_SUBJECT_RELEVANCE)
            self.metrics["finnhub_driver_candidates"] += len(accepted)
            self.metrics["finnhub_candidates_to_verification"] += len(accepted)
        if accepted:
            return self._dedupe([*primary, *accepted])
        if self.requests >= self.max_requests or not getattr(self.tertiary, "enabled", False):
            return self._dedupe(primary)
        fmp_items = self.tertiary.search_company(symbol, start_date, end_date)
        self.requests += 1
        self.metrics["fmp_news_requests"] += 1
        self.metrics["fmp_news_candidates"] += len(fmp_items)
        self.metrics["fmp_fallback_activated"] = True
        return self._dedupe([*primary, *fmp_items])

    def _dedupe(self, candidates: list[NewsCandidate]) -> list[NewsCandidate]:
        seen: set[str] = set()
        output: list[NewsCandidate] = []
        for item in candidates:
            published = str(item.published_at or "")[:16]
            symbols = ",".join(sorted(str(value).upper() for value in item.affected_symbols))
            key = "|".join(((item.source_url or "").strip().rstrip("/").lower(), " ".join(item.title.lower().split()), published, symbols))
            if key not in seen:
                seen.add(key)
                output.append(item)
        self.metrics["cross_provider_duplicates_removed"] += len(candidates) - len(output)
        return output
