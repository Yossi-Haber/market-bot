"""Build date-scoped historical inputs for offline Market Driver evaluation.

The builder intentionally stages every date under ``output/history`` and never
uses the current production snapshot paths.  It reuses the production market,
movers, Finnhub, verification, and Market Driver runtime components.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import app
import movers
from drivers.runtime import run_market_driver_pipeline
from drivers.cross_asset import build_cross_asset_hypotheses
from drivers.market_driver_discovery import (
    MARKET_PROVIDER_ORDER,
    build_market_driver_queries,
    build_market_discovery_result,
    market_discovery_cache_path,
    save_market_discovery_cache,
)
from evaluate_market_drivers import _dated_snapshot
from cache_config import cache_root
from market_date import select_as_of_bars
from news.verification_acquisition import CACHE_DIR as VERIFICATION_CACHE_DIR
from news.marketaux import MarketauxDiscovery
from news.gdelt import GdeltClient
from news.google_news import GoogleNewsClient


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
HISTORY_DIR = OUTPUT_DIR / "history"
MANIFEST_PATH = HISTORY_DIR / "historical_cache_manifest.json"
DEFAULT_MAX_DAYS = 10
NEW_YORK = ZoneInfo("America/New_York")
US_MARKET_CLOSE = datetime_time(16, 0)


def _atomic_json(path: Path, payload: Any) -> None:
    """Atomically write UTF-8 JSON and preserve an existing complete file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _history_path(market_date: str) -> Path:
    return HISTORY_DIR / market_date


def requested_dates(*, market_date: str | None = None, from_date: str | None = None, to_date: str | None = None, market_dates: str | None = None) -> list[str]:
    if market_date:
        values = [market_date]
    elif market_dates:
        values = [item.strip() for item in market_dates.split(",") if item.strip()]
    elif from_date and to_date:
        start, end = date.fromisoformat(from_date), date.fromisoformat(to_date)
        if end < start:
            raise ValueError("DATE_RANGE_REVERSED")
        values = []
        current = start
        while current <= end:
            values.append(current.isoformat())
            current += timedelta(days=1)
    else:
        raise ValueError("DATE_ARGUMENT_REQUIRED")
    # Validate and deduplicate without changing explicit order.
    result: list[str] = []
    for value in values:
        normalized = date.fromisoformat(value).isoformat()
        if normalized not in result:
            result.append(normalized)
    return result


def trading_day_status(
    market_date: str,
    *,
    now: datetime | None = None,
    allow_incomplete: bool = False,
) -> str:
    """Classify a date without treating an open U.S. session as historical.

    ``now`` is injectable so callers and tests can make the session boundary
    deterministic.  Only the current New York calendar date can be marked
    incomplete; prior weekdays remain historical trading days.
    """
    parsed = date.fromisoformat(market_date)
    if parsed.weekday() >= 5:
        return "NON_TRADING_DAY"
    current = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    if not allow_incomplete and parsed == current.date() and current.time() < US_MARKET_CLOSE:
        return "INCOMPLETE_TRADING_DAY"
    return "TRADING_DAY"


