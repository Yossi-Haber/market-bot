"""Run one isolated Market Driver event through production verification."""

from __future__ import annotations

import argparse
import logging
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

from news.providers.finnhub import FinnhubProvider, cluster_finnhub_candidates
from news.verification_acquisition import LiveEvidenceAcquirer, resolve_verification_route
from news.verification_v2 import verify_candidate

BASE_DIR = Path(__file__).resolve().parent


class _NoFMP:
    """Fail closed if this isolated harness accidentally reaches FMP."""

    enabled = False
    requests = 0

    def search_press_releases(self, *args: Any, **kwargs: Any) -> list[Any]:
        self.requests += 1
        raise RuntimeError("FMP_MUST_NOT_BE_CALLED")


def _candidate_from_cluster(cluster: dict[str, Any], symbol: str) -> dict[str, Any]:
    """Preserve the production cluster metadata required by verification."""
    best = cluster.get("best_candidate")
    published_at = getattr(best, "published_at", None)
    source_url = getattr(best, "source_url", None)
    source_name = getattr(best, "source_name", None)
    return {
        "title": cluster.get("canonical_event_title"),
        "summary": None,
        "published_at": published_at,
        "affected_symbols": [symbol.upper()],
        "target_symbol": cluster.get("target_symbol", symbol.upper()),
        "event_type": cluster.get("event_type"),
        "category": cluster.get("event_type"),
        "actors": list(cluster.get("actors", [])),
        "topic_family": cluster.get("topic_family"),
        "event_actions": list(cluster.get("event_actions", [])),
        "event_signature": cluster.get("event_signature"),
        "event_cluster_id": cluster.get("event_cluster_id"),
        "canonical_event_title": cluster.get("canonical_event_title"),
        "target_role": cluster.get("target_role"),
        "source_url": source_url,
        "source_name": source_name,
        "discovery_source": "finnhub",
    }


def _attempt_for_route(attempts: list[dict[str, str]], route_step: str) -> dict[str, str] | None:
    prefixes = {
        "REGULATOR_OFFICIAL": ("REGULATOR_OFFICIAL",),
        "COMPANY_IR": ("OFFICIAL_IR",),
        "SEC_EDGAR": ("SEC_EDGAR",),
        "OFFICIAL_IR_DISCOVERY": ("OFFICIAL_IR_DISCOVERY",),
        "TIER2_SEARCH": ("TIER2_SEARCH",),
    }
    for attempt in attempts:
        if str(attempt.get("type", "")).startswith(prefixes.get(route_step, (route_step,))):
            return attempt
    return None


