"""Run the bounded official/Marketaux/news-verification diagnostic only."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from news.live import LiveNewsCoverage


BASE_DIR = Path(__file__).resolve().parent


def load_debug_movers(snapshot_path: Path, logger: logging.Logger) -> list[str]:
    """Load exactly the production Top 5 gainers plus Bottom 5 losers."""
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.info("No existing movers snapshot; running general-news diagnostics only")
        return []
    mover_symbols = list(dict.fromkeys([
        str(item.get("symbol", "")).upper()
        for group in ("top_gainers", "top_losers")
        for item in snapshot.get(group, [])
        if isinstance(item, dict) and item.get("symbol")
    ]))
    logger.info("[NEWS DEBUG] Movers loaded: %d/10", len(mover_symbols))
    if len(mover_symbols) < 10:
        logger.warning("[NEWS DEBUG] Movers incomplete: top_gainers=%s top_losers=%s", type(snapshot.get("top_gainers")).__name__, type(snapshot.get("top_losers")).__name__)
    return mover_symbols


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--force-live", action="store_true", help="Bypass same-day discovery and verification caches and require live requests.")
    parser.add_argument("--disable-slow-fallback", action="store_true", help="Disable optional slow fallback discovery paths.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    logger = logging.getLogger("debug_news_performance")
    load_dotenv(BASE_DIR / ".env")

    mover_symbols = load_debug_movers(BASE_DIR / "output" / "movers_snapshot.json", logger)

    if args.disable_slow_fallback:
        os.environ["NEWS_DISABLE_SLOW_FALLBACK"] = "true"
    coverage = LiveNewsCoverage(logger, args.market_date, perform_verification=True, force_live=args.force_live)
    _, budget = coverage.collect(symbols=[], mover_symbols=mover_symbols)
    print("LIVE NEWS DEBUG RESULT")
    print(f"movers_loaded={len(mover_symbols)}")
    print(f"official_live_requests={budget.get('official_requests', 0)}")
    print(f"marketaux_live_requests={budget.get('marketaux_requests', 0)}")
    print(f"marketaux_enabled={budget.get('marketaux_enabled', False)}")
    print(f"marketaux_disabled_reason={budget.get('marketaux_disabled_reason')}")
    print(f"marketaux_requests_attempted={budget.get('marketaux_requests_attempted', 0)}")
    print(f"marketaux_requests_succeeded={budget.get('marketaux_requests_succeeded', 0)}")
    print(f"verification_live_requests={budget.get('verification_requests', 0)}")
    print(f"slow_fallback_requests={budget.get('slow_fallback_requests', 0)}")
    print(f"slow_fallback_movers_attempted={budget.get('slow_fallback_movers_attempted', 0)}")
    print(f"slow_fallback_provider_requests={budget.get('slow_fallback_provider_requests', 0)}")
    print(f"candidates_before_relevance_filter={budget.get('candidates_before_relevance_filter', 0)}")
    print(f"candidates_after_relevance_filter={budget.get('candidates_after_relevance_filter', 0)}")
    print(f"irrelevant_candidates_filtered={budget.get('irrelevant_candidates_filtered', 0)}")
    print(f"timeouts={budget.get('timeouts', 0)}")
    print(f"circuit_breakers_opened={budget.get('circuit_breakers_opened', 0)}")
    print(f"budget_exhausted={budget.get('budget_exhausted', False)}")
    print(f"total_news_seconds={budget.get('total_news_seconds', 0)}")
    print(json.dumps({"market_date": args.market_date, "mover_symbols": mover_symbols, "performance": budget}, ensure_ascii=False, indent=2))
    marketaux_missing = budget.get("marketaux_disabled_reason") == "MARKETAUX_API_KEY_MISSING"
    if args.force_live and not marketaux_missing and budget.get("live_requests_total", 0) == 0:
        logger.error("[NEWS DEBUG] FORCE_LIVE validation failed: live_requests_total=0")
        return 1
    if args.force_live and budget.get("marketaux_expected") and budget.get("marketaux_requests_attempted", 0) == 0:
        logger.error("MARKETAUX_EXPECTED_BUT_NOT_EXECUTED")
        return 1
    if marketaux_missing:
        logger.warning("MARKETAUX_DISABLED reason=API_KEY_MISSING")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
