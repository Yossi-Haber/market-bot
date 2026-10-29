"""Production daily-run orchestrator for the Market Bot pipeline."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

from app import BASE_DIR, OUTPUT_DIR, configure_logging, main as collect_market_data
from config import load_llm_settings
from delivery.telegram import run_delivery
from drivers.runtime import run_market_driver_pipeline
from llm.narrative import run_narrative
from news.live import LiveNewsCoverage
from news.catalyst import write_live_news_catalyst_snapshot
from news.verification_v2 import public_story
from render.markdown import render_from_file
from report.composer import compose_daily_report
from visual.builder import load_json
from generate_visual_report_v3 import main as generate_visual_v3

STATUS_PATH = OUTPUT_DIR / "daily_run_status.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the production and safe-QA run modes."""
    parser = argparse.ArgumentParser(description="Run the full Market Bot daily pipeline.")
    parser.add_argument("--market-date", help="Expected market date in YYYY-MM-DD format.")
    parser.add_argument("--dry-run", action="store_true", help="Run all stages and skip Telegram delivery.")
    parser.add_argument("--skip-telegram", action="store_true", help="Run through validation without Telegram delivery.")
    parser.add_argument("--refresh-movers", action="store_true", help="Force fresh Twelve Data mover collection.")
    parser.add_argument("--disable-movers-prefilter", action="store_true", help="Force legacy full-universe Twelve Data mover collection.")
    parser.add_argument("--refresh-market", action="store_true", help="Force fresh market and macro collection.")
    parser.add_argument("--refresh-marketaux", action="store_true", help="Bypass the Marketaux cache for this run.")
    parser.add_argument("--refresh-verification", action="store_true", help="Bypass the Verification V2 cache for this run.")
    parser.add_argument("--v2-evidence-output", help="Opt-in evidence capture directory; report-only mode and disabled LLM required.")
    return parser.parse_args(argv)


