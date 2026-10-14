"""Shadow-only movers prefilter benchmarking.

This module never supplies production mover values. It measures how well a
Yahoo Finance discovery ranking would have covered the validated Twelve Data
Top 5/Bottom 5 result.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

from config import Instrument


YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
DISCOVERY_TIMEOUT = (5, 10)
DISCOVERY_SOURCE = "Yahoo Finance chart API (discovery only)"
HISTORICAL_LOOKBACK_DAYS = 21
YAHOO_SYMBOL_OVERRIDES = {"BRK.B": "BRK-B"}


def yahoo_provider_symbol(canonical_symbol: str) -> str:
    """Map a canonical market ticker to Yahoo chart-symbol syntax."""
    return YAHOO_SYMBOL_OVERRIDES.get(canonical_symbol, canonical_symbol.replace(".", "-"))


def collect_discovery_changes(
    universe: list[Instrument],
    market_date: str,
    logger: logging.Logger,
    *,
    request_get: Callable[..., Any] = requests.get,
    historical: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collect as-of daily percentage estimates for the shadow universe."""
    started = time.monotonic()
    discovered: list[dict[str, Any]] = []
    requests_made = 0
    for instrument in universe:
        provider_symbol = yahoo_provider_symbol(instrument.symbol)
        try:
            if historical:
                response = request_get(
                    YAHOO_CHART_URL.format(symbol=provider_symbol),
                    params=_historical_params(market_date),
                    headers={"User-Agent": "market_bot/1.0", "Accept": "application/json"},
                    timeout=DISCOVERY_TIMEOUT,
                )
                requests_made += 1
                response.raise_for_status()
                observations, exchange_timezone = _parse_historical_observations(response.json(), market_date)
            else:
                # Keep current/live prefilter acquisition semantics unchanged.
                response = request_get(
                    YAHOO_CHART_URL.format(symbol=provider_symbol),
                    params={"range": "10d", "interval": "1d", "events": "history"},
                    headers={"User-Agent": "market_bot/1.0"},
                    timeout=DISCOVERY_TIMEOUT,
                )
                requests_made += 1
                response.raise_for_status()
                observations = _parse_observations(response.json(), market_date)
                exchange_timezone = "America/New_York"
            if len(observations) < 2:
                raise ValueError(f"FEWER_THAN_TWO_DISCOVERY_BARS timezone={exchange_timezone}")
            latest, previous = observations[0], observations[1]
            change = latest["close"] - previous["close"]
            change_pct = ((latest["close"] / previous["close"]) - 1) * 100
            discovered.append({
                "symbol": instrument.symbol,
                "discovery_change_pct": round(change_pct, 6),
                "discovery_close": round(latest["close"], 6),
                "discovery_previous_close": round(previous["close"], 6),
                "observation_date": latest["date"],
            })
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
            logger.warning("Movers shadow discovery failed for %s: %s", instrument.symbol, type(exc).__name__)
    metrics = {
        "runtime_seconds": round(time.monotonic() - started, 3),
        "http_requests": requests_made,
        "symbols_requested": len(universe),
        "symbols_discovered": len(discovered),
        "critical_failure": False,
    }
    return discovered, metrics


