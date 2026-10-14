"""Orchestrate official and discovery news with explicit budgets and verification."""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from news.editorial import cluster_stories
from news.discovery import BroadDiscovery, deduplicate_candidates
from news.marketaux import MarketauxDiscovery
from news.official import OfficialNewsCollector
from news.schema import NewsCandidate
from news.verification_v2 import VerificationDiagnostics, verify_candidate
from news.verification_acquisition import LiveEvidenceAcquirer
from news.performance import NEWS_DISCOVERY_BUDGET_SECONDS, NewsPerformance, ProviderRequest
import requests


@dataclass
class NewsRequestBudget:
    """Per-run request counters exposed for logs and audits."""

    news_requests_total: int = 0
    marketaux_requests: int = 0
    marketaux_requests_attempted: int = 0
    marketaux_requests_succeeded: int = 0
    marketaux_requests_failed: int = 0
    marketaux_usage_limit_reached: bool = False
    marketaux_requests_skipped_usage_limit: int = 0
    marketaux_enabled: bool = False
    marketaux_disabled_reason: str | None = None
    marketaux_expected: bool = False
    official_requests: int = 0
    official_candidates_total: int = 0
    official_verified_total: int = 0
    official_editorial_eligible_total: int = 0
    official_filtered_irrelevant: int = 0
    official_filtered_stale: int = 0
    official_filtered_undated: int = 0
    marketaux_candidates_total: int = 0
    marketaux_in_window: int = 0
    marketaux_filtered_future: int = 0
    marketaux_filtered_stale: int = 0
    marketaux_filtered_low_quality: int = 0
    marketaux_filtered_non_news: int = 0
    marketaux_filtered_forecast: int = 0
    marketaux_queries_executed: int = 0
    marketaux_candidates_after_filter: int = 0
    marketaux_unique_events: int = 0
    query_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    verification_candidates: int = 0
    verification_official_matches: int = 0
    verification_tier2_matches: int = 0
    verification_confirmed: int = 0
    verification_not_confirmed: int = 0
    verification_conflicts: int = 0
    verification_requests: int = 0
    verification_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    official_discovery_seconds: float = 0.0
    marketaux_seconds: float = 0.0
    slow_fallback_seconds: float = 0.0
    verification_seconds: float = 0.0
    slow_fallback_requests: int = 0
    slow_fallback_movers_attempted: int = 0
    slow_fallback_provider_requests: int = 0
    candidates_before_relevance_filter: int = 0
    candidates_after_relevance_filter: int = 0
    irrelevant_candidates_filtered: int = 0
    early_stop: bool = False
    budget_exhausted: bool = False
    circuit_breakers_opened: int = 0
    cache_mode: str = "NORMAL"
    official_cache_hit: bool = False
    marketaux_cache_hit: bool = False
    verification_cache_hit: bool = False
    live_requests_total: int = 0
    timeouts: int = 0
    coverage_target: int = 2
    verified_stories_before_escalation: int = 0
    verified_stories_after_escalation: int = 0
    targeted_movers_considered: int = 0
    targeted_movers_queried: int = 0
    targeted_official_requests: int = 0
    targeted_marketaux_requests: int = 0
    targeted_marketaux_skipped_usage_limit: int = 0
    targeted_verification_requests: int = 0
    targeted_cache_hits: int = 0
    targeted_cache_misses: int = 0
    targeted_marketaux_http_success: int = 0
    targeted_marketaux_api_errors: int = 0
    targeted_marketaux_articles_returned: int = 0
    targeted_entity_matches: int = 0
    targeted_entity_missing: int = 0
    targeted_candidates_to_verification: int = 0
    targeted_tier2_requests: int = 0
    targeted_verification_sources_attempted: int = 0
    targeted_verification_no_source_attempt: int = 0
    targeted_verification_cache_hits: int = 0
    targeted_verification_schema_failures: int = 0
    targeted_candidates_ranked: int = 0
    targeted_candidates_selected: int = 0
    targeted_second_candidates_attempted: int = 0
    targeted_analysis_candidates_penalized: int = 0
    targeted_subject_mismatch_penalized: int = 0
    targeted_verified_from_rank1: int = 0
    targeted_verified_from_rank2: int = 0
    targeted_subject_gate_passed: int = 0
    targeted_subject_gate_rejected: int = 0
    targeted_event_driven_candidates: int = 0
    targeted_commentary_candidates: int = 0
    general_marketaux_timeouts: int = 0
    general_marketaux_timeout_seconds_total: float = 0.0
    targeted_marketaux_timeouts: int = 0
    fmp_news_requests: int = 0
    fmp_press_release_requests: int = 0
    fmp_news_candidates: int = 0
    fmp_press_release_candidates: int = 0
    fmp_evidence_matches: int = 0
    fmp_no_match: int = 0
    fmp_errors: int = 0
    fmp_fallback_activated: bool = False
    fmp_plan_restriction: bool = False
    fmp_requests_skipped_plan_restriction: int = 0
    subject_gate_validation_passed: bool = False
    coverage_deadline_exhausted: bool = False
    coverage_requests_skipped_budget: int = 0
    second_candidates_skipped_budget: int = 0
    coverage_escalation_started: bool = False
    coverage_escalation_skipped_reason: str | None = None
    targeted_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    coverage_escalation_seconds: float = 0.0
    coverage_target_met: bool = False
    news_coverage_status: str = "INSUFFICIENT"

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def feature_enabled(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


class LiveNewsCoverage:
    """Collect candidates safely; verification is deterministic and conservative."""

    def __init__(self, logger: logging.Logger, market_date: str, request_get: Any = None, refresh_marketaux: bool = False, perform_verification: bool = False, refresh_verification: bool = False, budget_seconds: float = NEWS_DISCOVERY_BUDGET_SECONDS, force_live: bool = False) -> None:
        self.logger, self.market_date, self.budget = logger, market_date, NewsRequestBudget()
        self.perform_verification = perform_verification
        self.refresh_marketaux = refresh_marketaux
        self.force_live = force_live
        self.performance = NewsPerformance(logger, budget_seconds=budget_seconds)
        base_request = request_get or requests.get
        self.official = OfficialNewsCollector(logger, market_date, request_get=ProviderRequest(self.performance, "official", base_request), refresh_cache=force_live)
        self.marketaux = MarketauxDiscovery(logger, market_date, request_get=ProviderRequest(self.performance, "marketaux", base_request), refresh_cache=refresh_marketaux or force_live)
        self.verification_acquirer = LiveEvidenceAcquirer(logger, market_date, request_get=ProviderRequest(self.performance, "verification", base_request), refresh_cache=refresh_verification or force_live)
        self.slow_discovery = BroadDiscovery(
            logger,
            market_date,
            gdelt_request_get=ProviderRequest(self.performance, "gdelt", base_request),
            google_request_get=ProviderRequest(self.performance, "google_news", base_request),
            refresh_cache=force_live,
        )

    def collect(self, queries: list[str] | None = None, symbols: list[str] | None = None, mover_symbols: list[str] | None = None, mover_details: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        candidates: list[NewsCandidate] = []
        official_candidates: list[NewsCandidate] = []
        stage_started = time.monotonic()
        if feature_enabled("ENABLE_OFFICIAL_NEWS", True):
            phase_started = time.monotonic()
            official_candidates = self.official.collect(symbols=symbols)
            candidates.extend(official_candidates)
            self.budget.official_requests = self.official.request_count
            self.budget.official_discovery_seconds = round(time.monotonic() - phase_started, 3)
        sufficient_official = any(item.verification == "VERIFIED" and item.editorial_eligible for item in official_candidates)
        if sufficient_official and not self.refresh_marketaux:
            self.budget.early_stop = True
            self.logger.info("[NEWS] Early stop activated: verified_content_sufficient")
        marketaux_configured = self.marketaux.enabled
        marketaux_enabled_by_config = self.force_live or feature_enabled("ENABLE_MARKETAUX", False)
        if not marketaux_configured:
            marketaux_reason = "MARKETAUX_API_KEY_MISSING" if not self.marketaux.token else "MARKETAUX_REQUEST_LIMIT_ZERO"
            self.budget.marketaux_enabled = False
            self.budget.marketaux_disabled_reason = marketaux_reason
        elif not marketaux_enabled_by_config:
            marketaux_reason = "MARKETAUX_CONFIG_DISABLED"
            self.budget.marketaux_enabled = False
            self.budget.marketaux_disabled_reason = marketaux_reason
        elif self.budget.early_stop:
            marketaux_reason = "MARKETAUX_SKIPPED_BY_POLICY"
            self.budget.marketaux_enabled = True
            self.budget.marketaux_disabled_reason = marketaux_reason
        elif self.performance.exhausted:
            marketaux_reason = "NEWS_BUDGET_EXHAUSTED"
            self.budget.marketaux_enabled = True
            self.budget.marketaux_disabled_reason = marketaux_reason
        else:
            marketaux_reason = "CONFIGURED"
            self.budget.marketaux_enabled = True
            self.budget.marketaux_expected = True
            self.budget.marketaux_disabled_reason = None
        self.logger.info("[NEWS] Marketaux status: %s reason=%s", "ENABLED" if self.budget.marketaux_enabled else "DISABLED", marketaux_reason)
        if marketaux_configured and marketaux_enabled_by_config and not self.budget.early_stop and not self.performance.exhausted:
            phase_started = time.monotonic()
            query_plan = self.normalize_query_plan(queries) if queries else self.default_queries(mover_symbols or symbols)
            if not query_plan:
                self.budget.marketaux_expected = False
                self.budget.marketaux_disabled_reason = "MARKETAUX_QUERY_PLAN_EMPTY"
                self.logger.info("[NEWS] Marketaux status: DISABLED reason=MARKETAUX_QUERY_PLAN_EMPTY")
            else:
                for item in query_plan[:6]:
                    if self.performance.exhausted:
                        break
                    candidates.extend(self.marketaux.search(item.get("query", ""), item["query_type"], item.get("symbols")))
            self.budget.marketaux_seconds = round(time.monotonic() - phase_started, 3)
            self.budget.marketaux_requests = self.marketaux.request_count
            self.budget.marketaux_requests_attempted = self.marketaux.requests_attempted
            self.budget.marketaux_requests_succeeded = self.marketaux.requests_succeeded
            self.budget.marketaux_requests_failed = self.marketaux.requests_failed
            self.budget.marketaux_usage_limit_reached = self.marketaux.usage_limit_reached
            self.budget.marketaux_requests_skipped_usage_limit = self.marketaux.requests_skipped_usage_limit
            for key in ("marketaux_candidates_total", "marketaux_in_window", "marketaux_filtered_future", "marketaux_filtered_stale", "marketaux_filtered_low_quality", "marketaux_filtered_non_news", "marketaux_filtered_forecast"):
                setattr(self.budget, key, self.marketaux.diagnostics[key])
            self.budget.marketaux_queries_executed = sum(1 for item in self.marketaux.query_diagnostics if "error_code" not in item)
            self.budget.marketaux_candidates_after_filter = sum(item["final_candidates"] for item in self.marketaux.query_diagnostics)
            self.budget.query_diagnostics = self.marketaux.query_diagnostics
        self.budget.candidates_before_relevance_filter = len(candidates)
        candidates = [item for item in candidates if _is_relevant_mover_candidate(item, mover_symbols)]
        self.budget.candidates_after_relevance_filter = len(candidates)
        self.budget.irrelevant_candidates_filtered = self.budget.candidates_before_relevance_filter - self.budget.candidates_after_relevance_filter
        if self.budget.irrelevant_candidates_filtered:
            self.logger.info("[NEWS] Mover relevance filter: before=%d after=%d filtered=%d", self.budget.candidates_before_relevance_filter, self.budget.candidates_after_relevance_filter, self.budget.irrelevant_candidates_filtered)
        useful_candidates = [item for item in candidates if getattr(item, "editorial_eligible", False) or item.discovery_source == "marketaux"]
        slow_disabled = feature_enabled("NEWS_DISABLE_SLOW_FALLBACK", False)
        slow_available = self.force_live or self.marketaux.enabled
        slow_needed = not useful_candidates and bool(mover_symbols) and slow_available
        slow_reason = "disabled" if slow_disabled else "marketaux_disabled" if not slow_available else "no_usable_general_or_mover_candidates" if slow_needed else "sufficient_candidates"
        self.logger.info("[NEWS] Slow fallback required: %s reason=%s", "NO" if not slow_needed or slow_disabled else "YES", slow_reason)
        if slow_needed and not slow_disabled and not self.performance.exhausted:
            phase_started = time.monotonic()
            slow_candidates: list[NewsCandidate] = []
            for symbol in mover_symbols[:3]:
                if self.performance.exhausted:
                    break
                self.budget.slow_fallback_movers_attempted += 1
                found, _, _ = self.slow_discovery.search(symbol, symbol)
                self.budget.candidates_before_relevance_filter += len(found)
                relevant_found = [item for item in found if _is_relevant_mover_candidate(item, mover_symbols)]
                self.budget.candidates_after_relevance_filter += len(relevant_found)
                self.budget.irrelevant_candidates_filtered += len(found) - len(relevant_found)
                slow_candidates.extend(relevant_found)
            candidates.extend(deduplicate_candidates(slow_candidates))
            self.budget.slow_fallback_seconds = round(time.monotonic() - phase_started, 3)
            self.budget.slow_fallback_provider_requests = self.slow_discovery.gdelt.request_count + self.slow_discovery.google_news.request_count
            self.budget.slow_fallback_requests = self.budget.slow_fallback_provider_requests
        if self.budget.irrelevant_candidates_filtered:
            self.logger.info("[NEWS] Mover relevance filter: before=%d after=%d filtered=%d", self.budget.candidates_before_relevance_filter, self.budget.candidates_after_relevance_filter, self.budget.irrelevant_candidates_filtered)
        self.budget.news_requests_total = self.budget.official_requests + self.budget.marketaux_requests
        self.budget.official_candidates_total = len(official_candidates)
        self.budget.official_verified_total = sum(item.verification == "VERIFIED" for item in official_candidates)
        self.budget.official_editorial_eligible_total = sum(item.editorial_eligible for item in official_candidates)
        self.budget.official_filtered_irrelevant = sum(item.verification == "VERIFIED" and not item.editorial_eligible and item.content_type not in {"BLOG", "INDEX_PAGE", "DATA_PAGE"} for item in official_candidates)
        self.budget.official_filtered_stale = sum(bool(item.editorial_eligible is False and item.published_at and item.content_type in {"RELEASE", "FILING", "SPEECH"}) for item in official_candidates)
        self.budget.official_filtered_undated = sum(item.editorial_eligible is False and not item.published_at for item in official_candidates)
        candidates = sorted(candidates, key=lambda item: getattr(item, "discovery_score", 0), reverse=True)
        marketaux_candidates = [item for item in candidates if item.discovery_source == "marketaux"]
        discovery_for_verification = [item for item in candidates if item.discovery_source != "official"][:5]
        self.budget.verification_candidates = len(discovery_for_verification)
        verification_started = time.monotonic()
        verified = verify_candidates(candidates[:15])
        if discovery_for_verification:
            acquired = self.verification_acquirer.acquire([item.as_dict() for item in discovery_for_verification]) if self.perform_verification else {}
            verification_results = []
            for item in discovery_for_verification:
                evidence, acquisition_diagnostics = acquired.get(item.title, (item.as_dict().get("verification_evidence", []), None))
                result, diagnostics = verify_candidate(item.as_dict(), evidence)
                if acquisition_diagnostics is not None:
                    diagnostics.attempted_sources = acquisition_diagnostics.attempted_sources
                    attempts = [attempt if isinstance(attempt, dict) else {"type": str(attempt), "status": "CHECKED"} for attempt in diagnostics.attempted_sources]
                    diagnostics.attempted_sources = attempts
                    if diagnostics.matched_source_type:
                        for attempt in attempts:
                            if attempt.get("type", "").startswith(diagnostics.matched_source_type):
                                attempt["status"] = "MATCH"
                    elif any(attempt.get("type", "").startswith(("SEC_EDGAR", "OFFICIAL_IR", "OFFICIAL_REGULATOR", "REGULATOR_OFFICIAL")) for attempt in attempts):
                        diagnostics.reason = "official_path_checked_no_match"
                    elif any(attempt.get("type") == "TIER2_SEARCH" for attempt in attempts):
                        diagnostics.reason = "tier2_checked_no_match"
                    else:
                        diagnostics.reason = "no_supported_source_path"
                verification_results.append(result)
                self.budget.verification_diagnostics.append({"title": item.title, **diagnostics.as_dict()})
                if diagnostics.matched_source_type == "OFFICIAL":
                    self.budget.verification_official_matches += 1
                elif diagnostics.matched_source_type == "TIER2":
                    self.budget.verification_tier2_matches += 1
                if result["verification"] == "VERIFIED":
                    self.budget.verification_confirmed += 1
                elif result["verification"] == "REJECTED_CONFLICT":
                    self.budget.verification_conflicts += 1
                else:
                    self.budget.verification_not_confirmed += 1
            by_title = {item.get("title"): item for item in verification_results}
            verified = [by_title.get(item.get("title"), item) for item in verified]
        self.budget.verified_stories_before_escalation = _verified_story_count(verified)
        self.budget.coverage_target_met = self.budget.verified_stories_before_escalation >= self.budget.coverage_target
        if not self.budget.coverage_target_met and self.perform_verification and mover_symbols:
            self._run_coverage_escalation(verified, mover_symbols, mover_details or [])
        self.budget.verified_stories_after_escalation = _verified_story_count(verified)
        self.budget.coverage_target_met = self.budget.verified_stories_after_escalation >= self.budget.coverage_target
        self.budget.subject_gate_validation_passed = all(
            ranked.get("subject_relevance_score", 0) >= MIN_TARGETED_SUBJECT_RELEVANCE
            for item in self.budget.targeted_diagnostics
            for ranked in item.get("candidate_ranking", [])
            if ranked.get("selected_for_verification") is True
        )
        self.budget.news_coverage_status = "SUFFICIENT" if self.budget.coverage_target_met else "INSUFFICIENT"
        self.budget.verification_seconds = round(time.monotonic() - verification_started, 3)
        self.budget.marketaux_unique_events = len(verify_candidates(marketaux_candidates[:15]))
        self._populate_marketaux_timeout_metrics()
        self.budget.verification_requests = self.verification_acquirer.request_count
        fmp = getattr(self.verification_acquirer, "fmp_provider", None)
        if fmp is not None:
            self.budget.fmp_news_requests = int(getattr(fmp, "news_requests", 0))
            self.budget.fmp_press_release_requests = int(getattr(fmp, "press_release_requests", 0))
            self.budget.fmp_news_candidates = int(getattr(fmp, "news_candidates", 0))
            self.budget.fmp_press_release_candidates = int(getattr(fmp, "press_release_candidates", 0))
            self.budget.fmp_evidence_matches = int(getattr(fmp, "evidence_matches", 0))
            self.budget.fmp_no_match = int(getattr(fmp, "no_match", 0))
            self.budget.fmp_errors = int(getattr(fmp, "errors", 0))
            self.budget.fmp_plan_restriction = bool(getattr(fmp, "plan_restriction", False))
            self.budget.fmp_requests_skipped_plan_restriction = int(getattr(fmp, "requests_skipped_plan_restriction", 0))
        self.budget.news_requests_total = self.budget.official_requests + self.budget.marketaux_requests + self.verification_acquirer.request_count
        self.budget.marketaux_usage_limit_reached = self.marketaux.usage_limit_reached
        self.budget.marketaux_requests_skipped_usage_limit = self.marketaux.requests_skipped_usage_limit
        self.budget.budget_exhausted = self.performance.exhausted
        self.budget.circuit_breakers_opened = self.performance.circuit_breakers_opened
        self.budget.timeouts = self.performance.timeouts
        self.budget.cache_mode = "FORCE_LIVE" if self.force_live else "NORMAL"
        self.budget.official_cache_hit = self.official.cache_hits > 0
        self.budget.marketaux_cache_hit = self.marketaux.cache_hits > 0
        self.budget.verification_cache_hit = self.verification_acquirer.cache_hits > 0
        self.budget.live_requests_total = self.budget.official_requests + self.budget.marketaux_requests + self.budget.verification_requests + self.budget.slow_fallback_requests
        self.budget.total_news_seconds = round(time.monotonic() - stage_started, 3)
        if self.budget.budget_exhausted:
            self.logger.warning("[NEWS] Continuing with %d existing candidates", len(candidates))
        self.logger.info("[NEWS PERF] marketaux_enabled=%s marketaux_disabled_reason=%s marketaux_requests_attempted=%d marketaux_requests_succeeded=%d slow_fallback_movers_attempted=%d slow_fallback_provider_requests=%d candidates_before_relevance_filter=%d candidates_after_relevance_filter=%d irrelevant_candidates_filtered=%d", self.budget.marketaux_enabled, self.budget.marketaux_disabled_reason, self.budget.marketaux_requests_attempted, self.budget.marketaux_requests_succeeded, self.budget.slow_fallback_movers_attempted, self.budget.slow_fallback_provider_requests, self.budget.candidates_before_relevance_filter, self.budget.candidates_after_relevance_filter, self.budget.irrelevant_candidates_filtered)
        self.logger.info("[NEWS PERF] official_discovery_seconds=%.3f marketaux_seconds=%.3f slow_fallback_seconds=%.3f verification_seconds=%.3f total_news_seconds=%.3f official_requests=%d marketaux_requests=%d slow_fallback_requests=%d verification_requests=%d timeouts=%d circuit_breakers_opened=%d early_stop=%s budget_exhausted=%s", self.budget.official_discovery_seconds, self.budget.marketaux_seconds, self.budget.slow_fallback_seconds, self.budget.verification_seconds, self.budget.total_news_seconds, self.budget.official_requests, self.budget.marketaux_requests, self.budget.slow_fallback_requests, self.budget.verification_requests, self.performance.timeouts, self.performance.circuit_breakers_opened, self.budget.early_stop, self.budget.budget_exhausted)
        self.logger.info("News request budget: %s", self.budget.as_dict())
        return verified, self.budget.as_dict()

    def _run_coverage_escalation(self, verified: list[dict[str, Any]], mover_symbols: list[str], mover_details: list[dict[str, Any]]) -> None:
        """Run at most four mover-specific Marketaux/verification attempts."""
        started = self.performance.clock()
        escalation_deadline = started + 20.0
        for client in (self.marketaux.request_get, self.verification_acquirer.request_get):
            if isinstance(client, ProviderRequest):
                client.set_deadline(escalation_deadline)
        self.budget.coverage_escalation_started = True
        self.logger.info("[NEWS COVERAGE] escalation_started=True")
        self.logger.info("[NEWS COVERAGE] verified_before=%d", self.budget.verified_stories_before_escalation)
        detail_by_symbol = {str(item.get("symbol", "")).upper(): item for item in mover_details if isinstance(item, dict)}
        ordered = sorted(dict.fromkeys(str(symbol).upper() for symbol in mover_symbols), key=lambda symbol: abs(float(detail_by_symbol.get(symbol, {}).get("change_pct", 0) or 0)), reverse=True)
        self.budget.targeted_movers_considered = min(len(ordered), 4)
        if self.marketaux.usage_limit_reached:
            self.budget.coverage_escalation_skipped_reason = "MARKETAUX_USAGE_LIMIT_REACHED"
            self.budget.targeted_marketaux_skipped_usage_limit = self.budget.targeted_movers_considered
            for _symbol in ordered[:4]:
                self.logger.info("[NEWS] Marketaux request skipped: query_type=TARGETED_MOVER symbol=%s reason=MARKETAUX_USAGE_LIMIT_REACHED", _symbol)
        elif not self.budget.marketaux_enabled:
            self.budget.coverage_escalation_skipped_reason = self.budget.marketaux_disabled_reason or "MARKETAUX_DISABLED"
        elif self.performance.exhausted:
            self.budget.coverage_escalation_skipped_reason = "NEWS_BUDGET_EXHAUSTED"
        if self.budget.coverage_escalation_skipped_reason:
            self.logger.info("[NEWS COVERAGE] escalation_skipped reason=%s", self.budget.coverage_escalation_skipped_reason)
            self.budget.coverage_escalation_seconds = max(0.001, round(self.performance.clock() - started, 3))
            for client in (self.marketaux.request_get, self.verification_acquirer.request_get):
                if isinstance(client, ProviderRequest):
                    client.set_deadline(None)
            return
        for symbol in ordered[:4]:
            remaining = min(escalation_deadline - self.performance.clock(), self.performance.budget_seconds - self.performance.elapsed)
            if remaining <= 0 or self.performance.exhausted or _verified_story_count(verified) >= self.budget.coverage_target:
                if remaining <= 0:
                    self.budget.coverage_deadline_exhausted = True
                    self.budget.coverage_requests_skipped_budget += 1
                break
            mover_started = self.performance.clock()
            detail = detail_by_symbol.get(symbol, {})
            company_name = str(detail.get("company_name") or detail.get("name") or detail.get("company") or "").strip()
            aliases = [str(alias).strip() for alias in (detail.get("aliases") or detail.get("company_aliases") or []) if str(alias).strip()]
            company_names = [company_name, *aliases] if company_name else aliases
            # Targeted Marketaux requests use the provider's entity filter;
            # the query text is intentionally empty for the primary request.
            query = ""
            self.logger.info("[NEWS COVERAGE] mover_start symbol=%s", symbol)
            self.logger.info("[NEWS COVERAGE] remaining_budget_before_discovery=%.3f", remaining)
            self.logger.info("[NEWS COVERAGE] Marketaux targeted request symbol=%s query_type=TARGETED_MOVER remaining_escalation_seconds=%.3f remaining_news_seconds=%.3f", symbol, escalation_deadline - self.performance.clock(), self.performance.budget_seconds - self.performance.elapsed)
            before_marketaux = self.marketaux.requests_attempted
            before_marketaux_cache = self.marketaux.cache_hits
            candidates = self.marketaux.search(query, "TARGETED_MOVER", [symbol])
            marketaux_requests = self.marketaux.requests_attempted - before_marketaux
            marketaux_cache_hits = self.marketaux.cache_hits - before_marketaux_cache
            self.budget.targeted_marketaux_requests += marketaux_requests
            self.budget.targeted_cache_hits += marketaux_cache_hits
            self.budget.targeted_cache_misses += marketaux_requests
            # This counter means a real provider request, not merely a cache lookup.
            if marketaux_requests:
                self.budget.targeted_movers_queried += 1
            provider_diagnostic = self.marketaux.query_diagnostics[-1].copy() if self.marketaux.query_diagnostics else {}
            raw_candidates = int(provider_diagnostic.get("raw_candidates", 0))
            in_window = int(provider_diagnostic.get("in_window", 0))
            quality_candidates = int(provider_diagnostic.get("quality_pass", 0))
            non_news_filtered = int(provider_diagnostic.get("non_news_filtered", 0))
            forecast_filtered = int(provider_diagnostic.get("forecast_filtered", 0))
            provider_status = provider_diagnostic.get("status")
            provider_error = provider_diagnostic.get("error_code")
            provider_returned = int(provider_diagnostic.get("returned", raw_candidates) or 0)
            entity_matches = int(provider_diagnostic.get("entity_matches", 0) or 0)
            entity_missing = int(provider_diagnostic.get("entity_missing", 0) or 0)
            self.budget.targeted_marketaux_http_success += int(bool(marketaux_requests and provider_status is not None and not provider_error))
            self.budget.targeted_marketaux_api_errors += int(bool(provider_error))
            self.budget.targeted_marketaux_articles_returned += provider_returned
            self.budget.targeted_entity_matches += entity_matches
            self.budget.targeted_entity_missing += entity_missing
            rejection_reasons: dict[str, int] = {}
            if raw_candidates > in_window:
                rejection_reasons["OUTSIDE_MARKET_DATE_WINDOW"] = raw_candidates - in_window
            if in_window > quality_candidates:
                rejection_reasons["LOW_SOURCE_QUALITY"] = in_window - quality_candidates
            if non_news_filtered:
                rejection_reasons["NON_NEWS"] = non_news_filtered
            if forecast_filtered:
                rejection_reasons["FORECAST_OR_PREVIEW"] = forecast_filtered
            candidates_before_relevance = len(candidates)
            relevant_candidates = [item for item in candidates if _is_relevant_mover_candidate(item, [symbol], company_names or None)]
            if candidates_before_relevance > len(relevant_candidates):
                rejection_reasons["INCIDENTAL_OR_UNRELATED_MENTION"] = candidates_before_relevance - len(relevant_candidates)
            candidates = _deduplicate_news_candidates(relevant_candidates)
            deduped_count = len(candidates)
            if len(relevant_candidates) > deduped_count:
                rejection_reasons["DUPLICATE_CANDIDATE"] = len(relevant_candidates) - deduped_count
            gate_results = []
            gated_candidates = []
            for candidate in candidates:
                subject_score, gate_reason = _targeted_subject_relevance(candidate, symbol, company_names)
                passed = subject_score >= MIN_TARGETED_SUBJECT_RELEVANCE
                gate_results.append({"headline": candidate.title, "subject_relevance_score": subject_score, "subject_gate": "PASS" if passed else "FAIL", "subject_gate_reason": gate_reason})
                if passed:
                    gated_candidates.append(candidate)
                    self.budget.targeted_subject_gate_passed += 1
                else:
                    self.budget.targeted_subject_gate_rejected += 1
            candidates = gated_candidates
            self.budget.targeted_diagnostics.append({
                "symbol": symbol,
                "company_name": company_name,
                "query": query,
                "status": provider_status,
                "found": int(provider_diagnostic.get("found", 0) or 0),
                "returned": provider_returned,
                "entity_matches": entity_matches,
                "entity_missing": entity_missing,
                "raw_results": raw_candidates,
                "in_window": in_window,
                "after_quality_filter": quality_candidates,
                "after_non_news_filter": max(0, quality_candidates - non_news_filtered),
                "after_forecast_filter": max(0, quality_candidates - non_news_filtered - forecast_filtered),
                "after_relevance_filter": len(relevant_candidates),
                "after_subject_gate": len(candidates),
                "subject_gate_results": gate_results,
                "deduplicated_candidates": deduped_count,
                "verification_candidates": min(deduped_count, 2),
                "rejection_reasons": rejection_reasons,
            })
            ranked = _rank_targeted_candidates(candidates, symbol, company_names)
            self.budget.targeted_candidates_ranked += len(ranked)
            self.budget.targeted_analysis_candidates_penalized += sum(1 for item in ranked if item["analysis_penalty"] > 0)
            self.budget.targeted_subject_mismatch_penalized += sum(1 for item in ranked if item["subject_relevance_score"] < 25)
            self.budget.targeted_event_driven_candidates += sum(1 for item in ranked if item["event_signal_score"] >= 32)
            self.budget.targeted_commentary_candidates += sum(1 for item in ranked if item["event_signal_score"] < 32)
            self.budget.targeted_candidates_selected += min(len(ranked), 2)
            self.budget.targeted_diagnostics[-1]["verification_candidates"] = min(len(ranked), 2)
            self.budget.targeted_diagnostics[-1]["candidate_ranking"] = [
                {key: value for key, value in item.items() if key != "candidate"} for item in ranked
            ]
            self.logger.info("[NEWS COVERAGE] %s Marketaux: status=%s found=%s returned=%s entity_matches=%d entity_missing=%d to_verification=%d", symbol, provider_status, provider_diagnostic.get("found", 0), provider_returned, entity_matches, entity_missing, min(len(ranked), 2))
            self.logger.info("[NEWS COVERAGE] %s targeted diagnostics: query=%s raw=%d in_window=%d quality=%d non_news_filtered=%d forecast_filtered=%d relevance_pass=%d subject_gate_pass=%d subject_gate_rejected=%d deduplicated_candidates=%d verification_candidates=%d rejection_reasons=%s", symbol, query, raw_candidates, in_window, quality_candidates, non_news_filtered, forecast_filtered, len(relevant_candidates), len(candidates), len(gate_results) - len(candidates), deduped_count, min(len(candidates), 2), rejection_reasons)
            if not candidates:
                reason = "TARGET_SUBJECT_MISMATCH" if gate_results and not candidates else "CACHE_HIT_NO_CANDIDATES" if marketaux_cache_hits and not marketaux_requests else "NO_PROVIDER_CANDIDATES"
                self.logger.info("[NEWS COVERAGE] mover_end symbol=%s verified_added=0 skipped_reason=%s elapsed=%.3f", symbol, reason, time.monotonic() - mover_started)
                continue
            self.logger.info("[NEWS COVERAGE] mover=%s verification_attempts=%d", symbol, min(len(ranked), 2))
            first_confirmed = False
            for rank, ranked_item in enumerate(ranked[:2], start=1):
                remaining_before_verify = min(escalation_deadline - self.performance.clock(), self.performance.budget_seconds - self.performance.elapsed)
                candidate = ranked_item["candidate"]
                if rank == 2:
                    if first_confirmed or self.performance.exhausted or remaining_before_verify <= 0 or _verified_story_count(verified) >= self.budget.coverage_target:
                        if remaining_before_verify <= 0:
                            self.budget.second_candidates_skipped_budget += 1
                            self.budget.coverage_deadline_exhausted = True
                            self.logger.info("[NEWS COVERAGE] SECOND_CANDIDATE_SKIPPED_BUDGET symbol=%s", symbol)
                        break
                    self.budget.targeted_second_candidates_attempted += 1
                self.logger.info("[NEWS COVERAGE] mover=%s remaining_budget_before_verify_rank%d=%.3f", symbol, rank, remaining_before_verify)
                if remaining_before_verify <= 0:
                    self.budget.coverage_requests_skipped_budget += 1
                    self.budget.coverage_deadline_exhausted = True
                    break
                schema_error = _targeted_schema_error(candidate.as_dict())
                if schema_error:
                    self.budget.targeted_verification_schema_failures += 1
                    self.budget.targeted_verification_no_source_attempt += 1
                    self.logger.warning("[NEWS COVERAGE] targeted verification schema failure symbol=%s rank=%d reason=TARGETED_CANDIDATE_SCHEMA_INVALID details=%s", symbol, rank, schema_error)
                    continue
                self.budget.targeted_verification_requests += 1
                self.budget.targeted_candidates_to_verification += 1
                before_verification = self.verification_acquirer.request_count
                before_verification_cache = self.verification_acquirer.cache_hits
                acquired = self.verification_acquirer.acquire([candidate.as_dict()])
                verification_cache_hits = self.verification_acquirer.cache_hits - before_verification_cache
                self.budget.targeted_verification_cache_hits += verification_cache_hits
                self.budget.targeted_cache_hits += verification_cache_hits
                evidence, acquisition_diagnostics = acquired.get(candidate.title, ([], None))
                result, verification_diagnostics = verify_candidate(candidate.as_dict(), evidence)
                attempts = _normalise_acquisition_attempts(acquisition_diagnostics)
                verification_diagnostics.attempted_sources = attempts
                self.budget.targeted_verification_sources_attempted += len(attempts)
                self.budget.targeted_official_requests += sum(1 for item in attempts if item["type"].startswith(("OFFICIAL", "REGULATOR_OFFICIAL", "SEC_EDGAR")))
                self.budget.targeted_tier2_requests += sum(1 for item in attempts if item["type"].startswith("TIER2"))
                if not attempts:
                    self.budget.targeted_verification_no_source_attempt += 1
                if verification_diagnostics.matched_source_type:
                    for attempt in attempts:
                        if attempt["type"].startswith(verification_diagnostics.matched_source_type):
                            attempt["status"] = "MATCH"
                elif any(attempt["type"].startswith(("SEC_EDGAR", "OFFICIAL_IR", "OFFICIAL_REGULATOR", "REGULATOR_OFFICIAL")) for attempt in attempts):
                    verification_diagnostics.reason = "official_path_checked_no_match"
                elif any(attempt["type"] == "TIER2_SEARCH" for attempt in attempts):
                    verification_diagnostics.reason = "tier2_checked_no_match"
                self.logger.info("[NEWS COVERAGE] targeted verification: symbol=%s headline=%s acquisition_started=%s attempted_sources=%s official_match=%s tier2_match=%s status=%s reason=%s", symbol, candidate.title, bool(acquisition_diagnostics is not None), attempts, verification_diagnostics.matched_source_type == "OFFICIAL", verification_diagnostics.matched_source_type == "TIER2", result.get("verification"), verification_diagnostics.reason)
                self.budget.verification_candidates += 1
                self.budget.verification_diagnostics.append({"title": candidate.title, **verification_diagnostics.as_dict()})
                if verification_diagnostics.matched_source_type == "OFFICIAL":
                    self.budget.verification_official_matches += 1
                elif verification_diagnostics.matched_source_type == "TIER2":
                    self.budget.verification_tier2_matches += 1
                if result.get("verification") == "VERIFIED":
                    self.budget.verification_confirmed += 1
                    if result.get("editorial_eligible") is True:
                        verified.append(result)
                        first_confirmed = rank == 1
                        if rank == 1:
                            self.budget.targeted_verified_from_rank1 += 1
                        else:
                            self.budget.targeted_verified_from_rank2 += 1
                        self.logger.info("[NEWS COVERAGE] mover=%s verified_added=1", symbol)
            self.logger.info("[NEWS COVERAGE] mover_end symbol=%s verified_added=%d elapsed=%.3f", symbol, _verified_story_count(verified) - self.budget.verified_stories_before_escalation, self.performance.clock() - mover_started)
        elapsed = self.performance.clock() - started
        self.budget.coverage_deadline_exhausted = self.budget.coverage_deadline_exhausted or elapsed >= 20.0
        for client in (self.marketaux.request_get, self.verification_acquirer.request_get):
            if isinstance(client, ProviderRequest):
                client.set_deadline(None)
        self.budget.coverage_escalation_seconds = max(0.001, round(elapsed, 3))
        self.logger.info("[NEWS COVERAGE] movers_targeted=%d", self.budget.targeted_movers_queried)
        self.logger.info("[NEWS COVERAGE] verified_after=%d", _verified_story_count(verified))
        self.logger.info("[NEWS COVERAGE] escalation_seconds=%.3f", self.budget.coverage_escalation_seconds)

    def _populate_marketaux_timeout_metrics(self) -> None:
        """Classify Marketaux timeout diagnostics without changing breaker policy."""
        general = [item for item in self.marketaux.query_diagnostics if item.get("query_type") != "TARGETED_MOVER"]
        targeted = [item for item in self.marketaux.query_diagnostics if item.get("query_type") == "TARGETED_MOVER"]
        is_timeout = lambda item: str(item.get("error_code", "")).upper() in {"MARKETAUX_TIMEOUT", "READTIMEOUT", "CONNECTTIMEOUT"}
        self.budget.general_marketaux_timeouts = sum(1 for item in general if is_timeout(item))
        self.budget.targeted_marketaux_timeouts = sum(1 for item in targeted if is_timeout(item))
        self.budget.general_marketaux_timeout_seconds_total = round(sum(float(item.get("timeout_seconds", 0) or 0) for item in general if is_timeout(item)), 3)

    @staticmethod
    def default_queries(symbols: list[str] | None = None) -> list[dict[str, Any]]:
        return [
            {"query_type": "US_MARKET_SESSION", "query": '"S&P 500"|"Nasdaq"|"Wall Street"|"US stocks"'},
            {"query_type": "RATES_FED", "query": '"Federal Reserve"|"Treasury yields"|"interest rates"'},
            {"query_type": "AI_SEMICONDUCTORS", "query": "AI|semiconductors|chips", "symbols": ["NVDA", "AMD", "AVGO"]},
            {"query_type": "TOP_MOVERS", "symbols": (symbols or [])[:8]},
            {"query_type": "CORPORATE_EVENTS", "query": "earnings|guidance|acquisition|merger|partnership|approved|filed|launched"},
            {"query_type": "MACRO_COMMODITIES", "query": "oil|gold|dollar|inflation|jobs"},
        ]

    @staticmethod
    def normalize_query_plan(queries: list[Any]) -> list[dict[str, str]]:
        normalized = []
        for item in queries:
            if isinstance(item, dict):
                query = str(item.get("query", ""))
                symbols = [str(value).upper() for value in item.get("symbols", []) if str(value).strip()]
                if query.strip() or symbols:
                    normalized.append({"query_type": str(item.get("query_type", "GENERAL")), "query": query, "symbols": symbols})
            elif str(item).strip():
                normalized.append({"query_type": "GENERAL", "query": str(item), "symbols": []})
        return normalized


def verify_candidates(candidates: list[NewsCandidate]) -> list[dict[str, Any]]:
    """Publish official Tier-1 events; leave discovery-only candidates unconfirmed."""
    grouped = cluster_stories([candidate.as_dict() for candidate in candidates])
    output: list[dict[str, Any]] = []
    for item in grouped:
        official = bool(item.get("official")) and int(item.get("source_tier", 4)) == 1
        item["verification"] = "VERIFIED" if official else "NOT_CONFIRMED"
        output.append(item)
    return output


_INCIDENTAL_MOVER_TERMS = {
    "shoplifting", "theft", "stolen", "store", "stores", "football", "soccer",
    "sponsorship", "sponsor", "concert", "festival", "arena", "stadium",
}
_CORPORATE_EVENT_TERMS = {
    "lawsuit", "litigation", "investigation", "regulator", "regulatory", "earnings",
    "revenue", "guidance", "acquisition", "merger", "partnership", "contract",
    "financing", "bankruptcy", "restructuring", "dividend", "filing", "approved",
}


def _is_relevant_mover_candidate(candidate: Any, mover_symbols: list[str] | None, company_names: list[str] | None = None) -> bool:
    """Drop obvious incidental mover mentions before spending verification requests."""
    if not mover_symbols or getattr(candidate, "discovery_source", "official") == "official":
        return True
    text = " ".join(str(getattr(candidate, field, "") or "") for field in ("title", "summary")).lower()
    affected = {str(value).upper() for value in (getattr(candidate, "affected_symbols", []) or [])}
    title_tokens = set(text.replace("/", " ").replace("-", " ").split())
    mover_match = bool(affected.intersection(str(symbol).upper() for symbol in mover_symbols)) or any(str(symbol).lower() in title_tokens for symbol in mover_symbols)
    if not mover_match and company_names:
        title_lower = str(getattr(candidate, "title", "") or "").lower()
        mover_match = any(_company_name_in_title(name, title_lower) for name in company_names if name)
    if not mover_match:
        return True
    incidental = any(term in text for term in _INCIDENTAL_MOVER_TERMS)
    corporate = any(term in text for term in _CORPORATE_EVENT_TERMS)
    return not incidental or corporate


def _company_name_in_title(company_name: str, title: str) -> bool:
    """Match a full company name in the title without accepting a fragment."""
    normalized = " ".join(str(company_name).lower().replace(",", " ").split())
    for suffix in (" incorporated", " inc.", " inc", " corporation", " corp.", " corp", " company", " co.", " co"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)].rstrip()
            break
    if not normalized:
        return False
    if normalized in title:
        return True
    # Curated mover names often include a legal suffix or a multi-word name;
    # the first distinctive brand token is an accepted public alias.
    alias = normalized.split()[0]
    return len(alias) >= 4 and re.search(rf"\b{re.escape(alias)}\b", title) is not None


def _deduplicate_news_candidates(candidates: list[NewsCandidate]) -> list[NewsCandidate]:
    """Deduplicate normalized NewsCandidate objects without DiscoveryCandidate aliases."""
    seen: set[tuple[str, str, str]] = set()
    output: list[NewsCandidate] = []
    for candidate in candidates:
        key = (
            str(getattr(candidate, "source_url", "") or "").rstrip("/").lower(),
            " ".join(str(candidate.title).lower().split()),
            str(candidate.published_at or "")[:16],
        )
        if key not in seen:
            seen.add(key)
            output.append(candidate)
    return output


def _normalise_acquisition_attempts(acquisition_diagnostics: Any) -> list[dict[str, str]]:
    """Preserve source-attempt diagnostics from the shared acquisition path."""
    if acquisition_diagnostics is None:
        return []
    raw_attempts = getattr(acquisition_diagnostics, "attempted_sources", []) or []
    return [
        attempt if isinstance(attempt, dict) else {"type": str(attempt), "status": "CHECKED"}
        for attempt in raw_attempts
    ]


def _targeted_schema_error(candidate: dict[str, Any]) -> str | None:
    """Return an explicit reason when targeted handoff lacks verifier fields."""
    required = {
        "title": candidate.get("title"),
        "source_url": candidate.get("source_url"),
        "published_at": candidate.get("published_at"),
        "affected_symbols": candidate.get("affected_symbols"),
    }
    missing = [name for name, value in required.items() if not value or (name == "affected_symbols" and not isinstance(value, list))]
    return f"missing={','.join(missing)}" if missing else None


_TARGETED_EVENT_TERMS: tuple[tuple[str, tuple[str, ...], int], ...] = (
    ("GUIDANCE_CHANGE", ("guidance", "raises guidance", "cuts guidance", "outlook"), 48),
    ("EARNINGS_RESULTS", ("earnings", "revenue", "profit", "quarterly results", "financial results"), 48),
    ("M_AND_A", ("acquisition", "acquire", "merger", "takeover"), 46),
    ("MAJOR_CONTRACT", ("major contract", "order", "agreement", "contract", "awarded"), 42),
    ("REGULATORY", ("regulator", "regulatory", "approved", "approval", "lawsuit", "settlement"), 42),
    ("EXECUTIVE_CHANGE", ("ceo", "chief executive", "appoints", "resigns", "executive"), 38),
    ("FINANCING", ("financing", "capital raise", "funding", "restructuring", "layoffs"), 38),
    ("PARTNERSHIP", ("partnership", "strategic announcement", "investor day"), 32),
    ("OPERATIONAL_UPDATE", ("deliveries", "production", "operational", "software issue", "launches", "conference"), 25),
)
MIN_TARGETED_SUBJECT_RELEVANCE = 30
_TARGETED_ANALYSIS_TERMS = ("a look at", "is a buy", "how much", "what investors should know", "market reaction expected", "valuation", "gf value", "could", "why ", "forecast", "prediction", "price target", "outlook", "is showing signs")


def _targeted_subject_relevance(candidate: NewsCandidate, symbol: str, company_names: list[str]) -> tuple[int, str]:
    """Score whether the mover is the story subject, not merely an entity tag."""
    title = str(candidate.title or "")
    title_lower = title.lower()
    summary_lower = str(candidate.summary or "").lower()
    symbol_token = symbol.lower()
    title_tokens = {token.strip(".,:;()[]") for token in title.replace("/", " ").split()}
    if symbol.upper() in title_tokens:
        return 48, "TICKER_IN_HEADLINE"
    if any(_company_name_in_title(name, title_lower) for name in company_names if name):
        return 48, "COMPANY_IN_HEADLINE"
    normalized_names = [" ".join(name.lower().replace(",", " ").split()) for name in company_names if name]
    if any(name and name in summary_lower for name in normalized_names):
        return 32, "COMPANY_IN_DESCRIPTION"
    return 0, "TARGET_SUBJECT_MISMATCH"


def _rank_targeted_candidates(candidates: list[NewsCandidate], symbol: str, company_names: list[str]) -> list[dict[str, Any]]:
    """Rank targeted candidates by subject relevance and event verifiability."""
    ranked: list[dict[str, Any]] = []
    symbol_lower = symbol.lower()
    names = [" ".join(name.lower().replace(",", " ").split()) for name in company_names if name]
    for candidate in candidates:
        title = str(candidate.title or "")
        text = f"{title} {candidate.summary or ''}".lower()
        title_signal_text = title.lower()
        title_lower = title.lower()
        company_in_title = any(_company_name_in_title(name, title_lower) for name in names)
        ticker_in_title = symbol.upper() in {token.strip(".,:;()[]") for token in title.replace("/", " ").split()}
        entity_match = symbol.upper() in {str(value).upper() for value in (candidate.affected_symbols or [])}
        subject_score, _ = _targeted_subject_relevance(candidate, symbol, company_names)
        event_type, event_score = "OTHER", 8
        for name, terms, score in _TARGETED_EVENT_TERMS:
            if any(term in title_signal_text for term in terms):
                event_type, event_score = name, score
                break
        analysis_penalty = 24 if any(term in title_signal_text for term in _TARGETED_ANALYSIS_TERMS) else 0
        broad_subject_penalty = 22 if entity_match and not company_in_title and not ticker_in_title else 0
        subject_score = max(0, subject_score - broad_subject_penalty)
        source_score = 6 if int(getattr(candidate, "source_tier", 4) or 4) <= 2 else 3
        verification_likelihood = source_score + (4 if event_score >= 32 else 0) - analysis_penalty // 4
        selection_score = subject_score + event_score + verification_likelihood - analysis_penalty
        ranked.append({
            "candidate": candidate,
            "headline": title,
            "event_type": event_type,
            "subject_relevance_score": subject_score,
            "event_signal_score": event_score,
            "verification_likelihood_score": verification_likelihood,
            "candidate_selection_score": selection_score,
            "analysis_penalty": analysis_penalty,
            "subject_mismatch_penalty": broad_subject_penalty,
        })
    ranked.sort(key=lambda item: item["candidate_selection_score"], reverse=True)
    for rank, item in enumerate(ranked, start=1):
        item["rank"] = rank
        item["selected_for_verification"] = rank <= 2
    return ranked


def _verified_story_count(stories: list[dict[str, Any]]) -> int:
    """Count only verified, editorially eligible stories for coverage."""
    return sum(1 for item in stories if item.get("verification") == "VERIFIED" and item.get("editorial_eligible") is True)