def write_status(payload: dict[str, Any]) -> None:
    """Persist safe run state without credentials or raw provider responses."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _market_date() -> str:
    snapshot = load_json(OUTPUT_DIR / "market_snapshot.json")
    value = snapshot.get("market_date")
    if not value:
        raise RuntimeError("MARKET_DATE_MISSING")
    return str(value)


def _market_date_resolution() -> dict[str, Any]:
    snapshot = load_json(OUTPUT_DIR / "market_snapshot.json")
    value = snapshot.get("market_date_resolution")
    if not isinstance(value, dict):
        raise RuntimeError("MARKET_DATE_RESOLUTION_MISSING")
    return value


def _cache_summary() -> dict[str, Any]:
    """Read safe cache counters produced by the market and movers stages."""
    summary: dict[str, Any] = {
        "market_snapshot": "MISS",
        "movers_snapshot": "MISS",
        "fresh_market_symbols": 0,
        "cached_market_symbols": 0,
        "fresh_mover_symbols": 0,
        "cached_mover_symbols": 0,
        "movers_prefilter_mode": "unknown",
        "movers_discovery_runtime_seconds": 0,
        "movers_discovery_coverage": 0,
        "movers_candidate_pool_size": 0,
        "movers_twelve_data_symbols_requested": 0,
        "movers_prefilter_fallback_used": False,
        "movers_prefilter_fallback_reason": None,
        "news_performance": {},
        "catalyst_performance": {},
    }
    try:
        market = load_json(OUTPUT_DIR / "market_snapshot.json")
        market_metadata = market.get("cache_metadata", {})
        summary["market_snapshot"] = str(market_metadata.get("cache_status", "MISS"))
        twelve_data = market.get("providers", {}).get("Twelve Data", {})
        if summary["market_snapshot"] == "HIT":
            summary["cached_market_symbols"] = int(market_metadata.get("symbols_requested", 0))
        else:
            summary["fresh_market_symbols"] = int(twelve_data.get("requested", twelve_data.get("successful", 0)))
    except (OSError, ValueError, TypeError):
        pass
    try:
        movers = load_json(OUTPUT_DIR / "movers_snapshot.json")
        summary["movers_snapshot"] = str(movers.get("cache_status", "MISS"))
        prefilter = movers.get("prefilter", {})
        if isinstance(prefilter, dict):
            summary["movers_prefilter_mode"] = str(prefilter.get("mode", "unknown"))
            summary["movers_discovery_runtime_seconds"] = prefilter.get("discovery_runtime_seconds", 0)
            summary["movers_discovery_coverage"] = prefilter.get("discovered_symbols", 0)
            summary["movers_candidate_pool_size"] = prefilter.get("candidate_pool_size", 0)
            summary["movers_twelve_data_symbols_requested"] = prefilter.get("twelve_data_symbols_requested", movers.get("symbols_requested", 0))
            summary["movers_prefilter_fallback_used"] = prefilter.get("fallback_used", False)
            summary["movers_prefilter_fallback_reason"] = prefilter.get("fallback_reason")
        if summary["movers_snapshot"] == "HIT":
            summary["cached_mover_symbols"] = int(movers.get("symbols_requested", 0))
        else:
            summary["fresh_mover_symbols"] = int(movers.get("symbols_requested", 0))
    except (OSError, ValueError, TypeError):
        pass
    try:
        live_news = load_json(OUTPUT_DIR / "live_news_v3_snapshot.json")
        request_budget = live_news.get("request_budget", {})
        if isinstance(request_budget, dict):
            summary["news_performance"] = {
                key: request_budget.get(key)
                for key in (
                    "official_discovery_seconds", "marketaux_seconds", "slow_fallback_seconds", "verification_seconds",
                    "total_news_seconds", "official_requests", "marketaux_requests", "slow_fallback_requests",
                    "verification_requests", "timeouts", "circuit_breakers_opened", "early_stop", "budget_exhausted",
                    "cache_mode", "official_cache_hit", "marketaux_cache_hit", "verification_cache_hit", "live_requests_total",
                    "marketaux_enabled", "marketaux_disabled_reason", "marketaux_requests_attempted", "marketaux_requests_succeeded",
                    "marketaux_usage_limit_reached", "marketaux_requests_skipped_usage_limit", "targeted_marketaux_skipped_usage_limit",
                    "slow_fallback_movers_attempted", "slow_fallback_provider_requests", "candidates_before_relevance_filter",
                    "candidates_after_relevance_filter", "irrelevant_candidates_filtered",
                    "coverage_target", "verified_stories_before_escalation", "verified_stories_after_escalation",
                    "targeted_movers_considered", "targeted_movers_queried", "targeted_official_requests",
                    "targeted_marketaux_requests", "targeted_verification_requests", "coverage_escalation_seconds",
                    "targeted_cache_hits", "targeted_cache_misses", "coverage_escalation_started",
                    "coverage_escalation_skipped_reason", "coverage_target_met", "news_coverage_status",
                    "targeted_marketaux_http_success", "targeted_marketaux_api_errors",
                    "targeted_marketaux_articles_returned", "targeted_entity_matches", "targeted_entity_missing",
                    "targeted_candidates_to_verification",
                    "targeted_tier2_requests", "targeted_verification_sources_attempted",
                    "targeted_verification_no_source_attempt", "targeted_verification_cache_hits",
                    "targeted_candidates_ranked", "targeted_candidates_selected", "targeted_second_candidates_attempted",
                    "targeted_analysis_candidates_penalized", "targeted_subject_mismatch_penalized",
                    "targeted_verified_from_rank1", "targeted_verified_from_rank2",
                    "targeted_subject_gate_passed", "targeted_subject_gate_rejected",
                    "targeted_event_driven_candidates", "targeted_commentary_candidates",
                    "coverage_deadline_exhausted", "coverage_requests_skipped_budget", "second_candidates_skipped_budget",
                    "general_marketaux_timeouts", "general_marketaux_timeout_seconds_total", "targeted_marketaux_timeouts",
                    "subject_gate_validation_passed",
                    "fmp_news_requests", "fmp_press_release_requests", "fmp_news_candidates", "fmp_press_release_candidates",
                    "fmp_evidence_matches", "fmp_no_match", "fmp_errors", "fmp_fallback_activated", "fmp_plan_restriction", "fmp_requests_skipped_plan_restriction",
                )
                if key in request_budget
            }
    except (OSError, ValueError, TypeError):
        pass
    try:
        catalysts = load_json(OUTPUT_DIR / "catalysts_snapshot.json")
        stats = catalysts.get("request_statistics", {})
        if isinstance(stats, dict):
            summary["catalyst_performance"] = {
                key: stats.get(key)
                for key in ("live_news_seconds", "catalyst_mapping_seconds", "catalyst_gap_fill_seconds", "duplicate_requests_avoided", "catalyst_network_requests", "runtime_seconds", "total_http_requests", "slow_fallbacks_used", "slow_fallback_cap")
                if key in stats
            }
    except (OSError, ValueError, TypeError):
        pass
    return summary


def run_catalyst_mapping_stage(logger: logging.Logger) -> Path:
    """Build catalysts from the already completed Live News V3 snapshot."""
    return write_live_news_catalyst_snapshot(
        OUTPUT_DIR / "movers_snapshot.json",
        OUTPUT_DIR / "live_news_v3_snapshot.json",
        OUTPUT_DIR,
        logger,
    )


def run_live_news_stage(market_date: str, *, refresh_marketaux: bool, refresh_verification: bool, logger: logging.Logger) -> Path:
    """Run LIVE NEWS V3 and Verification V2 using their existing bounded clients."""
    movers = load_json(OUTPUT_DIR / "movers_snapshot.json")
    mover_details = [
        item
        for group in ("top_gainers", "top_losers")
        for item in movers.get(group, [])
        if isinstance(item, dict) and item.get("symbol")
    ]
    mover_symbols = list(dict.fromkeys([
        str(item.get("symbol", "")).upper()
        for group in ("top_gainers", "top_losers")
        for item in movers.get(group, [])
        if item.get("symbol")
    ]))
    stories, budget = LiveNewsCoverage(
        logger,
        market_date,
        refresh_marketaux=refresh_marketaux,
        perform_verification=True,
        refresh_verification=refresh_verification,
    ).collect(symbols=[], mover_symbols=mover_symbols, mover_details=mover_details)
    public_budget = dict(budget)
    public_budget.pop("verification_diagnostics", None)
    path = OUTPUT_DIR / "live_news_v3_snapshot.json"
    path.write_text(
        json.dumps(
            {"market_date": market_date, "generated_at": datetime.now(timezone.utc).isoformat(), "request_budget": public_budget, "stories": [public_story(item) for item in stories]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    logger.info("LIVE NEWS V3 snapshot created: %s", path)
    return path


def run_market_driver_stage(market_date: str, *, refresh_verification: bool, logger: logging.Logger) -> Path:
    """Run the symbol-scoped Market Drivers production stage."""
    return run_market_driver_pipeline(market_date, OUTPUT_DIR, refresh_verification=refresh_verification, logger=logger)


def _require_file(path: Path, label: str) -> Path:
    if not path.is_file():
        raise RuntimeError(f"{label}_MISSING")
    return path


def validate_send_gate() -> dict[str, Any]:
    """Validate the final report, selected narrative, and V3 artifacts before sending."""
    report = load_json(_require_file(OUTPUT_DIR / "daily_report_data.json", "REPORT"))
    readiness = report.get("publication_readiness", {}).get("status")
    if readiness not in {"READY", "READY_WITH_WARNINGS"}:
        raise RuntimeError("PUBLICATION_NOT_READY")
    selection = load_json(_require_file(OUTPUT_DIR / "daily_report_selection.json", "NARRATIVE_SELECTION"))
    if selection.get("selected_source") not in {"LLM", "DETERMINISTIC"}:
        raise RuntimeError("NARRATIVE_NOT_SELECTED")
    if selection.get("selected_source") == "LLM" and selection.get("guardrails_valid") is not True:
        raise RuntimeError("LLM_GUARDRAILS_FAILED")
    _require_file(OUTPUT_DIR / "daily_report_final.md", "FINAL_NARRATIVE")
    html_path = _require_file(OUTPUT_DIR / "daily_report_card_v3.html", "V3_HTML")
    png_path = _require_file(OUTPUT_DIR / "daily_report_card_v3.png", "V3_PNG")
    validation_path = _require_file(OUTPUT_DIR / "daily_report_v3_visual_validation.json", "V3_VALIDATION")
    validation = load_json(validation_path)
    if validation.get("valid") is not True:
        raise RuntimeError("VISUAL_VALIDATION_FAILED")
    from visual.validate import png_metadata
    metadata = png_metadata(png_path)
    if (metadata.get("width"), metadata.get("height")) != (1080, 1600):
        raise RuntimeError("V3_PNG_WRONG_DIMENSIONS")
    return {"report": report, "selection": selection, "html": html_path, "png": png_path, "validation": validation}


def _step(logger: logging.Logger, number: int, label: str, action: Callable[[], Any]) -> Any:
    logger.info("[%d/15] %s", number, label)
    return action()


def run(argv: list[str] | None = None) -> int:
    """Run all production stages and return an automation-friendly exit code."""
    args = parse_args(argv)
    started = time.monotonic()
    logger = configure_logging()
    load_dotenv(BASE_DIR / ".env")
    logger.info("Daily production run started")
    requested_date = args.market_date
    stage = "PREFLIGHT"
    evidence_capture = None
    try:
        capture_output = getattr(args, "v2_evidence_output", None) or os.getenv("MARKETBOT_V2_EVIDENCE_OUTPUT")
        if capture_output and (not (args.dry_run or args.skip_telegram) or load_llm_settings()["enable_llm"]):
            raise PreflightFailure("V2_CAPTURE_REQUIRES_REPORT_ONLY_AND_LLM_DISABLED")
        run_preflight(args, logger)
        stage = "MARKET_DATA"
        collection_code = _step(logger, 1, "Market data", lambda: collect_market_data(market_date_override=requested_date, refresh_movers=args.refresh_movers, refresh_market=args.refresh_market, movers_prefilter_enabled=not args.disable_movers_prefilter))
        if collection_code:
            raise RuntimeError("MARKET_COLLECTION_FAILED")
        market_date = requested_date or _market_date()
        if requested_date and requested_date != _market_date():
            raise RuntimeError("MARKET_DATE_MISMATCH")
        market_date_resolution = {"source": "EXPLICIT", "agreement": "explicit"} if requested_date else _market_date_resolution()
        if capture_output:
            from research_v2.capture import EvidenceCapture
            evidence_capture = EvidenceCapture(market_date, capture_output)
            evidence_capture.manifest["capture"]["report_pipeline_completed"] = False
            evidence_capture.__enter__()
        logger.info("Canonical market date resolved: %s", market_date)
        logger.info("Resolution source: %s", market_date_resolution.get("source"))
        logger.info("Core agreement: %s", market_date_resolution.get("agreement"))
        cache_summary = _cache_summary()
        logger.info("Fresh Twelve Data symbols requested: %d", cache_summary["fresh_market_symbols"])
        logger.info("Cached Twelve Data symbols reused: %d", cache_summary["cached_market_symbols"])

        stage = "MACRO"
        _step(logger, 2, "Macro", lambda: _require_file(OUTPUT_DIR / "market_snapshot.json", "MACRO_SNAPSHOT"))
        stage = "MOVERS"
        _step(logger, 3, "Movers", lambda: _require_file(OUTPUT_DIR / "movers_snapshot.json", "MOVERS_SNAPSHOT"))
        stage = "MARKET_DRIVERS"
        _step(logger, 4, "Market Drivers", lambda: run_market_driver_stage(market_date, refresh_verification=args.refresh_verification, logger=logger))
        stage = "LIVE_NEWS"
        _step(logger, 5, "Live news V3", lambda: run_live_news_stage(market_date, refresh_marketaux=args.refresh_marketaux, refresh_verification=args.refresh_verification, logger=logger))
        stage = "VERIFICATION_V2"
        _step(logger, 6, "Live News verification artifact", lambda: _require_file(OUTPUT_DIR / "live_news_v3_snapshot.json", "VERIFICATION"))
        stage = "CATALYST_MAPPING"
        _step(logger, 7, "Catalyst mapping from Live News V3", lambda: run_catalyst_mapping_stage(logger))
        stage = "COMPOSE"
        _step(logger, 8, "Daily report composition", lambda: compose_daily_report(logger))
        stage = "DETERMINISTIC_REPORT"
        _step(logger, 9, "Deterministic report", lambda: render_from_file(logger=logger) or (_ for _ in ()).throw(RuntimeError("REPORT_RENDER_FAILED")))
        stage = "LLM_NARRATIVE"
        _step(logger, 10, "LLM narrative", lambda: run_narrative(logger=logger) or (_ for _ in ()).throw(RuntimeError("NARRATIVE_FAILED")))
        stage = "LLM_GUARDRAILS"
        _step(logger, 11, "LLM guardrails", lambda: _require_file(OUTPUT_DIR / "daily_report_selection.json", "LLM_SELECTION"))
        stage = "FINAL_NARRATIVE"
        _step(logger, 12, "Final narrative selection", lambda: _require_file(OUTPUT_DIR / "daily_report_final.md", "FINAL_NARRATIVE"))
        stage = "VISUAL_GENERATION"
        _step(logger, 13, "Visual V3 HTML", generate_visual_v3)
        stage = "VISUAL_PNG"
        _step(logger, 14, "Visual V3 PNG", lambda: _require_file(OUTPUT_DIR / "daily_report_card_v3.png", "V3_PNG"))
        stage = "VISUAL_VALIDATION"
        gate = _step(logger, 15, "Visual validation", validate_send_gate)
        if args.dry_run or args.skip_telegram:
            logger.info("[16/16] Telegram delivery skipped")
            telegram_status = "NOT_SENT"
            sent = False
            if args.dry_run:
                print("DAILY RUN READY — TELEGRAM NOT SENT (DRY RUN)")
        else:
            stage = "TELEGRAM_DELIVERY"
            durable_delivery = None
            if os.getenv("MARKETBOT_PERSISTENT_DELIVERY_MODE") == "1":
                hmac_key = os.getenv("TELEGRAM_RECIPIENT_ID_HMAC_KEY", "")
                if len(hmac_key.encode("utf-8")) < 32:
                    raise PreflightFailure("CLOUD_DELIVERY_STATE_OR_HMAC_KEY_MISSING")
                backend = os.getenv("MARKETBOT_STATE_BACKEND", "github").strip().lower()
                if backend == "d1":
                    from persistence.d1_client import D1DeliveryLedger, D1StateClient

                    durable_delivery = D1DeliveryLedger(D1StateClient.from_env(), hmac_key)
                elif backend == "github":
                    from persistence.delivery_ledger import DeliveryLedger
                    from persistence.github_state import GitHubStateStore

                    state_repo = os.getenv("MARKETBOT_STATE_REPO", "").strip()
                    if not state_repo:
                        raise PreflightFailure("CLOUD_STATE_REPOSITORY_REQUIRED")
                    durable_delivery = DeliveryLedger(GitHubStateStore(Path(state_repo)), hmac_key)
                else:
                    raise PreflightFailure("CLOUD_STATE_BACKEND_INVALID")
            delivery_code = _step(logger, 16, "Telegram delivery", lambda: run_delivery(output_dir=OUTPUT_DIR, durable_ledger=durable_delivery))
            delivery = load_json(OUTPUT_DIR / "daily_report_delivery.json") if (OUTPUT_DIR / "daily_report_delivery.json").is_file() else {}
            telegram_status = str(delivery.get("status", "FAILED"))
            sent = telegram_status == "SENT"
            if delivery_code:
                raise DeliveryFailure(telegram_status)
        cache_summary = _cache_summary()
        payload = {"status": "SUCCESS", "market_date": market_date, "market_date_resolution": market_date_resolution, "visual_version": "V3", "telegram_sent": sent, "telegram_status": telegram_status, "cache": cache_summary, "runtime_seconds": round(time.monotonic() - started, 3)}
        write_status(payload)
        if evidence_capture is not None:
            evidence_capture.manifest["capture"]["report_pipeline_completed"] = True
        logger.info("Daily run summary: market_date=%s narrative_source=%s verified_stories=%d visual=PASS telegram=%s movers_prefilter=%s discovery=%s/120 candidate_pool=%s twelve_data_symbols=%s fallback=%s runtime=%.3fs", market_date, gate["selection"].get("selected_source"), len(gate["report"].get("top_stories", [])), telegram_status, cache_summary["movers_prefilter_mode"], cache_summary["movers_discovery_coverage"], cache_summary["movers_candidate_pool_size"], cache_summary["movers_twelve_data_symbols_requested"], cache_summary["movers_prefilter_fallback_used"], time.monotonic() - started)
        print(f"Market date: {market_date}")
        print(f"Narrative source: {gate['selection'].get('selected_source')}")
        print(f"Verified stories: {len(gate['report'].get('top_stories', []))}")
        try:
            driver_snapshot = load_json(OUTPUT_DIR / "market_driver_snapshot.json")
            driver_metrics = driver_snapshot.get("metrics", {})
            analysis = driver_snapshot.get("driver_analysis", {})
            print("MARKET DRIVERS:")
            print(f"meaningful_moves={driver_metrics.get('meaningful_moves', 0)}")
            print(f"confirmed={driver_metrics.get('confirmed', 0)}")
            print(f"plausible={driver_metrics.get('plausible', 0)}")
            print(f"unexplained={driver_metrics.get('unexplained', 0)}")
            print(f"main_driver_status={(analysis.get('main_market_driver') or {}).get('verification_status')}")
            print("stock_drivers:")
            for item in analysis.get("stock_catalysts", []):
                story = next((story for story in driver_snapshot.get("candidate_stories", []) if set(story.get("affected_symbols", [])) & set(item.get("related_symbols", []))), {})
                print(f"{','.join(item.get('related_symbols', []))} move={item.get('strength')} event_type={story.get('event_type')} status={item.get('verification_status')} source={story.get('source_name')}")
            print("PROVIDERS:")
            for key in ("finnhub_live_requests", "finnhub_cache_hits", "marketaux_requests", "regulator_requests", "official_requests", "tier2_requests", "fmp_requests"):
                print(f"{key}={driver_metrics.get(key, 0)}")
            print("VERIFICATION:")
            print(f"candidates={driver_metrics.get('verification_candidates', 0)}")
            print(f"confirmed={driver_metrics.get('verification_confirmed', 0)}")
            print(f"not_confirmed={driver_metrics.get('verification_not_confirmed', 0)}")
        except (OSError, ValueError, TypeError):
            logger.warning("MARKET_DRIVER_SUMMARY_UNAVAILABLE")
        print("Visual status: PASS (V3)")
        print(f"Telegram status: {telegram_status}")
        print(f"Runtime: {time.monotonic() - started:.3f}s")
        return 0
    except PreflightFailure as exc:
        try:
            write_status({"status": "FAILED", "failed_stage": "PREFLIGHT", "telegram_sent": False, "error": str(exc)})
        except OSError:
            logger.exception("Unable to write preflight failure status")
        logger.error("PREFLIGHT FAILED — %s", exc)
        print(f"PREFLIGHT FAILED — {exc}")
        return 1
    except DeliveryFailure as exc:
        write_status({"status": "FAILED", "failed_stage": stage, "telegram_sent": False, "telegram_status": str(exc)})
        logger.error("Daily run failed at %s: %s", stage, exc)
        print(f"DAILY RUN FAILED — stage={stage} error={exc}")
        return 3
    except RuntimeError as exc:
        code = 2 if stage in {"VISUAL_VALIDATION", "FINAL_NARRATIVE"} or str(exc) in {"PUBLICATION_NOT_READY", "NARRATIVE_NOT_SELECTED", "LLM_GUARDRAILS_FAILED", "V3_PNG_WRONG_DIMENSIONS", "VISUAL_VALIDATION_FAILED"} else 1
        write_status({"status": "FAILED", "failed_stage": stage, "telegram_sent": False, "error": str(exc)})
        logger.error("Daily run failed at %s: %s", stage, exc)
        print(f"DAILY RUN FAILED — stage={stage} error={exc}")
        return code
    except Exception as exc:
        write_status({"status": "FAILED", "failed_stage": stage, "telegram_sent": False, "error": type(exc).__name__})
        logger.exception("Daily run failed at %s", stage)
        print(f"DAILY RUN FAILED — stage={stage} error={type(exc).__name__}")
        return 1
    finally:
        if evidence_capture is not None:
            try:
                evidence_capture.__exit__(None, None, None)
                evidence_capture.finish(OUTPUT_DIR / "daily_report_data.json")
            except Exception as exc:
                logger.warning("V2_CAPTURE_FINALIZATION_FAILED error_class=%s", type(exc).__name__)


class DeliveryFailure(RuntimeError):
    """Raised after all configured recipients were attempted but delivery was not complete."""


class PreflightFailure(RuntimeError):
    """Raised when a required local dependency or production configuration is unavailable."""


def run_preflight(args: argparse.Namespace, logger: logging.Logger) -> None:
    """Check local runtime and required configuration before any API collection."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PreflightFailure("PLAYWRIGHT_IMPORT_UNAVAILABLE") from exc
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:
        raise PreflightFailure("PLAYWRIGHT_CHROMIUM_UNAVAILABLE") from exc
    logger.info("[PREFLIGHT] Playwright Chromium: OK")

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        probe = OUTPUT_DIR / ".market_bot_preflight"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise PreflightFailure("OUTPUT_DIRECTORY_NOT_WRITABLE") from exc
    logger.info("[PREFLIGHT] Output directory: OK")

    if not os.getenv("TWELVE_DATA_API_KEY", "").strip() or os.getenv("TWELVE_DATA_API_KEY", "").strip() == "PASTE_API_KEY_HERE":
        raise PreflightFailure("TWELVE_DATA_API_KEY_MISSING")
    logger.info("[PREFLIGHT] Market API config: OK")

    llm_settings = load_llm_settings()
    if llm_settings["enable_llm"] and not os.getenv("OPENAI_API_KEY", "").strip():
        raise PreflightFailure("OPENAI_API_KEY_MISSING")
    logger.info("[PREFLIGHT] LLM config: OK")

    telegram_enabled = not args.dry_run and not args.skip_telegram and os.getenv("TELEGRAM_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
    if telegram_enabled:
        if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or not os.getenv("TELEGRAM_CHAT_IDS", "").strip():
            raise PreflightFailure("TELEGRAM_CONFIG_MISSING")
        if os.getenv("MARKETBOT_PERSISTENT_DELIVERY_MODE") == "1":
            if len(os.getenv("TELEGRAM_RECIPIENT_ID_HMAC_KEY", "").encode("utf-8")) < 32:
                raise PreflightFailure("TELEGRAM_RECIPIENT_HMAC_KEY_MISSING")
        if os.getenv("MARKETBOT_PERSISTENT_DELIVERY_MODE") == "1" or os.getenv("MARKETBOT_PERSISTENT_QUOTA_MODE") == "1":
            backend = os.getenv("MARKETBOT_STATE_BACKEND", "github").strip().lower()
            try:
                if backend == "d1":
                    from persistence.d1_client import D1StateClient
                    D1StateClient.from_env().read()
                elif backend == "github":
                    state_repo = os.getenv("MARKETBOT_STATE_REPO", "").strip()
                    if not state_repo:
                        raise PreflightFailure("CLOUD_STATE_REPOSITORY_REQUIRED")
                    from persistence.github_state import GitHubStateStore, StateUnavailable
                    GitHubStateStore(Path(state_repo)).read(fetch=False)
                else:
                    raise PreflightFailure("CLOUD_STATE_BACKEND_INVALID")
            except PreflightFailure:
                raise
            except Exception as exc:
                raise PreflightFailure(str(exc) if type(exc).__name__ in {"StateUnavailable", "D1StateError"} else "CLOUD_STATE_UNAVAILABLE") from None
    logger.info("[PREFLIGHT] Telegram config: %s", "OK" if telegram_enabled else "SKIPPED")


if __name__ == "__main__":
    sys.exit(run())
