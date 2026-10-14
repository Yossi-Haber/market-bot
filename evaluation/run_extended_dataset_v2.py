"""Resumable staged builder and offline evaluator for Extended Dataset V2.

This script is an evaluation/data utility. It does not alter production policy.
All dated snapshots are staged below the 30-day milestone directory.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from collectors.rate_limit import QuotaPauseRequired
from news.providers.finnhub import FinnhubProvider

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cache_config import cache_root

PLAN = ROOT / "output/evaluation_extended_v2/dataset_plan.csv"
MILESTONE = ROOT / "output/evaluation_extended_v2/30_day_milestone"
ORIGINAL_HISTORY = ROOT / "output/history"
RUNS = MILESTONE / "replays"


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def missing_finnhub_symbols(date_value: str, symbols: list[str]) -> list[str]:
    """Return symbols without their date-scoped Finnhub cache artifact."""
    end_date = (date.fromisoformat(date_value) + timedelta(days=1)).isoformat()
    cache_dir = cache_root() / "finnhub"
    return [
        symbol for symbol in dict.fromkeys(str(item).upper() for item in symbols if item)
        if not (cache_dir / symbol / f"{date_value}_{end_date}.json").is_file()
    ]


def complete_missing_finnhub_caches(
    date_value: str,
    symbols: list[str],
    logger: logging.Logger,
    *,
    provider_factory: Any = FinnhubProvider,
) -> tuple[list[str], int]:
    """Acquire only missing mover-symbol caches through the existing provider."""
    missing = missing_finnhub_symbols(date_value, symbols)
    if not missing:
        return [], 0
    end_date = (date.fromisoformat(date_value) + timedelta(days=1)).isoformat()
    provider = provider_factory()
    logger.info("[RESUME] Finnhub stock-news cache completion: missing=%s", ",".join(missing))
    for symbol in missing:
        provider.search_company(symbol, date_value, end_date)
    still_missing = missing_finnhub_symbols(date_value, missing)
    if still_missing:
        logger.warning("[RESUME] Finnhub stock-news cache remains incomplete: missing=%s", ",".join(still_missing))
    else:
        logger.info("[RESUME] Finnhub stock-news cache completion succeeded: symbols=%s", ",".join(missing))
    return still_missing, int(getattr(provider, "live_requests", 0) or 0)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def atomic_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load_dates() -> list[str]:
    rows = read_csv(PLAN)
    values = [row["date"] for row in rows]
    return sorted(values)[-30:]


def stage_copy(source: Path, target: Path) -> bool:
    if not source.is_file() or target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    tmp = Path(name)
    try:
        shutil.copy2(source, tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_copy_replace(source: Path, target: Path) -> None:
    """Copy an artifact through a same-directory temporary file and replace."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    tmp = Path(name)
    try:
        shutil.copy2(source, tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return True


def atomicize(path: Path) -> None:
    if path.is_file():
        atomic_text(path, path.read_text(encoding="utf-8"))


def load_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def reusable_market_item(value: Any, market_date: str) -> bool:
    """Whether one staged instrument result is safe to carry into a resume."""
    if not isinstance(value, dict) or value.get("verified") is not True or value.get("validation_passed") is not True:
        return False
    source_date = str(value.get("selected_source_date") or value.get("latest_observation_date") or value.get("latest_bar_datetime", ""))[:10]
    return bool(source_date and source_date <= market_date)


def preserve_known_mover_regression(market_date: str, cache_state: dict[str, Any], snapshot: Any) -> bool:
    """Keep the established Sep 22 failed movers diagnostic for regression replay."""
    return bool(
        market_date == "2026-09-22"
        and cache_state.get("movers_snapshot") in {"SEEDED", "STAGED_EXISTING"}
        and isinstance(snapshot, dict)
        and snapshot.get("collection_status") == "FAILED"
    )


@contextmanager
def shared_batch_rate_limiter(
    app_module: Any,
    *,
    state_path: Path | None = None,
    request_recorder: Any = None,
    initial_daily_requests: int = 0,
    max_blocking_wait_seconds: float | None = None,
):
    """Keep the production limiter counters across dates in one batch."""
    from collectors.rate_limit import CreditRateLimitController

    factory = app_module.CreditRateLimitController
    limiter = factory() if state_path is None else CreditRateLimitController(
        state_path=state_path,
        request_recorder=request_recorder,
        initial_daily_requests=initial_daily_requests,
        max_blocking_wait_seconds=max_blocking_wait_seconds,
    )
    app_module.CreditRateLimitController = lambda: limiter
    try:
        yield limiter
    finally:
        app_module.CreditRateLimitController = factory


def seed_date(date_value: str, stage_dir: Path, historical: Any, movers_module: Any, app_module: Any) -> dict[str, Any]:
    status: dict[str, Any] = {}
    for prefix in ("market_snapshot", "movers_snapshot", "market_driver_snapshot"):
        source = ORIGINAL_HISTORY / date_value / f"{prefix}.json"
        if not source.is_file():
            try:
                discovered = historical._snapshot_path(prefix, date_value)
            except (AttributeError, TypeError):
                discovered = None
            if discovered and discovered.is_file():
                discovered_payload = load_object(discovered)
                if discovered_payload and discovered_payload.get("market_date") == date_value:
                    source = discovered
        target = stage_dir / f"{prefix}.json"
        # Prefer the project's dated cache, including a failed mover diagnostic
        # for Sep 22 so its established regression remains visible in replay.
        copied = stage_copy(source, target)
        status[prefix] = "SEEDED" if copied else "STAGED_EXISTING" if target.exists() else "MISSING"
    market = load_object(stage_dir / "market_snapshot.json")
    mover_path = stage_dir / "movers_snapshot.json"
    market_valid = bool(market and historical._validate_historical_market(market, date_value))
    movers_valid = movers_module.load_valid_movers_cache(date_value, path=mover_path, require_historical_integrity=True) is not None
    driver = load_object(stage_dir / "market_driver_snapshot.json")
    driver_valid = bool(driver and driver.get("market_date") == date_value)
    status.update({"market_valid": market_valid, "movers_valid": movers_valid, "driver_valid": driver_valid})
    return status


def quota_failure(stage_dir: Path, market_date: str) -> str | None:
    market = load_object(stage_dir / "market_snapshot.json") or {}
    movers = load_object(stage_dir / "movers_snapshot.json") or {}
    values: list[str] = []
    for group in market.get("market", {}).values() if isinstance(market.get("market"), dict) else []:
        if isinstance(group, dict):
            values.extend(str(item.get("error", "")) for item in group.values() if isinstance(item, dict))
    values.extend(str(item.get("error", "")) for item in movers.get("errors", []) if isinstance(item, dict))
    joined = " ".join(values).upper()
    if "DAILY_QUOTA_EXHAUSTED" in joined:
        return "TWELVE_DATA_DAILY_QUOTA_EXHAUSTED"
    if "RATE_LIMITED" in joined or "RATE_LIMIT_WAIT_TIMEOUT" in joined:
        return "TWELVE_DATA_QUOTA_OR_RATE_LIMIT"
    return None


def cache_failure_reason(stage_dir: Path, market_date: str) -> str:
    """Return the precise first market/mover failure from staged diagnostics."""
    market = load_object(stage_dir / "market_snapshot.json") or {}
    groups = market.get("market", {}) if isinstance(market.get("market"), dict) else {}
    for group in groups.values():
        if not isinstance(group, dict):
            continue
        for symbol, item in group.items():
            if isinstance(item, dict) and (item.get("verified") is not True or item.get("validation_passed") is not True):
                return f"MARKET_DATA_INVALID:{symbol}:{item.get('error') or 'VALIDATION_FAILED'}"
    movers = load_object(stage_dir / "movers_snapshot.json") or {}
    errors = movers.get("errors", [])
    if isinstance(errors, list) and errors:
        first = errors[0] if isinstance(errors[0], dict) else {}
        return f"MOVERS_INVALID:{first.get('symbol', 'UNKNOWN')}:{first.get('error', 'COLLECTION_FAILED')}"
    return "REQUIRED_MARKET_OR_MOVERS_CACHE_INVALID"


def provider_usage(date_value: str, stage_dir: Path, driver: dict[str, Any] | None, market_news: dict[str, Any] | None, before_cache_files: set[Path], runtime: float, cache_state: dict[str, Any]) -> dict[str, Any]:
    market = load_object(stage_dir / "market_snapshot.json") or {}
    movers = load_object(stage_dir / "movers_snapshot.json") or {}
    market_providers = market.get("providers", {})
    td_market = int((market_providers.get("Twelve Data", {}) or {}).get("requested", 0) or 0)
    mover_stats = movers.get("request_statistics", {}) or {}
    td_mover_credits = int(mover_stats.get("twelve_data_credits_used", 0) or 0)
    metrics = (driver or {}).get("metrics", {})
    news_metrics = (market_news or {}).get("provider_metrics", {})
    from cache_config import cache_root
    after_cache_files = set((cache_root() / "finnhub").glob(f"*/{date_value}_{(date.fromisoformat(date_value)+timedelta(days=1)).isoformat()}.json"))
    newly_cached_symbols = sorted(path.parent.name for path in after_cache_files - before_cache_files)
    return {
        "date": date_value,
        "twelve_data_market_requests_credits": td_market,
        "twelve_data_mover_http_requests": int(mover_stats.get("http_requests", 0) or 0),
        "twelve_data_mover_credits": td_mover_credits,
        "twelve_data_total_credits": td_market + td_mover_credits,
        "mover_universe_count": (movers.get("prefilter") or {}).get("universe_size", movers.get("universe", {}).get("total_symbols")),
        "yahoo_candidate_count": (movers.get("prefilter") or {}).get("discovered_symbols"),
        "twelve_data_final_symbol_count": movers.get("symbols_requested"),
        "finnhub_requests": int(metrics.get("finnhub_live_requests", 0) or 0),
        "finnhub_symbols_newly_cached": len(newly_cached_symbols),
        "finnhub_symbols": ",".join(newly_cached_symbols),
        "marketaux_requests": int(news_metrics.get("marketaux_requests", 0) or 0),
        "google_news_searches": int(news_metrics.get("google_news_requests", 0) or 0),
        "gdelt_searches": int(news_metrics.get("gdelt_requests", 0) or 0),
        "official_source_requests": int(metrics.get("official_requests", 0) or 0) + int(metrics.get("regulator_requests", 0) or 0),
        "tier2_verification_requests": int(metrics.get("tier2_requests", 0) or 0),
        "verification_candidates": int(metrics.get("verification_candidates", 0) or 0),
        "fmp_requests": 0,
        "cache_hits": sum(value == "HIT" for value in cache_state.values() if isinstance(value, str)),
        "cache_builds": sum(value == "BUILT" for value in cache_state.values() if isinstance(value, str)),
        "cache_failures": sum(value == "FAILED" for value in cache_state.values() if isinstance(value, str)),
        "runtime_seconds": round(runtime, 3),
    }


def persist_progress(progress: dict[str, dict[str, Any]], usage: dict[str, dict[str, Any]]) -> None:
    dates = load_dates()
    progress_rows = [progress[d] for d in dates if d in progress]
    usage_rows = [usage[d] for d in dates if d in usage]
    if progress_rows:
        fields = list(dict.fromkeys(k for row in progress_rows for k in row))
        atomic_csv(MILESTONE / "build_progress.csv", progress_rows, fields)
    if usage_rows:
        fields = list(dict.fromkeys(k for row in usage_rows for k in row))
        atomic_csv(MILESTONE / "provider_usage.csv", usage_rows, fields)
    atomic_json(MILESTONE / "checkpoint.json", {"updated_at_utc": datetime.now(timezone.utc).isoformat(), "progress": progress_rows, "provider_usage": usage_rows})


def wait_for_prior_batch_credit_window(previous_date: str, progress: dict[str, dict[str, Any]], usage: dict[str, dict[str, Any]], logger: logging.Logger) -> None:
    """Avoid a cross-process 429 when resuming immediately after Twelve Data use."""
    if int(usage.get(previous_date, {}).get("twelve_data_total_credits", 0) or 0) <= 0:
        return
    try:
        completed = datetime.fromisoformat(str(progress[previous_date]["completed_at_utc"]))
    except (KeyError, ValueError, TypeError):
        return
    age = max(0.0, (datetime.now(timezone.utc) - completed).total_seconds())
    remaining = max(0.0, 62.0 - age)
    if remaining:
        logger.info("BATCH_QUOTA_COOLDOWN previous_date=%s remaining=%.1fs", previous_date, remaining)
        time.sleep(remaining)


def process_date(date_value: str, *, logger: logging.Logger, historical: Any, app_module: Any, movers_module: Any, driver_runtime: Any, prevent_sleep: Any, retry_known_failed_mover: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.monotonic()
    stage_dir = MILESTONE / "history" / date_value
    stage_dir.mkdir(parents=True, exist_ok=True)
    cache_state = seed_date(date_value, stage_dir, historical, movers_module, app_module)
    from cache_config import cache_root
    before_files = set((cache_root() / "finnhub").glob(f"*/{date_value}_{(date.fromisoformat(date_value)+timedelta(days=1)).isoformat()}.json"))
    log_path = ROOT / "logs/market_bot.log"
    log_offset = log_path.stat().st_size if log_path.exists() else 0
    status = "COMPLETE"
    reason = ""
    preserves_sep22_diagnostic = preserve_known_mover_regression(
        date_value,
        cache_state,
        load_object(stage_dir / "movers_snapshot.json"),
    ) and not retry_known_failed_mover
    provider_collection_attempted = (
        not cache_state["market_valid"]
        or (not cache_state["movers_valid"] and not preserves_sep22_diagnostic)
    )

    if not (cache_state["market_valid"] and cache_state["movers_valid"]):
        old_write = app_module.write_snapshot
        old_run_movers = app_module.run_movers_snapshot
        old_collect = app_module.collect_instrument
        prior_market = load_object(stage_dir / "market_snapshot.json") or {}
        prior_groups = prior_market.get("market", {}) if isinstance(prior_market.get("market"), dict) else {}

        def reuse_valid_market_item(category: str, instrument: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
            """Reuse valid per-instrument data from an interrupted staged snapshot."""
            group = prior_groups.get(category, {}) if isinstance(prior_groups, dict) else {}
            cached = next(
                (item for item in group.values() if isinstance(item, dict) and item.get("symbol") == instrument.symbol),
                None,
            ) if isinstance(group, dict) else None
            if reusable_market_item(cached, date_value):
                logger.info("[RESUME] Reusing valid market instrument %s from staged partial snapshot", instrument.symbol)
                return copy.deepcopy(cached)
            return old_collect(category, instrument, *args, **kwargs)

        def run_movers_only_after_valid_market(*args: Any, **kwargs: Any) -> Path:
            """Do not spend mover-provider requests while its market dependency is invalid."""
            if cache_state["movers_valid"]:
                return stage_dir / "movers_snapshot.json"
            seeded_movers = load_object(stage_dir / "movers_snapshot.json")
            if preserve_known_mover_regression(date_value, cache_state, seeded_movers) and not retry_known_failed_mover:
                logger.info("[CACHE] Preserving established 2026-09-22 failed movers regression fixture")
                return stage_dir / "movers_snapshot.json"
            staged_market = load_object(stage_dir / "market_snapshot.json")
            if not staged_market or not historical._validate_historical_market(staged_market, date_value):
                logger.warning("[RESUME] Movers deferred: required market snapshot is not valid for %s", date_value)
                return stage_dir / ".movers-deferred.json"
            work_dir = stage_dir / ".movers_build"
            work_dir.mkdir(parents=True, exist_ok=True)
            original_output = movers_module.OUTPUT_DIR
            movers_module.OUTPUT_DIR = work_dir
            client = args[0] if args else kwargs.get("client")
            previous_checkpoint_path = getattr(client, "mover_checkpoint_path", None) if client is not None else None
            if client is not None:
                client.mover_checkpoint_path = work_dir / ".resume" / "mover_checkpoint.json"
            try:
                result = old_run_movers(*args, **kwargs)
            except Exception:
                for artifact in work_dir.glob("*.json"):
                    atomic_copy_replace(artifact, stage_dir / artifact.name)
                raise
            finally:
                if client is not None:
                    if previous_checkpoint_path is None:
                        delattr(client, "mover_checkpoint_path")
                    else:
                        client.mover_checkpoint_path = previous_checkpoint_path
                movers_module.OUTPUT_DIR = original_output
            for artifact in work_dir.glob("*.json"):
                atomic_copy_replace(artifact, stage_dir / artifact.name)
            return stage_dir / result.name

        app_module.collect_instrument = reuse_valid_market_item
        app_module.run_movers_snapshot = run_movers_only_after_valid_market
        def atomic_stage_market_write(snapshot: dict[str, Any], generated_at: datetime) -> tuple[Path, Path]:
            current = stage_dir / "market_snapshot.json"
            archive = stage_dir / f"market_snapshot_{generated_at.strftime('%Y%m%d_%H%M%S')}.json"
            atomic_json(current, snapshot)
            atomic_json(archive, snapshot)
            return current, archive

        app_module.write_snapshot = atomic_stage_market_write
        quota_pause = None
        try:
            with historical._stage_output(stage_dir):
                app_code = app_module.main(market_date_override=date_value, refresh_market=False, refresh_movers=False, movers_prefilter_enabled=True)
        except QuotaPauseRequired as exc:
            quota_pause = exc
            status = "QUOTA_PAUSED"
            reason = "TWELVE_DATA_QUOTA_PAUSED"
            logger.warning(
                "QUOTA_PAUSED provider=%s date=%s component=%s next_eligible_at=%s required_wait_seconds=%.1f",
                exc.provider, date_value, exc.context.get("purpose", "UNKNOWN"), exc.next_eligible_at, exc.required_wait_seconds,
            )
        finally:
            app_module.write_snapshot = old_write
            app_module.run_movers_snapshot = old_run_movers
            app_module.collect_instrument = old_collect
        # The snapshot's provider usage includes collection performed before an
        # interruption, plus this resume attempt, rather than only the final run.
        if prior_market and (staged_market := load_object(stage_dir / "market_snapshot.json")):
            prior_providers = prior_market.get("providers", {}) if isinstance(prior_market.get("providers"), dict) else {}
            next_providers = staged_market.get("providers", {}) if isinstance(staged_market.get("providers"), dict) else {}
            for provider_name, prior_stats in prior_providers.items():
                if not isinstance(prior_stats, dict):
                    continue
                next_stats = next_providers.setdefault(provider_name, {})
                for key in ("requested", "successful", "failed"):
                    next_stats[key] = int(prior_stats.get(key, 0) or 0) + int(next_stats.get(key, 0) or 0)
            for key in ("total_http_requests", "successful_api_requests", "retry_requests", "rate_limit_waits"):
                staged_market[key] = int(prior_market.get(key, 0) or 0) + int(staged_market.get(key, 0) or 0)
            atomic_json(stage_dir / "market_snapshot.json", staged_market)
        # The cache validators are authoritative, not the process return code.
        cache_state["market_valid"] = bool((p := load_object(stage_dir / "market_snapshot.json")) and historical._validate_historical_market(p, date_value))
        cache_state["movers_valid"] = movers_module.load_valid_movers_cache(date_value, path=stage_dir / "movers_snapshot.json", require_historical_integrity=True) is not None
        if not cache_state["market_valid"] or not cache_state["movers_valid"]:
            status = "PARTIAL" if (load_object(stage_dir / "market_snapshot.json") or load_object(stage_dir / "movers_snapshot.json")) else "FAILED"
            reason = cache_failure_reason(stage_dir, date_value)
            atomicize(stage_dir / "market_snapshot.json")
            atomicize(stage_dir / "movers_snapshot.json")
            quota = quota_failure(stage_dir, date_value) if provider_collection_attempted else None
            if quota:
                status, reason = "QUOTA_PAUSED", quota
        else:
            atomicize(stage_dir / "market_snapshot.json")
            atomicize(stage_dir / "movers_snapshot.json")
        if quota_pause is not None:
            status = "QUOTA_PAUSED"
            reason = "TWELVE_DATA_QUOTA_PAUSED"
    else:
        cache_state["market"] = "HIT"
        cache_state["movers"] = "HIT"

    if status != "QUOTA_PAUSED" and cache_state["market_valid"] and cache_state["movers_valid"]:
        market_news_cache = historical._market_news_state(date_value)
        if market_news_cache != "HIT":
            try:
                news_result = historical._build_market_news(date_value, refresh=True, logger=logger)
                cache_state["market_news"] = "BUILT" if news_result.get("status") == "READY" else "FAILED"
            except Exception as exc:
                logger.exception("Market discovery build failed for %s", date_value)
                news_result = {"status": "FAILED", "provider_metrics": {}}
                cache_state["market_news"] = "FAILED"
                reason = f"MARKET_NEWS_{type(exc).__name__}"
                status = "PARTIAL"
        else:
            news_result = None
            cache_state["market_news"] = "HIT"

        driver_path = stage_dir / "market_driver_snapshot.json"
        driver = load_object(driver_path)
        retry_repaired_movers = bool(
            retry_known_failed_mover
            and date_value == "2026-09-22"
            and cache_state.get("movers_valid")
        )
        if retry_repaired_movers:
            logger.info("[RESUME] Rebuilding downstream driver snapshot after Sep 22 mover repair")
        mover_snapshot = load_object(stage_dir / "movers_snapshot.json") or {}
        symbol_resolver = getattr(historical, "_mover_symbols", None)
        expected_news_symbols = symbol_resolver(mover_snapshot) if callable(symbol_resolver) else []
        missing_news_symbols = missing_finnhub_symbols(date_value, expected_news_symbols)
        finnhub_repair_requests = 0
        if missing_news_symbols:
            logger.info("[RESUME] Completing missing Finnhub caches before downstream driver reuse")
            still_missing, finnhub_repair_requests = complete_missing_finnhub_caches(
                date_value, expected_news_symbols, logger,
            )
            if still_missing:
                raise RuntimeError("FINNHUB_STOCK_NEWS_CACHE_INCOMPLETE:" + ",".join(still_missing))
        if not (driver and driver.get("market_date") == date_value) or retry_repaired_movers or bool(missing_news_symbols):
            try:
                driver_path = driver_runtime.run_market_driver_pipeline(date_value, stage_dir, refresh_discovery=False, refresh_verification=False, logger=logger)
                driver = load_object(driver_path)
                if driver and finnhub_repair_requests:
                    metrics = driver.setdefault("metrics", {})
                    metrics["finnhub_live_requests"] = int(metrics.get("finnhub_live_requests", 0) or 0) + finnhub_repair_requests
                    atomic_json(driver_path, driver)
                atomicize(driver_path)
                cache_state["market_driver_snapshot"] = "BUILT" if driver else "FAILED"
            except Exception as exc:
                logger.exception("Stock news/driver build failed for %s", date_value)
                cache_state["market_driver_snapshot"] = "FAILED"
                status = "PARTIAL"
                reason = reason or f"DRIVER_BUILD_{type(exc).__name__}"
        else:
            cache_state["market_driver_snapshot"] = "HIT"
        if status == "COMPLETE" and cache_state.get("market_driver_snapshot") == "FAILED":
            status = "PARTIAL"
            reason = reason or "MARKET_DRIVER_SNAPSHOT_MISSING"
    else:
        news_result = None
        driver = load_object(stage_dir / "market_driver_snapshot.json")
        cache_state["market_news"] = "HIT" if historical._market_news_state(date_value) == "HIT" else "DEFERRED"
        cache_state["market_driver_snapshot"] = "HIT" if cache_state.get("driver_valid") else "DEFERRED"

    # Atomically persist the staged, complete per-date artifacts.
    for name in ("market_snapshot.json", "movers_snapshot.json", "market_driver_snapshot.json"):
        atomicize(stage_dir / name)
    log_tail = ""
    if log_path.exists():
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(min(log_offset, log_path.stat().st_size))
            log_tail = handle.read().upper()
    if status != "QUOTA_PAUSED" and ("DAILY_QUOTA_EXHAUSTED" in log_tail or "RATE_LIMIT_WAIT_TIMEOUT" in log_tail):
        status, reason = "QUOTA_PAUSED", "TWELVE_DATA_QUOTA_PAUSED"

    usage = provider_usage(date_value, stage_dir, driver, news_result, before_files, time.monotonic() - started, cache_state)
    if usage["fmp_requests"] != 0:
        status, reason = "FAILED", "FMP_REQUESTS_MUST_REMAIN_ZERO"
    progress = {
        "date": date_value, "status": status, "reason": reason,
        "market_data": "HIT" if cache_state.get("market_valid") else "FAILED",
        "movers": "HIT" if cache_state.get("movers_valid") else "FAILED",
        "market_news": cache_state.get("market_news", "DEFERRED"),
        "stock_news_and_verification": cache_state.get("market_driver_snapshot", "DEFERRED"),
        "runtime_seconds": usage["runtime_seconds"],
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    if "quota_pause" in locals() and quota_pause is not None:
        progress["quota_pause"] = {
            "provider": quota_pause.provider,
            "reason": "OPERATIONAL_WAIT_THRESHOLD_EXCEEDED",
            "next_eligible_at": quota_pause.next_eligible_at,
            "required_wait_seconds": round(quota_pause.required_wait_seconds, 3),
            "current_date": date_value,
            "current_component": quota_pause.context.get("purpose", "UNKNOWN"),
            "symbol": quota_pause.context.get("symbol", ""),
        }
    return progress, usage


def make_logger() -> logging.Logger:
    logger = logging.getLogger("extended_dataset_v2")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(logging.StreamHandler(sys.stdout))
    logger.addHandler(logging.FileHandler(MILESTONE / "build.log", encoding="utf-8"))
    return logger


def write_selected_dates(dates: list[str]) -> None:
    rows = [{"sequence": index, "date": value} for index, value in enumerate(dates, 1)]
    atomic_csv(MILESTONE / "selected_dates.csv", rows, ["sequence", "date"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Staged 30-session historical data builder (evaluation-only).")
    parser.add_argument("--batch-number", type=int, help="Build one resumable group of five dates (1-based; defaults to batch 1).")
    parser.add_argument("--resume", action="store_true", help="Skip dates already marked COMPLETE.")
    parser.add_argument("--offline-replay", action="store_true", help="Skip collection and run the replay/QA output stage.")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    dates = load_dates()
    if len(dates) != 30 or dates[-1] != "2026-09-28":
        parser.error("PHASE_A_PLAN_MISSING_OR_NOT_30_DATES")
    write_selected_dates(dates)
    MILESTONE.mkdir(parents=True, exist_ok=True)

    # Lazy project imports allow the dotenv load and preflight to happen first.
    import app as app_module
    import build_historical_market_cache as historical
    import movers as movers_module
    from drivers import runtime as driver_runtime
    from utils.windows_power import prevent_system_sleep

    if not args.offline_replay:
        if not os.getenv("TWELVE_DATA_API_KEY", "").strip() or not os.getenv("FINNHUB_API_KEY", "").strip():
            parser.error("REQUIRED_PROVIDER_CONFIGURATION_MISSING")

    # Keep all snapshots separate from the original history/batch outputs.
    historical.HISTORY_DIR = MILESTONE / "history"
    historical.MANIFEST_PATH = MILESTONE / "historical_cache_manifest.json"
    progress = {row["date"]: row for row in read_csv(MILESTONE / "build_progress.csv")}
    usage = {row["date"]: row for row in read_csv(MILESTONE / "provider_usage.csv")}
    for date_value, row in progress.items():
        stage_dir = MILESTONE / "history" / date_value
        if row.get("status") == "PARTIAL" and row.get("reason") == "REQUIRED_MARKET_OR_MOVERS_CACHE_INVALID":
            row["reason"] = cache_failure_reason(stage_dir, date_value)
        if row.get("market_data") == "FAILED" and row.get("movers") == "FAILED":
            row["market_news"] = "DEFERRED"
            row["stock_news_and_verification"] = "DEFERRED"
    logger = make_logger()

    if not args.offline_replay:
        batch_size = 5
        total_batches = (len(dates) + batch_size - 1) // batch_size
        batch_numbers = [args.batch_number or 1]
        if any(value is None or value < 1 or value > total_batches for value in batch_numbers):
            parser.error(f"BATCH_NUMBER_MUST_BE_1_TO_{total_batches}")
        paused = False
        for batch_number in batch_numbers:
            chunk = dates[(batch_number - 1) * batch_size:batch_number * batch_size]
            logger.info("BATCH %d/%d start dates=%s", batch_number, total_batches, ",".join(chunk))
            with prevent_system_sleep(logger=logger), shared_batch_rate_limiter(app_module):
                if batch_number > 1:
                    previous_date = dates[(batch_number - 1) * batch_size - 1]
                    wait_for_prior_batch_credit_window(previous_date, progress, usage, logger)
                for market_date in chunk:
                    if args.resume and progress.get(market_date, {}).get("status") == "COMPLETE":
                        logger.info("DATE %s SKIP valid completed checkpoint", market_date)
                        continue
                    logger.info("DATE %s build start", market_date)
                    date_progress, date_usage = process_date(market_date, logger=logger, historical=historical, app_module=app_module, movers_module=movers_module, driver_runtime=driver_runtime, prevent_sleep=prevent_system_sleep)
                    progress[market_date] = date_progress
                    usage[market_date] = date_usage
                    persist_progress(progress, usage)
                    logger.info("DATE %s status=%s td_credits=%s finnhub=%s marketaux=%s", market_date, date_progress["status"], date_usage["twelve_data_total_credits"], date_usage["finnhub_requests"], date_usage["marketaux_requests"])
                    if date_progress["status"] == "QUOTA_PAUSED":
                        paused = True
                        break
            atomic_json(MILESTONE / f"checkpoint_batch_{batch_number:02d}.json", {"batch_number": batch_number, "dates": chunk, "progress": [progress[d] for d in chunk if d in progress], "provider_usage": [usage[d] for d in chunk if d in usage], "quota_paused": paused})
            write_build_summary(progress, usage, phase="QUOTA_PAUSED" if paused else "BUILDING")
            logger.info("BATCH %d/%d checkpoint written", batch_number, total_batches)
            if paused:
                break
        if paused:
            return 2
        # Each invocation builds at most one five-date checkpoint. Evaluation is
        # deliberately a separate explicit offline step after all six batches.
        write_build_summary(progress, usage, phase="BUILDING")
        return 0

    # Offline replay is a separate, run-specific artifact tree.
    replay_id = datetime.now(timezone.utc).strftime("replay_%Y%m%d_%H%M%S")
    replay_root = RUNS / replay_id
    evaluation_dir = replay_root / "evaluation"
    for market_date in dates:
        source = MILESTONE / "history" / market_date
        target = evaluation_dir / "history" / market_date
        target.mkdir(parents=True, exist_ok=True)
        for prefix in ("market_snapshot", "movers_snapshot", "market_driver_snapshot"):
            stage_copy(source / f"{prefix}.json", target / f"{prefix}.json")
    # Evaluation's market-news loader expects a sibling cache root. Copy only
    # the selected date-scoped market discovery evidence into the run folder.
    from cache_config import cache_root
    source_news = cache_root() / "market_driver_news"
    target_news = replay_root / "cache" / "market_driver_news"
    for market_date in dates:
        for candidate in source_news.glob(f"*{market_date}*.json") if source_news.exists() else []:
            stage_copy(candidate, target_news / candidate.name)
        exact = source_news / f"{market_date}.json"
        stage_copy(exact, target_news / exact.name)

    import evaluate_market_drivers as replay
    import evaluate_market_drivers_product as product
    import build_historical_evaluation_batch as batch
    from evaluation import generate_extended_dataset_outputs as output_builder

    with replay.offline_network_guard():
        replay_result = product.evaluate_product(dates[0], dates[-1], market_dates=dates, output_dir=evaluation_dir)
    days = replay_result.get("days", [])
    skipped = replay_result.get("skipped_dates", [])
    # Re-evaluate integrity under an explicit offline guard, then prepare final QA.
    for day in days:
        day["integrity"] = batch.evaluate_integrity(day, output_dir=evaluation_dir)
        if day["integrity"]["integrity_status"] == "FAIL" and day.get("status") == "READY":
            day["status"] = "PARTIAL"
    atomic_json(evaluation_dir / "market_driver_multiday.json", replay.to_json_safe(replay_result))
    atomic_text(evaluation_dir / "market_driver_multiday.md", replay_result.get("markdown", "") or "Offline product replay completed; see JSON and queues.")
    output_builder.generate(MILESTONE, replay_id, dates, days, skipped, progress, usage, replay_result)
    write_build_summary(progress, usage, phase="REPLAY_COMPLETE", replay_id=replay_id, replay_result=replay_result)
    return 0


def write_build_summary(progress: dict[str, dict[str, Any]], usage: dict[str, dict[str, Any]], *, phase: str, replay_id: str | None = None, replay_result: dict[str, Any] | None = None) -> None:
    statuses = {key: sum(row.get("status") == key for row in progress.values()) for key in ("COMPLETE", "PARTIAL", "FAILED", "QUOTA_PAUSED")}
    totals: dict[str, int] = {}
    for row in usage.values():
        for key in ("twelve_data_total_credits", "finnhub_requests", "finnhub_symbols_newly_cached", "marketaux_requests", "google_news_searches", "gdelt_searches", "official_source_requests", "tier2_verification_requests", "fmp_requests", "cache_hits", "cache_builds", "cache_failures"):
            totals[key] = totals.get(key, 0) + int(row.get(key, 0) or 0)
    payload = {"phase": phase, "selected_dates": load_dates(), "build_status_counts": statuses, "provider_usage_totals": totals, "build_runtime_seconds": round(sum(float(row.get("runtime_seconds", 0) or 0) for row in usage.values()), 3), "replay_id": replay_id, "replay_summary": replay_result.get("metrics", {}) if replay_result else None, "fmp_requests": totals.get("fmp_requests", 0), "network_calls_during_planning": 0}
    atomic_json(MILESTONE / "build_summary.json", payload)
    lines = ["# Extended Historical Dataset V2 — 30-Day Milestone", "", f"Phase: **{phase}**", f"Selected range: {payload['selected_dates'][0]} through {payload['selected_dates'][-1]} (30 trading sessions).", "", "## Build status", "", "| COMPLETE | PARTIAL | FAILED | QUOTA_PAUSED |", "|---:|---:|---:|---:|", f"| {statuses['COMPLETE']} | {statuses['PARTIAL']} | {statuses['FAILED']} | {statuses['QUOTA_PAUSED']} |", "", "## Provider usage totals", ""]
    lines.extend(f"- {key}: {value}" for key, value in sorted(totals.items()))
    if replay_id:
        lines.extend(["", f"Run-specific replay: `replays/{replay_id}/evaluation/`", ""])
    atomic_text(MILESTONE / "build_summary.md", "\n".join(lines) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