def run_debug(
    symbol: str,
    market_date: str,
    force_live: bool = False,
    *,
    cache_dir: Path | None = None,
    request_get: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run one cached Finnhub cluster through the production verifier."""
    started = time.monotonic()
    symbol = symbol.upper()
    end_date = (date.fromisoformat(market_date) + timedelta(days=1)).isoformat()
    logger = logging.getLogger("debug_market_driver_verification")
    provider = FinnhubProvider(token="configured", request_get=request_get, cache_dir=cache_dir or BASE_DIR / "cache" / "finnhub")
    articles = provider.search_company(symbol, market_date, end_date)
    if not articles:
        raise RuntimeError(f"FINNHUB_CACHE_UNAVAILABLE symbol={symbol} window={market_date}_{end_date}")
    clusters = cluster_finnhub_candidates(articles, symbol, "Boeing" if symbol == "BA" else "")
    if not clusters:
        raise RuntimeError(f"FINNHUB_NO_ELIGIBLE_CLUSTER symbol={symbol}")
    selected = clusters[0]
    candidate = _candidate_from_cluster(selected, symbol)
    if candidate.get("target_symbol") != symbol or symbol not in {str(item).upper() for item in candidate.get("affected_symbols", [])}:
        raise RuntimeError("SELECTED_CLUSTER_SYMBOL_MISMATCH")
    if not candidate.get("event_type"):
        raise RuntimeError("EVENT_TYPE_MISSING")
    if not candidate.get("actors"):
        raise RuntimeError("EVENT_ACTORS_MISSING")
    route = resolve_verification_route(candidate)
    if not route:
        raise RuntimeError("VERIFICATION_ROUTE_MISSING")
    if candidate["event_type"] == "REGULATORY" and "FAA" in candidate["actors"] and route[0] != "REGULATOR_OFFICIAL":
        raise RuntimeError("REGULATORY_ROUTE_MISSING_REGULATOR")

    no_fmp = _NoFMP()
    acquirer = LiveEvidenceAcquirer(
        logger,
        market_date,
        request_get=request_get,
        fmp_provider=no_fmp,
        refresh_cache=force_live,
        cache_only=not force_live,
    )
    acquired = acquirer.acquire([candidate])
    evidence, diagnostics = acquired[candidate["title"]]
    result, verification = verify_candidate(candidate, evidence)
    if no_fmp.requests:
        raise RuntimeError("FMP_MUST_NOT_BE_CALLED")
    attempts = diagnostics.attempted_sources
    source_trace = []
    for step in route:
        attempt = _attempt_for_route(attempts, step)
        source_trace.append({
            "source": step,
            "status": attempt.get("status", "NOT_ATTEMPTED") if attempt else "NOT_ATTEMPTED",
            "match": "yes" if attempt and evidence else "no",
        })
    regulator_requests = sum(1 for item in attempts if item.get("type", "").startswith("REGULATOR_OFFICIAL") and item.get("status") not in {"CACHE_HIT", "CACHE_MISS"})
    ir_requests = sum(1 for item in attempts if item.get("type", "").startswith(("OFFICIAL_IR", "OFFICIAL_IR_DISCOVERY")) and item.get("status") not in {"CACHE_HIT", "CACHE_MISS"})
    tier2_requests = sum(1 for item in attempts if item.get("type", "").startswith("TIER2") and item.get("status") not in {"CACHE_HIT", "CACHE_MISS"})
    return {
        "symbol": symbol,
        "market_date": market_date,
        "cache_status": provider.last_cache_status,
        "cached_articles": provider.last_normalized_count,
        "eligible_candidates": sum(int(item.get("article_count", 0) or 0) for item in clusters),
        "clusters": len(clusters),
        "selected_event": candidate["canonical_event_title"],
        "event_signature": candidate["event_signature"],
        "event_type": candidate["event_type"],
        "actors": candidate["actors"],
        "topic_family": candidate["topic_family"],
        "event_actions": candidate["event_actions"],
        "verification_route": route,
        "source_trace": source_trace,
        "regulator_domain": diagnostics.regulator_domain,
        "regulator_requested_url": diagnostics.regulator_requested_url,
        "regulator_http_status": diagnostics.regulator_http_status,
        "regulator_error_code": diagnostics.regulator_error_code,
        "regulator_fallback_used": diagnostics.regulator_fallback_used,
        "regulator_match": "yes" if verification.matched_source_type == "OFFICIAL" and diagnostics.regulator_domain and verification.matched_domain and (verification.matched_domain == diagnostics.regulator_domain or verification.matched_domain.endswith("." + diagnostics.regulator_domain)) else "no",
        "verification_status": result.get("verification"),
        "matched_source_type": verification.matched_source_type,
        "matched_domain": verification.matched_domain,
        "reason": verification.reason,
        "final_driver_status": result.get("verification"),
        "http_requests": acquirer.request_count,
        "regulator_requests": regulator_requests,
        "ir_requests": ir_requests,
        "tier2_requests": tier2_requests,
        "fmp_requests": no_fmp.requests,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def _print_trace(trace: dict[str, Any]) -> None:
    print("MARKET DRIVER VERIFICATION DEBUG")
    print(f"symbol={trace['symbol']}")
    print(f"market_date={trace['market_date']}")
    print("\nDISCOVERY:")
    for key in ("cache_status", "cached_articles", "eligible_candidates", "clusters", "selected_event", "event_signature", "event_type", "actors", "topic_family", "event_actions"):
        print(f"{key}={trace[key]}")
    print("\nVERIFICATION:")
    print(f"verification_route={' -> '.join(trace['verification_route'])}")
    for index, item in enumerate(trace["source_trace"], start=1):
        print(f"source_{index}={item['source']}")
        print(f"source_{index}_status={item['status']}")
        print(f"source_{index}_match={item['match']}")
    print(f"regulator_domain={trace['regulator_domain']}")
    print(f"regulator_requested_url={trace['regulator_requested_url']}")
    print(f"regulator_http_status={trace['regulator_http_status']}")
    print(f"regulator_error_code={trace['regulator_error_code']}")
    print(f"regulator_fallback_used={trace['regulator_fallback_used']}")
    print(f"regulator_match={trace['regulator_match']}")
    print("\nDECISION:")
    for key in ("verification_status", "matched_source_type", "matched_domain", "reason", "final_driver_status"):
        print(f"{key}={trace[key]}")
    print("\nMETRICS:")
    for key in ("http_requests", "regulator_requests", "ir_requests", "tier2_requests", "fmp_requests", "elapsed_seconds"):
        print(f"{key}={trace[key]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--force-live", action="store_true")
    args = parser.parse_args(argv)
    load_dotenv(BASE_DIR / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    try:
        trace = run_debug(args.symbol, args.market_date, args.force_live)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"MARKET_DRIVER_VERIFICATION_FAILED reason={exc}")
        return 1
    _print_trace(trace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
