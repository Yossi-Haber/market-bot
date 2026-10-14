"""Single-provider FMP company-news and press-release diagnostic."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from news.providers.fmp import FMPProvider, event_match_score

BASE_DIR = Path(__file__).resolve().parent


def _print_group(label: str, raw_count: int, items: list, event: str | None, symbol: str) -> None:
    print(f"\nFMP {label}")
    print(f"raw_count={raw_count}")
    print(f"normalized_count={len(items)}")
    ranked = sorted(items, key=lambda item: event_match_score(item, event, symbol), reverse=True) if event else items
    for item in ranked[:5]:
        print(f"- headline: {item.title}")
        print(f"  published_at: {item.published_at}")
        print(f"  source: {item.source_name}")
        print(f"  url: {item.source_url}")
        print(f"  content_type: {item.content_type}")
        if event:
            print(f"  event_match_score: {event_match_score(item, event, symbol)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe FMP company news and press releases.")
    parser.add_argument("--symbol", default="BA")
    parser.add_argument("--from-date", default="2026-09-28")
    parser.add_argument("--to-date", default="2026-09-29")
    parser.add_argument("--event", default=None)
    args = parser.parse_args(argv)
    load_dotenv(BASE_DIR / ".env")
    if not os.getenv("FMP_API_KEY", "").strip():
        print("FMP_CONFIG_MISSING")
        return 1
    provider = FMPProvider()
    news = provider.search_company(args.symbol, args.from_date, args.to_date)
    press = provider.search_press_releases(args.symbol, args.from_date, args.to_date)
    print(f"FMP status={provider.status} HTTP={provider.last_http_status}")
    _print_group("COMPANY NEWS", provider.last_news_raw_count, news, args.event, args.symbol.upper())
    _print_group("PRESS RELEASES", provider.last_press_release_raw_count, press, args.event, args.symbol.upper())
    return 0 if provider.status == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
