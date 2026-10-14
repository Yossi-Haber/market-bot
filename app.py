"""Entry point for the daily US market snapshot bot."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from collectors.cboe import CboeClient
from collectors.fred import FredClient
from collectors.market import TwelveDataClient
from collectors.rate_limit import CreditRateLimitController
from collectors.yahoo import YahooClient
from config import MARKET_SYMBOLS
from movers import MoversCollectionFailure, run_movers_snapshot
from news.catalyst import run_catalyst_snapshot
from validation.validate import validate_fred_result, validate_macro_ohlc_result, validate_market_result
from market_date import MarketDateResolutionError, resolve_market_date, select_as_of_bars


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "market_bot.log"
MARKET_CACHE_SCHEMA_VERSION = 1
MARKET_COLLECTOR_VERSION = "market-v1"


def configure_logging() -> logging.Logger:
    """Configure file and console logging without exposing credentials."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("market_bot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


def write_snapshot(snapshot: dict[str, Any], generated_at: datetime) -> tuple[Path, Path]:
    """Write the current snapshot and a timestamped historical copy."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    current_path = OUTPUT_DIR / "market_snapshot.json"
    historical_path = OUTPUT_DIR / f"market_snapshot_{generated_at.strftime('%Y%m%d_%H%M%S')}.json"
    current_path.write_text(text, encoding="utf-8")
    historical_path.write_text(text, encoding="utf-8")
    return current_path, historical_path


def build_market_cache_metadata(
    market_date: str,
    resolution: dict[str, Any],
    all_results: list[dict[str, Any]],
    *,
    cache_status: str,
) -> dict[str, Any]:
    """Build the single metadata contract used by cache writes and reads."""
    validation_passed = all(result.get("verified") is True and result.get("validation_passed") is True for result in all_results)
    return {
        "canonical_market_date": market_date,
        "schema_version": MARKET_CACHE_SCHEMA_VERSION,
        "collector_version": MARKET_COLLECTOR_VERSION,
        "collection_status": "SUCCESS" if validation_passed else "FAILED",
        "validation_status": "PASSED" if validation_passed else "FAILED",
        "validation_passed": validation_passed,
        "required_symbols": [result.get("symbol") for result in all_results],
        "symbols_requested": len(all_results),
        "market_date_resolution": dict(resolution),
        "cache_status": cache_status,
    }


def validate_market_snapshot_cache(snapshot: Any, market_date: str, *, require_historical_integrity: bool = False) -> dict[str, Any]:
    """Return cache validity and safe, actionable rejection reasons."""
    reasons: list[str] = []
    if not isinstance(snapshot, dict):
        return {"valid": False, "reasons": ["INVALID_JSON_STRUCTURE"]}
    if snapshot.get("market_date") != market_date:
        reasons.append("CANONICAL_MARKET_DATE_MISMATCH")
    metadata = snapshot.get("cache_metadata")
    if not isinstance(metadata, dict):
        reasons.append("MISSING_CACHE_METADATA")
        return {"valid": False, "reasons": reasons}
    if metadata.get("canonical_market_date") != market_date:
        reasons.append("CACHE_METADATA_DATE_MISMATCH")
    if metadata.get("schema_version") != MARKET_CACHE_SCHEMA_VERSION:
        reasons.append("SCHEMA_VERSION_MISMATCH")
    if metadata.get("collector_version") != MARKET_COLLECTOR_VERSION:
        reasons.append("COLLECTOR_VERSION_MISMATCH")
    if metadata.get("collection_status") != "SUCCESS":
        reasons.append("COLLECTION_STATUS_NOT_SUCCESS")
    if metadata.get("validation_status") != "PASSED" or metadata.get("validation_passed") is not True:
        reasons.append("VALIDATION_STATUS_NOT_PASSED")
    resolution = snapshot.get("market_date_resolution")
    if not isinstance(resolution, dict) or not resolution.get("source") or not resolution.get("agreement"):
        reasons.append("MARKET_DATE_RESOLUTION_METADATA_MISSING")
    if not isinstance(metadata.get("market_date_resolution"), dict):
        reasons.append("CACHE_METADATA_RESOLUTION_MISSING")
    market = snapshot.get("market")
    if not isinstance(market, dict):
        reasons.append("MARKET_DATA_MISSING")
        return {"valid": False, "reasons": reasons}
    for category, group in MARKET_SYMBOLS.items():
        values = market.get(category)
        if not isinstance(values, dict):
            reasons.append(f"CATEGORY_MISSING:{category}")
            continue
        for symbol in group:
            item = values.get(symbol)
            if not isinstance(item, dict):
                reasons.append(f"REQUIRED_SYMBOL_MISSING:{symbol}")
                continue
            if item.get("verified") is not True or item.get("validation_passed") is not True:
                reasons.append(f"VALIDATION_FAILED:{symbol}")
            if not item.get("source"):
                reasons.append(f"SOURCE_MISSING:{symbol}")
            if item.get("canonical_market_date") != market_date:
                reasons.append(f"CANONICAL_DATE_MISSING:{symbol}")
            if category == "macro" and not all(item.get(field) for field in ("selected_observation_date", "freshness")):
                reasons.append(f"MISSING_AS_OF_METADATA:{symbol}")
    if snapshot.get("summary", {}).get("failed", 1) != 0:
        reasons.append("SUMMARY_HAS_CRITICAL_FAILURES")
    integrity = snapshot.get("historical_integrity")
    if require_historical_integrity and not isinstance(integrity, dict):
        reasons.append("HISTORICAL_INTEGRITY_METADATA_MISSING")
    if integrity is not None and (not isinstance(integrity, dict) or integrity.get("requested_market_date") != market_date or integrity.get("integrity_status") != "PASS" or integrity.get("future_data_leak_detected") is True):
        reasons.append("HISTORICAL_FUTURE_DATA_LEAK")
    return {"valid": not reasons, "reasons": reasons}


def load_valid_market_cache(market_date: str, *, path: Path | None = None, require_historical_integrity: bool = False) -> dict[str, Any] | None:
    """Return a validated same-date snapshot, or ``None`` for any cache miss."""
    cache_path = path or (OUTPUT_DIR / "market_snapshot.json")
    try:
        snapshot = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return snapshot if validate_market_snapshot_cache(snapshot, market_date, require_historical_integrity=require_historical_integrity)["valid"] else None


def inspect_market_snapshot_cache(market_date: str, *, path: Path | None = None, require_historical_integrity: bool = False) -> dict[str, Any]:
    """Inspect a cache candidate without exposing its contents."""
    cache_path = path or (OUTPUT_DIR / "market_snapshot.json")
    try:
        snapshot = json.loads(cache_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"valid": False, "reasons": ["CACHE_FILE_MISSING"]}
    except (OSError, json.JSONDecodeError):
        return {"valid": False, "reasons": ["CACHE_FILE_UNREADABLE"]}
    return validate_market_snapshot_cache(snapshot, market_date, require_historical_integrity=require_historical_integrity)


def log_market_cache_rejection(logger: logging.Logger, reasons: list[str]) -> None:
    """Log safe cache rejection diagnostics for the next controlled run."""
    logger.info("[CACHE] Market snapshot: MISS")
    if len(reasons) == 1:
        logger.info("[CACHE] Market snapshot rejection: %s", reasons[0])
    else:
        logger.info("[CACHE] Market snapshot rejection reasons:")
        for reason in reasons:
            logger.info("- %s", reason)


def collect_instrument(
    category: str,
    instrument: Any,
    client: TwelveDataClient,
    fred_client: FredClient,
    cboe_client: CboeClient,
    yahoo_client: YahooClient,
    logger: logging.Logger,
    market_date: str | None = None,
) -> dict[str, Any]:
    """Collect and validate one configured instrument without changing source rules."""
    logger.info("Request started: %s (%s)", instrument.symbol, instrument.display_name)
    def fetch(method: Any, instrument_value: Any) -> dict[str, Any]:
        if not market_date:
            return method(instrument_value, logger)
        try:
            return method(instrument_value, logger, market_date=market_date)
        except TypeError as exc:
            if "market_date" not in str(exc):
                raise
            # Compatibility for narrow offline test doubles; production
            # collectors all accept the historical keyword.
            return method(instrument_value, logger)

    if instrument.provider == "Twelve Data":
        result = fetch(client.fetch_instrument, instrument)
    elif instrument.provider == "FRED":
        result = fetch(fred_client.fetch_instrument, instrument)
    elif instrument.provider == "CBOE":
        result = fetch(cboe_client.fetch_vix, instrument)
        if not result.get("verified") and instrument.fallback_provider == "FRED":
            logger.warning("CBOE VIX primary source failed: %s", result.get("error", "unknown error"))
            logger.info("Attempting FRED VIX fallback")
            fallback_instrument = replace(instrument, provider="FRED", provider_symbol=instrument.fallback_symbol, fallback_provider=None, fallback_symbol=None)
            fallback_result = fetch(fred_client.fetch_instrument, fallback_instrument)
            fallback_result["fallback_used"] = True
            fallback_result["primary_source_error"] = result.get("error", "CBOE primary failure")
            result = fallback_result
    elif instrument.provider == "Yahoo Finance":
        result = fetch(yahoo_client.fetch_instrument, instrument)
    else:
        result = {"symbol": instrument.symbol, "display_name": instrument.display_name, "category": instrument.category, "source": "unconfigured", "verified": False, "error": "NO_PROVIDER_CONFIGURED"}

    valid = False
    if result.get("data_status") == "completed_daily_bar":
        valid, messages = validate_market_result(result)
        if not valid:
            logger.error("Validation status: failed for %s: %s", instrument.symbol, "; ".join(messages))
        else:
            logger.info("Validation status: passed for %s; warning=%s", instrument.symbol, result.get("warning", "none"))
    elif result.get("source") == "FRED" and result.get("verified"):
        valid, messages = validate_fred_result(result)
        if not valid:
            logger.error("Validation status: failed for %s: %s", instrument.symbol, "; ".join(messages))
        else:
            logger.info("Validation status: passed for %s", instrument.symbol)
    elif result.get("source") in {"CBOE", "Yahoo Finance / ICE Futures delayed"} and result.get("verified"):
        valid, messages = validate_macro_ohlc_result(result)
        if not valid:
            logger.error("Validation status: failed for %s: %s", instrument.symbol, "; ".join(messages))
        else:
            logger.info("Validation status: passed for %s", instrument.symbol)
    else:
        logger.error("Collection failure: %s - %s", instrument.symbol, result.get("error", "unknown provider error"))
        logger.info("Validation status: not run for %s because no market payload was received", instrument.symbol)
    result["validation_passed"] = bool(valid and result.get("verified") is True)
    if result.get("verified"):
        latest_date = result.get("latest_bar_datetime") or result.get("latest_observation_date")
        logger.info("Request success: %s; latest source date=%s", instrument.symbol, latest_date)
    return result


def main(market_date_override: str | None = None, refresh_movers: bool = False, refresh_market: bool = False, movers_prefilter_enabled: bool = True) -> int:
    """Run collection, validation, and JSON output generation."""
    logger = configure_logging()
    logger.info("Program startup")
    load_dotenv(BASE_DIR / ".env")
    api_key = os.getenv("TWELVE_DATA_API_KEY", "").strip()
    fred_api_key = os.getenv("FRED_API_KEY", "").strip()
    delay = max(0.0, float(os.getenv("TWELVE_DATA_REQUEST_DELAY_SECONDS", "0.25")))
    rate_limiter = CreditRateLimitController()
    client = TwelveDataClient(api_key, rate_limiter=rate_limiter)
    fred_client = FredClient(fred_api_key)
    cboe_client = CboeClient()
    yahoo_client = YahooClient()
    results: dict[str, dict[str, Any]] = {category: {} for category in MARKET_SYMBOLS}
    instruments = [(category, key, instrument) for category, group in MARKET_SYMBOLS.items() for key, instrument in group.items()]
    cache_hit = False
    cached_snapshot = None
    if market_date_override:
        if refresh_market:
            log_market_cache_rejection(logger, ["REFRESH_FORCED"])
        else:
            cache_diagnostic = inspect_market_snapshot_cache(market_date_override, require_historical_integrity=True)
            if cache_diagnostic["valid"]:
                cached_snapshot = load_valid_market_cache(market_date_override, require_historical_integrity=True)
            else:
                log_market_cache_rejection(logger, cache_diagnostic["reasons"])
    if cached_snapshot is not None:
        results = json.loads(json.dumps(cached_snapshot["market"]))
        cache_hit = True
        logger.info("[CACHE] Market snapshot: HIT")
        logger.info("Market API requests skipped: %d", len(instruments))

    to_collect = [] if cache_hit else instruments
    if not cache_hit and not market_date_override:
        to_collect = [(category, key, instrument) for category, key, instrument in instruments if category == "core"]

    for index, (category, key, instrument) in enumerate(to_collect):
        results[category][key] = collect_instrument(category, instrument, client, fred_client, cboe_client, yahoo_client, logger, market_date=market_date_override)
        if index < len(to_collect) - 1:
            time.sleep(delay)

    try:
        date_resolution = resolve_market_date(results.get("core", {}), market_date_override)
    except MarketDateResolutionError as exc:
        logger.error("Canonical market date resolution failed: %s", exc)
        return 1
    if not cache_hit and not market_date_override:
        if refresh_market:
            log_market_cache_rejection(logger, ["REFRESH_FORCED"])
        else:
            cache_diagnostic = inspect_market_snapshot_cache(date_resolution.market_date)
            cached_snapshot = load_valid_market_cache(date_resolution.market_date) if cache_diagnostic["valid"] else None
        if cached_snapshot is not None:
            results = json.loads(json.dumps(cached_snapshot["market"]))
            cache_hit = True
            logger.info("[CACHE] Market snapshot: HIT")
            logger.info("Market API requests skipped: %d", len(instruments) - len(to_collect))
        else:
            if not refresh_market:
                if not cache_diagnostic["valid"]:
                    log_market_cache_rejection(logger, cache_diagnostic["reasons"])
            remaining = [(category, key, instrument) for category, key, instrument in instruments if category != "core"]
            for index, (category, key, instrument) in enumerate(remaining):
                results[category][key] = collect_instrument(category, instrument, client, fred_client, cboe_client, yahoo_client, logger, market_date=market_date)
                if index < len(remaining) - 1:
                    time.sleep(delay)
    generated_at = datetime.now(timezone.utc)
    market_date = date_resolution.market_date
    logger.info("Canonical market date resolved: %s", market_date)
    logger.info("Resolution source: %s", date_resolution.source)
    logger.info("Core agreement: %s", date_resolution.agreement)
    if date_resolution.warning:
        logger.warning(date_resolution.warning)
    for symbol, item in results.get("macro", {}).items():
        if symbol == "GOLD" and item.get("verified"):
            results["macro"][symbol] = select_as_of_bars(item, market_date, require_exact=bool(market_date_override))
    all_results = [result for group in results.values() for result in group.values()]
    for result in all_results:
        if not result.get("verified") or not result.get("latest_observation_date"):
            continue
        observation_date = result.get("latest_observation_date")
        if observation_date == market_date:
            result["freshness"] = "current"
        else:
            result["freshness"] = "lagging"
            lag_days = (date.fromisoformat(market_date) - date.fromisoformat(observation_date)).days
            result["warning"] = "STALE_SOURCE_DATA" if lag_days > 3 else "SOURCE_DATE_BEHIND_MARKET_DATE"
            logger.warning("%s source date behind market date: %s < %s", result["symbol"], observation_date, market_date)
            if lag_days > 3:
                logger.warning("%s source data exceeds the %d-day stale threshold", result["symbol"], 3)
    for result in all_results:
        observation_date = result.get("latest_observation_date") or str(result.get("latest_bar_datetime", ""))[:10]
        result["canonical_market_date"] = market_date
        result["selected_observation_date"] = observation_date
        if result.get("verified") and not result.get("freshness"):
            result["freshness"] = "current" if observation_date == market_date else "lagging"
        result["validation_passed"] = bool(result.get("verified") and result.get("validation_passed"))
    historical_integrity = None
    if market_date_override:
        price_items = [item for category in ("core", "sectors") for item in results.get(category, {}).values()]
        if results.get("macro", {}).get("GOLD"):
            price_items.append(results["macro"]["GOLD"])
        macro_items = [item for symbol, item in results.get("macro", {}).items() if symbol != "GOLD"]
        source_dates = []
        exact_price_series = []
        asof_macro_series = []
        future_leak = False
        for item in [*price_items, *macro_items]:
            selected = item.get("selected_source_date") or str(item.get("latest_bar_datetime", ""))[:10] or item.get("latest_observation_date")
            if selected:
                source_dates.append(str(selected))
                if str(selected) > market_date:
                    future_leak = True
            entry = {"symbol": item.get("symbol"), "selected_source_date": selected}
            if item in macro_items:
                entry["asof_lag_days"] = item.get("asof_lag_days", 0)
                asof_macro_series.append(entry)
            else:
                entry["previous_source_date"] = item.get("previous_source_date")
                exact_price_series.append(entry)
        historical_integrity = {
            "requested_market_date": market_date,
            "future_data_leak_detected": future_leak,
            "exact_price_series": exact_price_series,
            "asof_macro_series": asof_macro_series,
            "max_source_date": max(source_dates) if source_dates else None,
            "integrity_status": "FAIL" if future_leak else "PASS",
        }
        if future_leak:
            logger.error("HISTORICAL_FUTURE_DATA_LEAK: requested=%s max_source_date=%s", market_date, historical_integrity["max_source_date"])
            for item in all_results:
                selected = item.get("selected_source_date") or str(item.get("latest_bar_datetime", ""))[:10] or item.get("latest_observation_date")
                if selected and str(selected) > market_date:
                    item["verified"] = False
                    item["validation_passed"] = False
                    item["error"] = "FUTURE_DATA_LEAK"
    snapshot = {
        "generated_at_utc": generated_at.isoformat(),
        "generated_at_local": generated_at.astimezone().isoformat(),
        "market_date": market_date,
        "market_date_resolution": {"source": date_resolution.source, "agreement": date_resolution.agreement, **({"warning": date_resolution.warning} if date_resolution.warning else {})},
        "provider": "Twelve Data + FRED + CBOE + Yahoo Finance",
        "market": results,
        **({"historical_integrity": historical_integrity, "status": "FAILED" if historical_integrity["integrity_status"] == "FAIL" else "READY"} if historical_integrity else {}),
        "cache_metadata": build_market_cache_metadata(
            market_date,
            {"source": date_resolution.source, "agreement": date_resolution.agreement, **({"warning": date_resolution.warning} if date_resolution.warning else {})},
            all_results,
            cache_status="HIT" if cache_hit else "MISS",
        ),
        "summary": {
            "requested": len(all_results),
            "successful": sum(1 for result in all_results if result.get("verified")),
            "failed": sum(1 for result in all_results if not result.get("verified")),
            "warnings": sum(1 for result in all_results if result.get("warning")),
        },
        "total_http_requests": client.request_count + fred_client.request_count + cboe_client.request_count + yahoo_client.request_count,
        "successful_api_requests": client.successful_api_requests + fred_client.successful_api_requests + cboe_client.successful_api_requests + yahoo_client.successful_api_requests,
        "retry_requests": client.retry_requests,
        "rate_limit_waits": rate_limiter.rate_limit_waits,
        "providers": {
            "Twelve Data": {
                "requested": client.request_count,
                "successful": client.successful_api_requests,
                "failed": client.request_count - client.successful_api_requests,
            },
            "FRED": {
                "successful": fred_client.successful_api_requests,
                "failed": fred_client.request_count - fred_client.successful_api_requests,
            },
            "CBOE": {
                "successful": cboe_client.successful_api_requests,
                "failed": cboe_client.request_count - cboe_client.successful_api_requests,
            },
            "Yahoo Finance": {
                "successful": yahoo_client.successful_api_requests,
                "failed": yahoo_client.request_count - yahoo_client.successful_api_requests,
            },
        },
    }
    current_path, historical_path = write_snapshot(snapshot, generated_at)
    logger.info("Final JSON created: %s and %s", current_path, historical_path)
    if historical_integrity and historical_integrity["integrity_status"] != "PASS":
        logger.error("HISTORICAL_FUTURE_DATA_LEAK: refusing historical movers and driver stages")
        return 1
    try:
        movers_path = run_movers_snapshot(client, logger, market_date, force_refresh=refresh_movers, prefilter_enabled=movers_prefilter_enabled, historical=bool(market_date_override))
    except MoversCollectionFailure as exc:
        logger.error("Movers collection blocked production: %s", exc)
        return 1
    try:
        movers_snapshot = json.loads(movers_path.read_text(encoding="utf-8"))
        movers_cache_status = movers_snapshot.get("cache_status", "MISS")
    except (OSError, json.JSONDecodeError):
        movers_cache_status = "MISS"
    logger.info("[CACHE] Movers snapshot: %s", movers_cache_status)
    logger.info("[CACHE] Live news: existing policy")
    logger.info("[CACHE] Verification: existing policy")
    # Catalyst mapping is performed after Live News V3 by run_daily.py.
    # Keeping market collection free of news discovery avoids duplicate work.
    logger.info("Total HTTP requests used: %d", snapshot["total_http_requests"])
    logger.info("Program completion")
    return 1 if snapshot["summary"]["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
