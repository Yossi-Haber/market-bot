"""Daily stock movers workflow using the safe Twelve Data batch path."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
from math import ceil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from collectors.market import TwelveDataClient
from collectors.rate_limit import QuotaPauseRequired
from collectors.symbols import normalize_symbol_for_twelve_data
from config import MOVERS_BATCH_SIZE, MOVERS_UNIVERSE_LIMIT, TOP_GAINERS_COUNT, TOP_LOSERS_COUNT
from config import Instrument
from validation.validate import validate_market_result
from movers_prefilter import collect_discovery_changes, run_shadow_benchmark


BASE_DIR = Path(__file__).resolve().parent
UNIVERSE_PATH = BASE_DIR / "data" / "universe.csv"
OUTPUT_DIR = BASE_DIR / "output"


def load_universe(limit: int = MOVERS_UNIVERSE_LIMIT) -> list[Instrument]:
    """Load and deduplicate the deterministic liquid large-cap universe."""
    by_symbol: dict[str, Instrument] = {}
    with UNIVERSE_PATH.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = (row.get("symbol") or "").strip().upper()
            company_name = (row.get("company_name") or "").strip()
            if not symbol or not company_name or symbol in by_symbol:
                continue
            by_symbol[symbol] = Instrument(
                symbol=symbol,
                display_name=company_name,
                category="movers",
                provider="Twelve Data",
                provider_symbol=normalize_symbol_for_twelve_data(symbol),
            )
    return list(by_symbol.values())[:limit]


MOVERS_CACHE_SCHEMA_VERSION = 1
MOVERS_ALGORITHM_VERSION = "movers-v2-prefilter"
MOVERS_MIN_VALID_RATIO = 110 / 120
PREFILTER_CANDIDATE_SIZE = 40
PREFILTER_SIDE_SIZE = 20
PREFILTER_MIN_DISCOVERED = 115
PREFILTER_MIN_VALID_RATIO = 0.95
_REQUIRED_MOVER_FIELDS = {
    "symbol",
    "company_name",
    "close",
    "previous_close",
    "change",
    "change_pct",
    "latest_bar_datetime",
    "source",
    "verified",
}


def universe_fingerprint(universe: list[Instrument]) -> str:
    """Return a stable fingerprint for the exact mover universe and names."""
    payload = "\n".join(f"{item.symbol}|{item.display_name}" for item in universe)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_valid_movers_cache(
    market_date: str,
    *,
    path: Path | None = None,
    prefilter_enabled: bool | None = None,
    require_historical_integrity: bool = False,
) -> dict[str, Any] | None:
    """Load a completed, same-date movers cache or return ``None`` safely."""
    cache_path = path or (OUTPUT_DIR / "movers_snapshot.json")
    try:
        snapshot = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(snapshot, dict):
        return None
    universe = load_universe()
    if snapshot.get("market_date") != market_date:
        return None
    if snapshot.get("universe_hash") != universe_fingerprint(universe):
        return None
    if snapshot.get("schema_version") != MOVERS_CACHE_SCHEMA_VERSION:
        return None
    if snapshot.get("algorithm_version") != MOVERS_ALGORITHM_VERSION:
        return None
    if prefilter_enabled is not None and snapshot.get("prefilter_enabled") is not prefilter_enabled:
        return None
    if snapshot.get("collection_status") != "SUCCESS":
        return None
    if require_historical_integrity:
        integrity = snapshot.get("historical_integrity")
        if not isinstance(integrity, dict) or integrity.get("requested_market_date") != market_date or integrity.get("integrity_status") != "PASS" or integrity.get("future_data_leak_detected") is True:
            return None
    if snapshot.get("prefilter_enabled") is True:
        candidate_size = snapshot.get("candidate_pool_size")
        fallback_used = snapshot.get("fallback_used") is True
        if not fallback_used and candidate_size != PREFILTER_CANDIDATE_SIZE:
            return None
        expected_requested = len(universe) if fallback_used else candidate_size
    else:
        expected_requested = len(universe)
    if snapshot.get("symbols_requested") != expected_requested:
        return None
    if not isinstance(snapshot.get("top_gainers"), list) or not isinstance(snapshot.get("top_losers"), list):
        return None
    if not snapshot["top_gainers"] or not snapshot["top_losers"]:
        return None
    for item in [*snapshot["top_gainers"], *snapshot["top_losers"]]:
        if not isinstance(item, dict) or not _REQUIRED_MOVER_FIELDS.issubset(item):
            return None
        if item.get("verified") is not True:
            return None
    return snapshot


def _prefilter_candidate_pool(
    universe: list[Instrument],
    discovery_results: list[dict[str, Any]],
    discovery_metrics: dict[str, Any],
    market_date: str,
) -> tuple[list[Instrument] | None, str | None]:
    """Validate Yahoo discovery and return a production candidate pool."""
    if len(universe) != MOVERS_UNIVERSE_LIMIT:
        return None, "UNIVERSE_SIZE_MISMATCH"
    if discovery_metrics.get("symbols_requested") != MOVERS_UNIVERSE_LIMIT:
        return None, "DISCOVERY_NOT_ALL_SYMBOLS_ATTEMPTED"
    if discovery_metrics.get("symbols_discovered", 0) < PREFILTER_MIN_DISCOVERED:
        return None, "DISCOVERY_COVERAGE_LOW"
    if discovery_metrics.get("critical_failure"):
        return None, "DISCOVERY_CRITICAL_FAILURE"

    universe_by_symbol = {item.symbol: item for item in universe}
    valid = [
        item for item in discovery_results
        if item.get("symbol") in universe_by_symbol
        and item.get("observation_date") == market_date
        and isinstance(item.get("discovery_change_pct"), (int, float))
    ]
    positive = sorted(
        [item for item in valid if float(item["discovery_change_pct"]) > 0],
        key=lambda item: float(item["discovery_change_pct"]),
        reverse=True,
    )
    negative = sorted(
        [item for item in valid if float(item["discovery_change_pct"]) < 0],
        key=lambda item: float(item["discovery_change_pct"]),
    )
    if len(positive) < PREFILTER_SIDE_SIZE:
        return None, "DISCOVERY_POSITIVE_SIDE_LOW"
    if len(negative) < PREFILTER_SIDE_SIZE:
        return None, "DISCOVERY_NEGATIVE_SIDE_LOW"

    symbols = list(dict.fromkeys(
        [item["symbol"] for item in positive[:PREFILTER_SIDE_SIZE]]
        + [item["symbol"] for item in negative[:PREFILTER_SIDE_SIZE]]
    ))
    if len(symbols) != PREFILTER_CANDIDATE_SIZE:
        return None, "CANDIDATE_POOL_SIZE_INVALID"
    if any(not isinstance(symbol, str) or symbol not in universe_by_symbol for symbol in symbols):
        return None, "CANDIDATE_POOL_SYMBOL_INVALID"
    return [universe_by_symbol[symbol] for symbol in symbols], None


def _prefilter_metadata(
    *,
    enabled: bool,
    universe_size: int,
    discovery_metrics: dict[str, Any] | None,
    candidate_pool_size: int,
    fallback_used: bool,
    fallback_reason: str | None,
    twelve_data_symbols_requested: int,
) -> dict[str, Any]:
    """Return the persisted production prefilter audit contract."""
    return {
        "enabled": enabled,
        "mode": "production" if enabled else "legacy_full_universe",
        "discovery_source": "Yahoo Finance" if enabled else None,
        "universe_size": universe_size,
        "discovered_symbols": (discovery_metrics or {}).get("symbols_discovered", 0),
        "discovery_runtime_seconds": (discovery_metrics or {}).get("runtime_seconds", 0),
        "candidate_pool_size": candidate_pool_size,
        "twelve_data_symbols_requested": twelve_data_symbols_requested,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
    }


def _collect_mover_universe(client: TwelveDataClient, logger: logging.Logger, market_date: str, universe: list[Instrument]) -> dict[str, Any]:
    """Collect and validate one Twelve Data production universe."""
    started = time.monotonic()
    request_start = client.request_count
    successful_start = client.successful_api_requests
    retry_start = client.retry_requests
    credits_start = client.rate_limiter.daily_requests
    waits_start = client.rate_limiter.rate_limit_waits
    valid_results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    batch_states: list[dict[str, Any]] = []
    checkpoint_path = getattr(client, "mover_checkpoint_path", None)
    if checkpoint_path and Path(checkpoint_path).is_file():
        try:
            saved = json.loads(Path(checkpoint_path).read_text(encoding="utf-8"))
            if saved.get("market_date") == market_date and isinstance(saved.get("valid_results"), list):
                valid_results = [item for item in saved["valid_results"] if isinstance(item, dict) and item.get("verified") is True]
                errors = [item for item in saved.get("errors", []) if isinstance(item, dict)]
                batch_states = [item for item in saved.get("batch_states", []) if isinstance(item, dict)]
                logger.info("[RESUME] Movers checkpoint loaded: %d validated symbols", len(valid_results))
        except (OSError, ValueError, TypeError):
            logger.warning("[RESUME] Ignoring malformed movers checkpoint for %s", market_date)
    completed_symbols = {str(item.get("symbol", "")).upper() for item in valid_results}
    processed = len(completed_symbols)
    skipped = 0
    symbols_returned = len(completed_symbols)
    batches_total = ceil(len(universe) / MOVERS_BATCH_SIZE) if universe else 0
    saved_batch_states = {int(item.get("batch_number", 0)): item for item in batch_states if item.get("batch_number")}
    for batch_index, offset in enumerate(range(0, len(universe), MOVERS_BATCH_SIZE), start=1):
        batch = [item for item in universe[offset : offset + MOVERS_BATCH_SIZE] if item.symbol.upper() not in completed_symbols]
        if not batch:
            continue
        logger.info("Batch %d/%d prepared: %s", batch_index, batches_total, ",".join(item.symbol for item in batch))
        if _batch_will_wait_for_quota(client, len(batch)):
            logger.info("Batch %d/%d waiting for quota", batch_index, batches_total)
        valid_before = len(valid_results)
        try:
            results = client.fetch_stock_batch(batch, logger, market_date=market_date)
        except QuotaPauseRequired:
            raise
        except Exception as exc:
            logger.error("Batch %d/%d failed before response: %s", batch_index, batches_total, type(exc).__name__)
            results = {}
        if not isinstance(results, dict):
            results = {}
        returned_count = sum(1 for instrument in batch if instrument.symbol in results and results[instrument.symbol].get("_provider_returned", bool(results[instrument.symbol].get("verified"))))
        symbols_returned += returned_count
        logger.info("Batch %d/%d received: %d/%d symbols", batch_index, batches_total, returned_count, len(batch))
        for instrument in batch:
            processed += 1
            result = results.get(instrument.symbol, {"symbol": instrument.symbol, "error": "MISSING_BATCH_RESULT"})
            if not result.get("verified"):
                errors = [entry for entry in errors if entry.get("symbol") != instrument.symbol]
                errors.append({"symbol": instrument.symbol, "company_name": instrument.display_name, "error": result.get("error", "PROVIDER_ERROR")})
                continue
            valid, messages = validate_market_result(result)
            result.pop("warning", None)
            if not valid:
                errors.append({"symbol": instrument.symbol, "company_name": instrument.display_name, "error": "VALIDATION_ERROR", "validation_errors": messages})
                continue
            if result.get("latest_bar_datetime", "")[:10] != market_date:
                skipped += 1
                errors.append({"symbol": instrument.symbol, "company_name": instrument.display_name, "error": "MOVER_DATE_MISMATCH", "latest_bar_datetime": result.get("latest_bar_datetime")})
                logger.warning("Excluded %s: MOVER_DATE_MISMATCH", instrument.symbol)
                continue
            if result.get("volume") is None or result.get("volume") <= 0:
                skipped += 1
                errors.append({"symbol": instrument.symbol, "company_name": instrument.display_name, "error": "MISSING_OR_INVALID_VOLUME"})
                continue
            if abs(float(result["change_pct"])) > 30:
                result["warning"] = "SUSPICIOUS_STOCK_MOVE"
            valid_results.append(_mover_output(result, instrument.display_name))
            completed_symbols.add(instrument.symbol.upper())
        batch_valid = len(valid_results) - valid_before
        batch_state = "SUCCESS" if returned_count == len(batch) and batch_valid == len(batch) else ("PARTIAL" if returned_count else "FAILED")
        state_entry = {"batch_number": batch_index, "symbols": [item.symbol for item in batch], "state": batch_state, "symbols_returned": returned_count, "symbols_valid": batch_valid}
        saved_batch_states[batch_index] = state_entry
        batch_states = [saved_batch_states[index] for index in sorted(saved_batch_states)]
        if checkpoint_path:
            checkpoint = Path(checkpoint_path)
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            temporary = checkpoint.with_suffix(checkpoint.suffix + ".tmp")
            temporary.write_text(json.dumps({"market_date": market_date, "updated_at": datetime.now(timezone.utc).isoformat(), "valid_results": valid_results, "errors": errors, "batch_states": batch_states}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, checkpoint)
        logger.info("Batch %d/%d completed: %s", batch_index, batches_total, batch_state)
    minimum_ratio = PREFILTER_MIN_VALID_RATIO if len(universe) == PREFILTER_CANDIDATE_SIZE else MOVERS_MIN_VALID_RATIO
    required_valid = ceil(len(universe) * minimum_ratio) if universe else 0
    collection_complete = bool(universe) and len(universe) == processed and len(valid_results) >= required_valid and len(batch_states) == batches_total
    logger.info("Movers collection summary: batches_total=%d batches_success=%d symbols_requested=%d symbols_returned=%d symbols_valid=%d symbols_failed=%d", batches_total, sum(1 for item in batch_states if item["state"] == "SUCCESS"), len(universe), symbols_returned, len(valid_results), len(universe) - len(valid_results))
    return {
        "started": started,
        "valid_results": valid_results,
        "errors": errors,
        "batch_states": batch_states,
        "processed": processed,
        "skipped": skipped,
        "symbols_returned": symbols_returned,
        "batches_total": batches_total,
        "collection_complete": collection_complete,
        "request_statistics": {
            "http_requests": client.request_count - request_start,
            "successful_api_requests": client.successful_api_requests - successful_start,
            "retry_requests": client.retry_requests - retry_start,
            "twelve_data_credits_used": client.rate_limiter.daily_requests - credits_start,
            "rate_limit_waits": client.rate_limiter.rate_limit_waits - waits_start,
            "runtime_seconds": round(time.monotonic() - started, 3),
        },
    }


def run_movers_snapshot(
    client: TwelveDataClient,
    logger: logging.Logger,
    market_date: str,
    *,
    force_refresh: bool = False,
    prefilter_enabled: bool = False,
    historical: bool = False,
) -> Path:
    """Collect production movers using Yahoo discovery with safe full fallback."""
    started = time.monotonic()
    universe = load_universe()
    current_path = OUTPUT_DIR / "movers_snapshot.json"
    cached = None if force_refresh else load_valid_movers_cache(market_date, path=current_path, prefilter_enabled=prefilter_enabled)
    if cached is not None:
        logger.info("Movers cache HIT — market_date=%s", market_date)
        cached["cache_status"] = "HIT"
        current_path.write_text(json.dumps(cached, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return current_path
    if force_refresh:
        logger.info("Movers cache refresh explicitly requested")
    discovery_metrics: dict[str, Any] | None = None
    candidate_universe = universe
    candidate_pool_size = 0
    fallback_used = False
    fallback_reason = "PREFILTER_DISABLED" if not prefilter_enabled else None
    if prefilter_enabled:
        logger.info("[PREFILTER] Discovery started")
        if historical:
            discovery_results, discovery_metrics = collect_discovery_changes(universe, market_date, logger, historical=True)
        else:
            discovery_results, discovery_metrics = collect_discovery_changes(universe, market_date, logger)
        logger.info("[PREFILTER] Discovery completed: %d/%d in %.1fs", discovery_metrics.get("symbols_discovered", 0), len(universe), discovery_metrics.get("runtime_seconds", 0))
        candidate_universe, reason = _prefilter_candidate_pool(universe, discovery_results, discovery_metrics, market_date)
        if reason:
            fallback_used = True
            fallback_reason = reason
            candidate_universe = universe
            logger.warning("[PREFILTER] FALLBACK_FULL_UNIVERSE reason=%s", reason)
            logger.info("[PREFILTER] Production mode: FALLBACK")
        else:
            fallback_used = False
            candidate_pool_size = len(candidate_universe)
            logger.info("[PREFILTER] Candidate pool built: %d", len(candidate_universe))
            logger.info("[PREFILTER] Yahoo discovery: PASS")
            logger.info("[PREFILTER] Candidate pool: %d/%d", len(candidate_universe), len(universe))
            logger.info("[PREFILTER] Twelve Data symbols reduced: %d -> %d", len(universe), len(candidate_universe))
            logger.info("[PREFILTER] Twelve Data batches: %d", ceil(len(candidate_universe) / MOVERS_BATCH_SIZE))
            logger.info("[PREFILTER] Production mode: ACTIVE")
    logger.info("Movers processing started: %d symbols", len(candidate_universe))
    collection = _collect_mover_universe(client, logger, market_date, candidate_universe)
    if prefilter_enabled and not fallback_used and not collection["collection_complete"]:
        fallback_used = True
        fallback_reason = "CANDIDATE_TWELVE_DATA_INCOMPLETE"
        logger.warning("[PREFILTER] FALLBACK_FULL_UNIVERSE reason=%s", fallback_reason)
        logger.info("[PREFILTER] Production mode: FALLBACK")
        candidate_universe = universe
        collection = _collect_mover_universe(client, logger, market_date, candidate_universe)
    final_universe = candidate_universe
    collection_complete = collection["collection_complete"]
    valid_results = collection["valid_results"]
    gainers = sorted(valid_results, key=lambda item: item["change_pct"], reverse=True)[:TOP_GAINERS_COUNT] if collection_complete else []
    losers = sorted(valid_results, key=lambda item: item["change_pct"])[:TOP_LOSERS_COUNT] if collection_complete else []
    prefilter = _prefilter_metadata(enabled=prefilter_enabled, universe_size=len(universe), discovery_metrics=discovery_metrics, candidate_pool_size=candidate_pool_size, fallback_used=fallback_used, fallback_reason=fallback_reason, twelve_data_symbols_requested=len(final_universe))
    generated_at = datetime.now(timezone.utc)
    historical_integrity = None
    if historical:
        prefilter_dates = [str(item.get("observation_date")) for item in (discovery_results if discovery_metrics else []) if item.get("observation_date")]
        validation_dates = [str(item.get("latest_bar_datetime", ""))[:10] for item in valid_results if item.get("latest_bar_datetime")]
        all_dates = [*prefilter_dates, *validation_dates]
        future_leak = any(value > market_date for value in all_dates)
        historical_integrity = {
            "requested_market_date": market_date,
            "future_data_leak_detected": future_leak,
            "prefilter_date": market_date if discovery_metrics is not None else None,
            "validation_date": market_date,
            "max_source_date": max(all_dates) if all_dates else None,
            "integrity_status": "FAIL" if future_leak else "PASS",
        }
    snapshot = {
        "generated_at_utc": generated_at.isoformat(), "market_date": market_date, "cache_status": "MISS",
        "collection_status": "SUCCESS" if collection_complete else "FAILED", "schema_version": MOVERS_CACHE_SCHEMA_VERSION,
        "algorithm_version": MOVERS_ALGORITHM_VERSION, "universe_hash": universe_fingerprint(universe),
        "symbols_requested": len(final_universe), "symbols_returned": collection["symbols_returned"], "symbols_valid": len(valid_results),
        "symbols_failed": len(final_universe) - len(valid_results), "batches": collection["batch_states"],
        "universe": {"source": "data/universe.csv (curated liquid S&P 500/Nasdaq 100 subset)", "total_symbols": len(final_universe), "processed": collection["processed"], "valid": len(valid_results), "failed": sum(1 for error in collection["errors"] if error["error"] not in {"MOVER_DATE_MISMATCH", "MISSING_OR_INVALID_VOLUME"}), "skipped": collection["skipped"]},
        "top_gainers": gainers, "top_losers": losers, "errors": collection["errors"], "prefilter": prefilter,
        "prefilter_enabled": prefilter_enabled, "candidate_pool_size": prefilter["candidate_pool_size"], "discovery_source": prefilter["discovery_source"], "fallback_used": fallback_used,
        "request_statistics": collection["request_statistics"],
        **({"historical_integrity": historical_integrity} if historical_integrity else {}),
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    if not collection_complete or (historical_integrity and historical_integrity["integrity_status"] != "PASS"):
        current_path.write_text(text, encoding="utf-8")
        if historical_integrity and historical_integrity["integrity_status"] != "PASS":
            logger.error("HISTORICAL_FUTURE_DATA_LEAK: refusing to calculate historical mover ranking")
            logger.warning("Shadow benchmark skipped: reason=HISTORICAL_FUTURE_DATA_LEAK")
            raise MoversCollectionFailure("HISTORICAL_FUTURE_DATA_LEAK")
        logger.error("Movers collection incomplete; refusing to calculate production ranking")
        logger.error("Movers failed diagnostic snapshot created: %s", current_path)
        logger.warning("Shadow benchmark skipped: reason=INCOMPLETE_TWELVE_DATA_GROUND_TRUTH")
        raise MoversCollectionFailure("INCOMPLETE_TWELVE_DATA_GROUND_TRUTH")
    history_path = OUTPUT_DIR / f"movers_snapshot_{generated_at.strftime('%Y%m%d_%H%M%S')}.json"
    current_path.write_text(text, encoding="utf-8")
    history_path.write_text(text, encoding="utf-8")
    logger.info("Top gainers calculated")
    logger.info("Top losers calculated")
    logger.info("Movers snapshot created: %s", current_path)
    if not prefilter_enabled or fallback_used:
        run_shadow_benchmark(universe, valid_results, market_date, logger, OUTPUT_DIR)
    else:
        logger.info("Shadow benchmark skipped: production prefilter is not full-universe ground truth")
    return current_path


def _mover_output(result: dict[str, Any], company_name: str) -> dict[str, Any]:
    """Select the stable public mover schema."""
    return {
        "symbol": result["symbol"],
        "company_name": company_name,
        "close": result["close"],
        "previous_close": result["previous_close"],
        "change": result["change"],
        "change_pct": result["change_pct"],
        "open": result.get("open"),
        "high": result.get("high"),
        "low": result.get("low"),
        "volume": result.get("volume"),
        "latest_bar_datetime": result["latest_bar_datetime"],
        "source": "Twelve Data",
        "verified": True,
        **({"warning": result["warning"]} if result.get("warning") else {}),
    }


def _batch_will_wait_for_quota(client: TwelveDataClient, credits_required: int) -> bool:
    """Predict the existing limiter wait for batch-level observability only."""
    limiter = client.rate_limiter
    credits_left = getattr(limiter, "credits_left", None)
    if credits_left is not None:
        return credits_left < credits_required
    requests_in_minute = getattr(limiter, "_requests_in_minute", 0)
    minute_quota = getattr(getattr(limiter, "limits", None), "minute_quota", 8)
    return requests_in_minute + credits_required > minute_quota


class MoversCollectionFailure(RuntimeError):
    """Raised when Twelve Data coverage is unsafe for production ranking."""