def build_benchmark(
    market_date: str,
    universe: list[Instrument],
    true_results: list[dict[str, Any]],
    discovery_results: list[dict[str, Any]],
    discovery_metrics: dict[str, Any],
) -> dict[str, Any]:
    """Compare discovery candidate pools with the unchanged true movers."""
    true_gainers = sorted(true_results, key=lambda item: float(item["change_pct"]), reverse=True)[:5]
    true_losers = sorted(true_results, key=lambda item: float(item["change_pct"]))[:5]
    true_symbols = [item["symbol"] for item in [*true_gainers, *true_losers]]
    discovery_gainers = sorted(discovery_results, key=lambda item: item["discovery_change_pct"], reverse=True)
    discovery_losers = sorted(discovery_results, key=lambda item: item["discovery_change_pct"])
    discovery_rank = {item["symbol"]: index for index, item in enumerate(discovery_gainers, start=1)}
    true_rank = {item["symbol"]: index for index, item in enumerate(sorted(true_results, key=lambda item: float(item["change_pct"]), reverse=True), start=1)}
    pools: dict[str, Any] = {}
    for size in (15, 20, 25):
        candidates = _dedupe([item["symbol"] for item in discovery_gainers[:size] + discovery_losers[:size]])
        pool_key = str(size * 2)
        captured = [symbol for symbol in true_symbols if symbol in candidates]
        gainers_captured = [item["symbol"] for item in true_gainers if item["symbol"] in candidates]
        losers_captured = [item["symbol"] for item in true_losers if item["symbol"] in candidates]
        pools[pool_key] = {
            "symbols": candidates,
            "size": len(candidates),
            "true_movers_captured": len(captured),
            "recall": _ratio(len(captured), len(true_symbols)),
            "gainers_captured": len(gainers_captured),
            "gainers_recall": _ratio(len(gainers_captured), len(true_gainers)),
            "losers_captured": len(losers_captured),
            "losers_recall": _ratio(len(losers_captured), len(true_losers)),
            "perfect": len(gainers_captured) == len(true_gainers) and len(losers_captured) == len(true_losers),
        }
    rank_errors = []
    true_by_symbol = {item["symbol"]: item for item in true_results}
    discovery_by_symbol = {item["symbol"]: item for item in discovery_results}
    for symbol in sorted(set(true_by_symbol) & set(discovery_by_symbol)):
        true_item = true_by_symbol[symbol]
        discovery_item = discovery_by_symbol[symbol]
        rank_errors.append({
            "symbol": symbol,
            "discovery_rank": discovery_rank.get(symbol),
            "true_rank": true_rank.get(symbol),
            "rank_delta": discovery_rank.get(symbol, 0) - true_rank.get(symbol, 0),
            "discovery_change_pct": discovery_item["discovery_change_pct"],
            "true_change_pct": true_item["change_pct"],
            "percentage_point_error": round(discovery_item["discovery_change_pct"] - float(true_item["change_pct"]), 6),
        })
    return {
        "market_date": market_date,
        "universe_size": len(universe),
        "true_movers": [{"symbol": item["symbol"], "change_pct": item["change_pct"], "direction": "gainer"} for item in true_gainers] + [{"symbol": item["symbol"], "change_pct": item["change_pct"], "direction": "loser"} for item in true_losers],
        "candidate_pools": pools,
        "recall": {key: value["recall"] for key, value in pools.items()},
        "gainers_recall": {key: value["gainers_recall"] for key, value in pools.items()},
        "losers_recall": {key: value["losers_recall"] for key, value in pools.items()},
        "rank_error": rank_errors,
        "discovery_source": DISCOVERY_SOURCE,
        "discovery_metrics": discovery_metrics,
        "production_unchanged": True,
    }


