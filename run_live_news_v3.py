"""Controlled CLI for the optional LIVE NEWS V3 coverage test."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from news.live import LiveNewsCoverage
from news.marketaux import MarketauxDiscovery
from news.verification_v2 import public_story

BASE_DIR = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the opt-in live news coverage test.")
    parser.add_argument("--market-date", default=datetime.now(timezone.utc).date().isoformat())
    parser.add_argument("--live", action="store_true", help="Enable real official/Marketaux requests.")
    parser.add_argument("--symbols", default="", help="Optional comma-separated symbols for SEC filing coverage.")
    parser.add_argument("--refresh-marketaux", action="store_true", help="Bypass Marketaux cache for this run only.")
    parser.add_argument("--refresh-verification", action="store_true", help="Bypass Verification V2 evidence cache for this run only.")
    parser.add_argument("--diagnose-marketaux", action="store_true", help="Run five raw Marketaux diagnostic probes and exit.")
    args = parser.parse_args()
    load_dotenv(BASE_DIR / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s live-news-v3 - %(message)s")
    if not args.live:
        print("LIVE_NEWS_DISABLED: rerun with --live for a controlled request")
        return 0
    if args.diagnose_marketaux:
        client = MarketauxDiscovery(logging.getLogger("market_bot.marketaux_diagnostic"), args.market_date, refresh_cache=True)
        production = next(item for item in LiveNewsCoverage.default_queries([]) if item["query_type"] == "AI_SEMICONDUCTORS")
        probes = client.diagnose({key: value for key, value in production.items() if key != "query_type"})
        output = BASE_DIR / "output" / "marketaux_diagnostic.json"
        output.write_text(json.dumps({"report_date": args.market_date, "probes": probes}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Marketaux diagnostic written: {output}")
        return 0
    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    mover_symbols: list[str] = []
    if not symbols:
        movers_path = BASE_DIR / "output" / "movers_snapshot.json"
        try:
            movers = json.loads(movers_path.read_text(encoding="utf-8"))
            mover_symbols = [str(item.get("symbol", "")).upper() for group in ("top_gainers", "top_losers") for item in movers.get(group, []) if item.get("symbol")][:8]
        except (OSError, ValueError, AttributeError):
            symbols = []
    stories, budget = LiveNewsCoverage(logging.getLogger("market_bot.live_news"), args.market_date, refresh_marketaux=args.refresh_marketaux, perform_verification=True, refresh_verification=args.refresh_verification).collect(symbols=symbols, mover_symbols=mover_symbols)
    output = BASE_DIR / "output" / "live_news_v3_snapshot.json"
    public_budget = dict(budget)
    public_budget.pop("verification_diagnostics", None)
    output.write_text(json.dumps({"market_date": args.market_date, "generated_at": datetime.now(timezone.utc).isoformat(), "request_budget": public_budget, "stories": [public_story(item) for item in stories]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Live news snapshot written: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