def _load(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _same_date_snapshot(directory: Path, prefix: str, market_date: str) -> dict[str, Any] | None:
    current = _load(directory / f"{prefix}.json")
    if current and current.get("market_date") == market_date:
        return current
    dated = _dated_snapshot(directory, prefix, market_date)
    return dated


def _snapshot_path(prefix: str, market_date: str) -> Path | None:
    """Find a same-date artifact in history first, then existing root history."""
    candidates = [_history_path(market_date) / f"{prefix}.json", OUTPUT_DIR / f"{prefix}.json"]
    candidates.extend(path for path in OUTPUT_DIR.glob(f"{prefix}_*.json"))
    valid = [path for path in candidates if path.is_file() and (_load(path) or {}).get("market_date") == market_date]
    return max(valid, key=lambda path: path.stat().st_mtime) if valid else None


def _mover_symbols(movers_snapshot: dict[str, Any] | None) -> list[str]:
    if not movers_snapshot:
        return []
    return list(dict.fromkeys(
        str(item.get("symbol", "")).upper()
        for group in ("top_gainers", "top_losers")
        for item in movers_snapshot.get(group, [])
        if isinstance(item, dict) and item.get("symbol")
    ))


def _finnhub_cache_status(market_date: str, symbols: list[str]) -> tuple[int, int]:
    end = (date.fromisoformat(market_date) + timedelta(days=1)).isoformat()
    root = BASE_DIR / "cache" / "finnhub"
    hits = sum(1 for symbol in symbols if (root / symbol / f"{market_date}_{end}.json").is_file())
    return hits, len(symbols)


def _verification_ready(market_date: str) -> bool:
    roots = (BASE_DIR / "cache" / "verification", BASE_DIR / "cache" / "verification_v2", BASE_DIR / "cache" / "official_news")
    # Verification artifacts are date-scoped.  A cache from another day must
    # never make this day's offline plan appear ready.
    for root in roots:
        dated_root = root / market_date
        if dated_root.exists() and any(path.is_file() for path in dated_root.rglob("*")):
            return True
    return False


def _finnhub_state(hits: int, total: int) -> str:
    """Return coverage state; zero total means symbols are not known."""
    if total <= 0:
        return "DEFERRED"
    if hits <= 0:
        return "MISS"
    if hits >= total:
        return "HIT"
    return "PARTIAL"


def _market_news_state(market_date: str) -> str:
    path = market_discovery_cache_path(cache_root(), market_date)
    payload = _load(path)
    if not payload:
        return "MISS"
    if payload.get("market_date") != market_date or not payload.get("offline_ready") or not payload.get("discovery_completed"):
        return "FAILED"
    if payload.get("point_in_time_status") != "PASS":
        return "FAILED"
    return "HIT"


def _load_historical_movers(market_date: str, path: Path) -> dict[str, Any] | None:
    """Load movers with the historical integrity requirement enabled."""
    try:
        return movers.load_valid_movers_cache(market_date, path=path, require_historical_integrity=True)
    except TypeError as exc:
        if "require_historical_integrity" not in str(exc):
            raise
        return movers.load_valid_movers_cache(market_date, path=path)


def _validate_historical_market(snapshot: Any, market_date: str) -> bool:
    """Validate a historical market artifact, with test-double compatibility."""
    try:
        return bool(app.validate_market_snapshot_cache(snapshot, market_date, require_historical_integrity=True)["valid"])
    except TypeError as exc:
        if "require_historical_integrity" not in str(exc):
            raise
        return bool(app.validate_market_snapshot_cache(snapshot, market_date)["valid"])


def inspect_date(market_date: str, *, now: datetime | None = None, allow_incomplete: bool = False) -> dict[str, Any]:
    directory = _history_path(market_date)
    market_path = _snapshot_path("market_snapshot", market_date)
    movers_path = _snapshot_path("movers_snapshot", market_date)
    market_candidate = _load(market_path) if market_path else None
    market = market_candidate if market_candidate and _validate_historical_market(market_candidate, market_date) else None
    movers_snapshot = _load_historical_movers(market_date, movers_path) if movers_path else None
    driver = _same_date_snapshot(directory, "market_driver_snapshot", market_date)
    symbols = _mover_symbols(movers_snapshot)
    finnhub_hits, finnhub_total = _finnhub_cache_status(market_date, symbols)
    movers_available = movers_snapshot is not None
    finnhub_state = _finnhub_state(finnhub_hits, finnhub_total) if movers_available else "DEFERRED"
    # Verification cannot be READY until the mover/event inputs exist.  A
    # date-scoped driver snapshot is the strongest offline-ready signal.
    verification = "DEFERRED"
    if movers_available and driver is not None:
        verification = "READY"
    elif movers_available and finnhub_state == "HIT" and _verification_ready(market_date):
        verification = "READY"
    elif movers_available and finnhub_state in {"MISS", "PARTIAL"}:
        verification = "DEFERRED"
    market_news = _market_news_state(market_date)
    actions: list[str] = []
    deferred: list[str] = []
    if market is None:
        actions.append("market")
    if movers_snapshot is None:
        actions.append("movers")
        deferred.extend(["news", "verification", "market_driver_snapshot"])
    elif finnhub_state != "HIT":
        actions.append("news")
    if verification != "READY" and "verification" not in deferred:
        actions.append("verification")
    if verification != "READY" and "market_driver_snapshot" not in deferred:
        actions.append("market_driver_snapshot")
    # Preserve order while avoiding duplicate actions/deferred states.
    actions = list(dict.fromkeys(actions))
    deferred = list(dict.fromkeys(deferred))
    plan_ready = not actions
    return {
        "requested_date": market_date,
        "market_date": market_date,
        "trading_day_status": trading_day_status(market_date, now=now, allow_incomplete=allow_incomplete),
        "market": "HIT" if market else "MISS",
        "movers": "HIT" if movers_snapshot else "MISS",
        "finnhub": f"{finnhub_hits}/{finnhub_total} {finnhub_state}" if movers_available else "DEFERRED",
        "verification": verification,
        "market_news": market_news,
        "actions_needed": ",".join(actions) if actions else "none",
        "deferred": ",".join(deferred) if deferred else "none",
        "plan_ready": plan_ready,
        "needs_build": not plan_ready,
        "market_snapshot": str(market_path) if market else None,
        "movers_snapshot": str(movers_path) if movers_snapshot else None,
        "market_driver_snapshot": str(directory / "market_driver_snapshot.json") if driver else None,
    }


@contextmanager
def _stage_output(directory: Path) -> Iterator[None]:
    """Redirect only output writers; restore module globals even on failure."""
    original_app, original_movers = app.OUTPUT_DIR, movers.OUTPUT_DIR
    app.OUTPUT_DIR = directory
    movers.OUTPUT_DIR = directory
    try:
        yield
    finally:
        app.OUTPUT_DIR = original_app
        movers.OUTPUT_DIR = original_movers


def _provider_metrics(directory: Path, driver: dict[str, Any]) -> dict[str, Any]:
    market = _load(directory / "market_snapshot.json") or {}
    movers_snapshot = _load(directory / "movers_snapshot.json") or {}
    runtime = driver.get("metrics", {}) if isinstance(driver, dict) else {}
    mover_stats = movers_snapshot.get("request_statistics", {}) if isinstance(movers_snapshot.get("request_statistics"), dict) else {}
    providers = market.get("providers", {})
    twelve = providers.get("Twelve Data", {}) if isinstance(providers.get("Twelve Data"), dict) else {}
    return {
        "market_requests": int(market.get("total_http_requests", 0) or 0),
        "twelve_data_requests": int(twelve.get("requested", mover_stats.get("twelve_data_requests", 0)) or 0),
        "finnhub_requests": int(runtime.get("finnhub_live_requests", 0) or 0),
        "marketaux_requests": int(runtime.get("marketaux_requests", 0) or 0),
        "regulator_requests": int(runtime.get("regulator_requests", 0) or 0),
        "official_requests": int(runtime.get("official_requests", 0) or 0),
        "tier2_requests": int(runtime.get("tier2_requests", 0) or 0),
        "fmp_requests": int(runtime.get("fmp_requests", 0) or 0),
    }


def _json_safe_market_story(value: Any) -> dict[str, Any]:
    if hasattr(value, "as_dict"):
        value = value.as_dict()
    if not isinstance(value, dict):
        return {}
    result = dict(value)
    for key, item in list(result.items()):
        if isinstance(item, (datetime, date)):
            result[key] = item.isoformat()
    result.setdefault("source_name", result.get("source_domain") or result.get("discovery_source"))
    result.setdefault("source_url", result.get("url"))
    return result


def _market_news_story_inputs(items: list[Any], provider: str) -> list[dict[str, Any]]:
    result = []
    for item in items:
        story = _json_safe_market_story(item)
        if story:
            story["provider"] = provider
            result.append(story)
    return result


def _build_market_news(market_date: str, *, refresh: bool, logger: logging.Logger) -> dict[str, Any]:
    """Run bounded broad-market discovery and write the dedicated cache."""
    market_path = _snapshot_path("market_snapshot", market_date)
    market_snapshot = _load(market_path) if market_path else None
    if not market_snapshot:
        return {"market_date": market_date, "status": "FAILED", "reason": "MARKET_SNAPSHOT_MISSING"}
    market = market_snapshot.get("market", {})
    report = {"market_date": market_date, "market": market, "macro": market.get("macro", {}), "sectors": market.get("sectors", {})}
    hypotheses = build_cross_asset_hypotheses(report)
    start = datetime.combine(date.fromisoformat(market_date) - timedelta(days=1), datetime_time.min, tzinfo=timezone.utc)
    end = datetime.combine(date.fromisoformat(market_date) + timedelta(days=1), datetime_time.min, tzinfo=timezone.utc)
    marketaux = MarketauxDiscovery(logger, market_date, max_requests=6, refresh_cache=refresh)
    gdelt = GdeltClient(logger, start, end)
    google = GoogleNewsClient(logger, start, end)
    hypothesis_results: list[dict[str, Any]] = []
    all_queries: list[dict[str, Any]] = []
    metrics = {"marketaux_requests": 0, "google_news_requests": 0, "gdelt_requests": 0, "finnhub_requests": 0, "official_requests": 0, "fmp_requests": 0}

    def run_queries(descriptors: list[dict[str, str]], phase_name: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        raw: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []
        attempted: list[str] = []
        for descriptor in descriptors:
            query = descriptor["query"]
            for provider in MARKET_PROVIDER_ORDER:
                provider_items: list[dict[str, Any]] = []
                provider_status = "not_attempted"
                if provider == "marketaux" and marketaux.enabled and marketaux.available_for_run:
                    attempted.append(provider)
                    provider_items = _market_news_story_inputs(marketaux.search(query=query, query_type="GENERAL"), provider)
                    metrics["marketaux_requests"] = marketaux.request_count
                    provider_status = "searched"
                elif provider == "google_news":
                    attempted.append(provider)
                    response = google.search(query, 10)
                    provider_items = _market_news_story_inputs(response.get("candidates", []) if isinstance(response, dict) else [], provider)
                    metrics["google_news_requests"] = google.request_count
                    provider_status = response.get("status", "failed") if isinstance(response, dict) else "failed"
                elif provider == "gdelt":
                    attempted.append(provider)
                    response = gdelt.search(query, 10)
                    provider_items = _market_news_story_inputs(response.get("candidates", []) if isinstance(response, dict) else [], provider)
                    metrics["gdelt_requests"] = gdelt.request_count
                    provider_status = response.get("status", "failed") if isinstance(response, dict) else "failed"
                raw.extend(provider_items)
                records.append({"provider": provider, "query": query, "query_phase": phase_name, "status": provider_status, "raw_results": len(provider_items)})
        return raw, records, list(dict.fromkeys(attempted))

    for hypothesis in hypotheses:
        query_plan = build_market_driver_queries(hypothesis, market_date, provider="market-discovery")
        raw_stories, query_records, providers_attempted = run_queries(query_plan, "PRIMARY")
        result = build_market_discovery_result(hypothesis, raw_stories, market_date, queries_generated=query_plan, providers_attempted=providers_attempted, phase=1)
        if result["metrics"]["eligible_count"] == 0:
            fallback_plan = build_market_driver_queries(hypothesis, market_date, provider="market-discovery", phase=2)
            fallback_raw, fallback_records, fallback_providers = run_queries(fallback_plan, "FALLBACK")
            if fallback_raw:
                raw_stories.extend(fallback_raw)
                query_records.extend(fallback_records)
                providers_attempted = list(dict.fromkeys(providers_attempted + fallback_providers))
                result = build_market_discovery_result(hypothesis, raw_stories, market_date, queries_generated=query_plan + fallback_plan, providers_attempted=providers_attempted, phase=2)
                result["fallback_used"] = True
            else:
                result["fallback_used"] = True
        result["query_records"] = query_records
        result["raw_results"] = len(raw_stories)
        result["filtered_results"] = result["candidates_after_market_filter"]
        result["eligible_results"] = result["metrics"]["eligible_count"]
        hypothesis_results.append(result)
        all_queries.extend(query_records)
    eligible = [item for result in hypothesis_results for item in result.get("market_eligible_candidates", [])]
    top_candidates = [item for result in hypothesis_results for item in result.get("top_candidates", [])][:5]
    rejected = [item for result in hypothesis_results for item in result.get("rejected_candidates", [])][:20]
    payload = {
        "market_date": market_date,
        "requested_market_date": market_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hypotheses": hypothesis_results,
        "queries": all_queries,
        "provider_metrics": metrics,
        "raw_candidate_count": sum(item.get("candidates_found", 0) for item in hypothesis_results),
        "filtered_candidate_count": sum(item.get("candidates_after_market_filter", 0) for item in hypothesis_results),
        "eligible_candidate_count": len(eligible),
        "market_eligible_candidates": eligible,
        "clusters": [item for result in hypothesis_results for item in result.get("top_candidates", [])],
        "top_candidates": top_candidates,
        "rejections": rejected,
        "offline_ready": True,
        "point_in_time_status": "PASS" if not any(item.get("point_in_time_status") == "FAIL" for candidate in eligible for item in [candidate]) else "FAIL",
        "discovery_completed": True,
        "fmp_requests": 0,
    }
    path = save_market_discovery_cache(cache_root(), market_date, payload)
    logger.info("Historical market discovery cache created: %s eligible=%s raw=%s", path, len(eligible), payload["raw_candidate_count"])
    return {"market_date": market_date, "status": "READY", "market_news": "HIT", "cache_path": str(path), "payload": payload}


def _print_market_news_diagnostics(result: dict[str, Any]) -> None:
    payload = result.get("payload", {})
    print(f"MARKET NEWS: date={result.get('market_date')} status={result.get('status')} cache={result.get('cache_path')}")
    for hypothesis in payload.get("hypotheses", []):
        print(f"HYPOTHESIS {hypothesis.get('hypothesis_id')} queries_generated={len(hypothesis.get('queries_generated', []))}")
        for query in hypothesis.get("query_records", []):
            print(f"  provider={query.get('provider')} phase={query.get('query_phase')} raw_results={query.get('raw_results', 0)} status={query.get('status')}")
        print("  TOP 5 candidates:")
        for candidate in hypothesis.get("top_candidates", [])[:5]:
            print(f"    {candidate.get('title', '')} | source={candidate.get('source_name')} | published_at={candidate.get('published_at')} | subject={candidate.get('market_subject_score')} match={candidate.get('hypothesis_match_score')} causal={candidate.get('causal_link_type')} timing={candidate.get('timing_relation')} gate={candidate.get('causal_gate')} eligible={candidate.get('causal_eligible', False)}")
        print("  TOP 5 rejected:")
        for candidate in hypothesis.get("rejected_candidates", [])[:5]:
            print(f"    {candidate.get('title', '')} | reason={candidate.get('rejection_reason')}")
    print(f"eligible_candidate_count={payload.get('eligible_candidate_count', 0)}")
    print(f"selected_candidate={(payload.get('top_candidates') or [{}])[0].get('title') if payload.get('eligible_candidate_count', 0) else 'NONE'}")
    print(f"market_driver_status={'PLAUSIBLE_NOT_VERIFIED' if payload.get('eligible_candidate_count', 0) else 'HYPOTHESIS_ONLY'}")
    print(f"FMP requests={payload.get('fmp_requests', 0)}")


def _build_day(market_date: str, *, refresh_market: bool, refresh_movers: bool, refresh_news: bool, refresh_verification: bool, logger: logging.Logger) -> dict[str, Any]:
    directory = _history_path(market_date)
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    # Seed the date-scoped workspace from an already validated root artifact;
    # app.main then reuses it without ever writing back to the root path.
    for prefix in ("market_snapshot", "movers_snapshot"):
        target = directory / f"{prefix}.json"
        source = _snapshot_path(prefix, market_date)
        if not target.exists() and source and source.resolve() != target.resolve():
            if prefix == "market_snapshot" and not _validate_historical_market(_load(source), market_date):
                continue
            if prefix == "movers_snapshot" and _load_historical_movers(market_date, source) is None:
                continue
            shutil.copy2(source, target)
    with _stage_output(directory):
        code = app.main(market_date_override=market_date, refresh_movers=refresh_movers, refresh_market=refresh_market, movers_prefilter_enabled=True)
    if code != 0:
        return {"market_date": market_date, "status": "FAILED", "missing_components": ["market_or_movers"], "failed_components": ["market_collection"], "errors": [f"APP_EXIT_{code}"], "provider_metrics": {}, "elapsed_seconds": round(time.monotonic() - started, 3)}
    market = _load(directory / "market_snapshot.json")
    movers_snapshot = _load(directory / "movers_snapshot.json")
    if not market or not movers_snapshot:
        return {"market_date": market_date, "status": "FAILED", "missing_components": [name for name, value in (("market_snapshot", market), ("movers_snapshot", movers_snapshot)) if not value], "failed_components": ["required_artifacts"], "errors": ["REQUIRED_ARTIFACT_MISSING"], "provider_metrics": {}, "elapsed_seconds": round(time.monotonic() - started, 3)}
    market_integrity = market.get("historical_integrity", {})
    movers_integrity = movers_snapshot.get("historical_integrity", {})
    if market_integrity.get("integrity_status") != "PASS" or movers_integrity.get("integrity_status") != "PASS":
        logger.error("Historical integrity failed for %s", market_date)
        return {"market_date": market_date, "status": "FAILED", "missing_components": ["historical_integrity"], "failed_components": ["historical_integrity"], "errors": ["HISTORICAL_FUTURE_DATA_LEAK"], "provider_metrics": {}, "elapsed_seconds": round(time.monotonic() - started, 3)}
    try:
        driver_path = run_market_driver_pipeline(market_date, directory, refresh_verification=refresh_verification, refresh_discovery=refresh_news, logger=logger)
        driver = _load(driver_path) or {}
    except Exception as exc:  # per-date failure must not abort a range
        logger.exception("Historical Market Driver failed for %s", market_date)
        return {"market_date": market_date, "status": "PARTIAL", "missing_components": ["market_driver_snapshot"], "failed_components": ["market_drivers"], "errors": [type(exc).__name__], "provider_metrics": {}, "elapsed_seconds": round(time.monotonic() - started, 3)}
    metrics = _provider_metrics(directory, driver)
    status = "READY" if driver and driver.get("market_date") == market_date else "PARTIAL"
    return {"market_date": market_date, "status": status, "market_snapshot": str(directory / "market_snapshot.json"), "movers_snapshot": str(directory / "movers_snapshot.json"), "finnhub_symbols_cached": _mover_symbols(movers_snapshot), "verification_cache_ready": bool(driver), "market_driver_snapshot": str(directory / "market_driver_snapshot.json") if driver else None, "missing_components": [] if status == "READY" else ["market_driver_snapshot"], "failed_components": [], "errors": [], "provider_metrics": metrics, "elapsed_seconds": round(time.monotonic() - started, 3)}


def build(dates: list[str], *, plan: bool = False, max_days: int = DEFAULT_MAX_DAYS, refresh_market: bool = False, refresh_movers: bool = False, refresh_news: bool = False, refresh_market_news: bool = False, refresh_verification: bool = False, allow_incomplete: bool = False, now: datetime | None = None, logger: logging.Logger | None = None) -> dict[str, Any]:
    """Plan or build dates, returning the manifest payload."""
    logger = logger or logging.getLogger("market_bot.historical_cache")
    statuses = {value: trading_day_status(value, now=now, allow_incomplete=allow_incomplete) for value in dates}
    trading_dates = [value for value in dates if statuses[value] == "TRADING_DAY"]
    if len(trading_dates) > max_days:
        raise ValueError(f"MAX_DAYS_EXCEEDED:{len(trading_dates)}>{max_days}")
    entries: list[dict[str, Any]] = []
    for market_date in dates:
        day_status = statuses[market_date]
        if day_status in {"NON_TRADING_DAY", "INCOMPLETE_TRADING_DAY"}:
            if plan:
                print(f"requested_date={market_date}\nmarket_date={market_date}\ntrading_day_status={day_status}")
            entries.append({"requested_date": market_date, "market_date": market_date, "status": "NON_TRADING_DAY", "missing_components": [], "failed_components": [], "errors": [], "provider_metrics": {}, "elapsed_seconds": 0.0})
            if day_status == "INCOMPLETE_TRADING_DAY":
                entries[-1]["status"] = day_status
            continue
        inspection = inspect_date(market_date, now=now, allow_incomplete=allow_incomplete)
        print(f"requested_date={market_date}\nmarket_date={market_date}\ntrading_day_status={day_status}") if plan else None
        if plan:
            print(f"market={inspection['market']}\nmovers={inspection['movers']}\nfinnhub={inspection['finnhub']}\nmarket_news={inspection.get('market_news', 'MISS')}\nverification={inspection['verification']}\nactions_needed={inspection['actions_needed']}\ndeferred={inspection['deferred']}\nplan_ready={inspection['plan_ready']}")
            entries.append({"market_date": market_date, "status": "PLAN_READY" if inspection["plan_ready"] else "PLAN_NEEDS_BUILD", "plan": inspection, "plan_ready": inspection["plan_ready"], "needs_build": not inspection["plan_ready"], "elapsed_seconds": 0.0})
            continue
        if refresh_market_news and not (refresh_market or refresh_movers or refresh_news or refresh_verification):
            result = _build_market_news(market_date, refresh=True, logger=logger)
            _print_market_news_diagnostics(result)
            entries.append(result)
            continue
        entry = _build_day(market_date, refresh_market=refresh_market, refresh_movers=refresh_movers, refresh_news=refresh_news, refresh_verification=refresh_verification, logger=logger)
        if refresh_market_news:
            market_news_result = _build_market_news(market_date, refresh=True, logger=logger)
            _print_market_news_diagnostics(market_news_result)
            entry["market_news"] = market_news_result.get("market_news", "FAILED")
            entry["market_news_cache"] = market_news_result.get("cache_path")
            entry["market_news_result"] = market_news_result
        entries.append(entry)
    manifest = {"generated_at": date.today().isoformat(), "plan": plan, "dates": entries}
    _atomic_json(MANIFEST_PATH, manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build date-scoped historical Market Driver caches.")
    parser.add_argument("--market-date")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--market-dates")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--max-days", type=int, default=DEFAULT_MAX_DAYS)
    parser.add_argument("--refresh-market", action="store_true")
    parser.add_argument("--refresh-movers", action="store_true")
    parser.add_argument("--refresh-news", action="store_true")
    parser.add_argument("--refresh-market-news", action="store_true")
    parser.add_argument("--refresh-verification", action="store_true")
    parser.add_argument("--allow-incomplete", action="store_true", help="Allow building the current open U.S. session.")
    args = parser.parse_args(argv)
    load_dotenv(BASE_DIR / ".env")
    try:
        dates = requested_dates(market_date=args.market_date, from_date=args.from_date, to_date=args.to_date, market_dates=args.market_dates)
        manifest = build(dates, plan=args.plan, max_days=args.max_days, refresh_market=args.refresh_market, refresh_movers=args.refresh_movers, refresh_news=args.refresh_news, refresh_market_news=args.refresh_market_news, refresh_verification=args.refresh_verification, allow_incomplete=args.allow_incomplete, logger=logging.getLogger("market_bot.historical_cache"))
    except ValueError as exc:
        parser.error(str(exc))
    ready_statuses = {"READY", "PLAN_READY"}
    ready = [item["market_date"] for item in manifest["dates"] if item.get("status") in ready_statuses]
    needs_build = [item["market_date"] for item in manifest["dates"] if item.get("status") == "PLAN_NEEDS_BUILD"]
    non_trading = sum(item.get("status") == "NON_TRADING_DAY" for item in manifest["dates"])
    incomplete = sum(item.get("status") == "INCOMPLETE_TRADING_DAY" for item in manifest["dates"])
    failed = sum(item.get("status") == "FAILED" for item in manifest["dates"])
    print(f"Range summary: plan_ready={len(ready)} needs_build={len(needs_build)} non_trading={non_trading} incomplete_trading_day={incomplete} failed={failed}")
    if ready:
        print("Offline evaluator:")
        print(f"python evaluate_market_drivers_product.py --market-dates {','.join(ready)} --offline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