def write_benchmark(benchmark: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    """Write the current and market-date historical benchmark files."""
    output_dir.mkdir(parents=True, exist_ok=True)
    history_dir = output_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(benchmark, ensure_ascii=False, indent=2) + "\n"
    current_path = output_dir / "movers_prefilter_benchmark.json"
    history_path = history_dir / f"movers_prefilter_benchmark_{benchmark['market_date']}.json"
    current_path.write_text(text, encoding="utf-8")
    history_path.write_text(text, encoding="utf-8")
    return current_path, history_path


def run_shadow_benchmark(
    universe: list[Instrument],
    true_results: list[dict[str, Any]],
    market_date: str,
    logger: logging.Logger,
    output_dir: Path,
) -> dict[str, Any] | None:
    """Run the non-blocking shadow benchmark after production movers finish."""
    try:
        discovery_results, metrics = collect_discovery_changes(universe, market_date, logger)
        if len(discovery_results) < 2:
            logger.warning("Movers shadow benchmark skipped: insufficient discovery data")
            return None
        benchmark = build_benchmark(market_date, universe, true_results, discovery_results, metrics)
        current_path, history_path = write_benchmark(benchmark, output_dir)
        logger.info("Movers shadow benchmark created: %s and %s", current_path, history_path)
        return benchmark
    except Exception as exc:  # Shadow mode must never block production.
        logger.warning("Movers shadow benchmark skipped: %s", type(exc).__name__)
        return None


def _parse_observations(payload: Any, market_date: str) -> list[dict[str, Any]]:
    chart = payload["chart"]
    result = chart["result"][0]
    timestamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    observations = []
    for timestamp, close in zip(timestamps, closes):
        if close is None:
            continue
        row_date = datetime.fromtimestamp(int(timestamp), timezone.utc).astimezone(ZoneInfo("America/New_York")).date().isoformat()
        if row_date <= market_date and float(close) > 0:
            observations.append({"date": row_date, "close": float(close)})
    observations.sort(key=lambda item: item["date"], reverse=True)
    return observations


def _historical_params(market_date: str) -> dict[str, Any]:
    """Yahoo chart period bounds for a bounded, exact historical as-of query."""
    target = date.fromisoformat(market_date)
    start = target - timedelta(days=HISTORICAL_LOOKBACK_DAYS)
    period1 = int(datetime.combine(start, datetime_time.min, timezone.utc).timestamp())
    # Yahoo's period2 is exclusive; D+1 includes the entire daily bar for D.
    period2 = int(datetime.combine(target + timedelta(days=1), datetime_time.min, timezone.utc).timestamp())
    return {"period1": period1, "period2": period2, "interval": "1d", "events": "history"}


def _parse_historical_observations(payload: Any, market_date: str) -> tuple[list[dict[str, Any]], str]:
    """Parse valid Yahoo closes through the exact historical target date."""
    try:
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        exchange_timezone = str(result["meta"]["exchangeTimezoneName"])
        exchange_zone = ZoneInfo(exchange_timezone)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError("YAHOO_RESPONSE_FORMAT_CHANGED") from exc
    observations: list[dict[str, Any]] = []
    closes = quote.get("close")
    if not isinstance(timestamps, list) or not isinstance(closes, list):
        raise ValueError("YAHOO_DAILY_TIMESTAMP_OR_CLOSE_ARRAY_MISSING")
    for index, timestamp in enumerate(timestamps):
        try:
            row_date = datetime.fromtimestamp(int(timestamp), timezone.utc).astimezone(exchange_zone).date().isoformat()
            close = float(closes[index])
            if row_date <= market_date and close > 0 and close == close and abs(close) != float("inf"):
                observations.append({"date": row_date, "close": close})
        except (IndexError, TypeError, ValueError, OverflowError):
            continue
    observations.sort(key=lambda item: item["date"], reverse=True)
    return observations, exchange_timezone


def collect_historical_discovery_diagnostics(
    universe: list[Instrument], market_dates: list[str], logger: logging.Logger,
    *, request_get: Callable[..., Any] = requests.get,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], int]:
    """Fetch one bounded Yahoo history window per ticker for date diagnostics."""
    if not market_dates:
        return {}, [], 0
    start_date = (date.fromisoformat(min(market_dates)) - timedelta(days=HISTORICAL_LOOKBACK_DAYS)).isoformat()
    end_date = max(market_dates)
    params = _historical_params(end_date)
    params["period1"] = int(datetime.combine(date.fromisoformat(start_date), datetime_time.min, timezone.utc).timestamp())
    by_date: dict[str, list[dict[str, Any]]] = {value: [] for value in market_dates}
    diagnostics: list[dict[str, Any]] = []
    request_count = 0
    for instrument in universe:
        symbol, provider_symbol = instrument.symbol, yahoo_provider_symbol(instrument.symbol)
        diag: dict[str, Any] = {"symbol": symbol, "yahoo_symbol": provider_symbol, "request_type": "bounded_historical_daily", "requested_start": start_date, "requested_end": end_date, "raw_dates": [], "exchange_timezone": None, "status": "FAIL"}
        try:
            response = request_get(YAHOO_CHART_URL.format(symbol=provider_symbol), params=params.copy(), headers={"User-Agent": "market_bot/1.0", "Accept": "application/json"}, timeout=DISCOVERY_TIMEOUT)
            request_count += 1
            diag["http_status"] = getattr(response, "status_code", None)
            response.raise_for_status()
            parsed, timezone_name = _parse_historical_observations(response.json(), end_date)
            diag["exchange_timezone"] = timezone_name
            diag["raw_dates"] = [item["date"] for item in parsed]
            diag["date_selections"] = {}
            for target in market_dates:
                as_of = [item for item in parsed if item["date"] <= target]
                row: dict[str, Any] = {"symbol": symbol, "market_date": target, "source": "Yahoo"}
                if len(as_of) < 2:
                    row.update({"status": "FAIL", "reason": "FEWER_THAN_TWO_VALID_BARS"})
                    diag["date_selections"][target] = {"status": "FAIL", "reason": row["reason"]}
                    by_date[target].append(row)
                    continue
                current, previous = as_of[0], as_of[1]
                row.update({"status": "PASS", "close": current["close"], "previous_close": previous["close"], "move_pct": ((current["close"] / previous["close"]) - 1) * 100, "target_date_bar": current["date"], "previous_valid_session_bar": previous["date"]})
                diag["date_selections"][target] = {key: row.get(key) for key in ("status", "target_date_bar", "previous_valid_session_bar", "close", "previous_close", "move_pct")}
                by_date[target].append(row)
            diag["status"] = "PASS" if all(any(item["symbol"] == symbol and item["status"] == "PASS" for item in by_date[target]) for target in market_dates) else "PARTIAL"
        except requests.RequestException as exc:
            diag.update({"error_class": type(exc).__name__, "error_message": str(exc), "http_status": getattr(getattr(exc, "response", None), "status_code", diag.get("http_status")), "status": "HTTP_ERROR"})
            for target in market_dates:
                by_date[target].append({"symbol": symbol, "market_date": target, "source": "Yahoo", "status": "FAIL", "reason": f"{type(exc).__name__}:{exc}"})
        except (TypeError, ValueError, KeyError) as exc:
            diag.update({"error_class": type(exc).__name__, "error_message": str(exc), "status": "PARSE_ERROR"})
            for target in market_dates:
                by_date[target].append({"symbol": symbol, "market_date": target, "source": "Yahoo", "status": "FAIL", "reason": f"{type(exc).__name__}:{exc}"})
        logger.info("Historical Yahoo diagnostic symbol=%s status=%s raw_dates=%s", symbol, diag["status"], diag["raw_dates"])
        diagnostics.append(diag)
    return by_date, diagnostics, request_count


def _dedupe(symbols: list[str]) -> list[str]:
    return list(dict.fromkeys(symbols))


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0
