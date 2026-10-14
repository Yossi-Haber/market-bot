"""Small, single-provider Finnhub Company News diagnostic."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from news.providers.finnhub import FinnhubProvider
from news.providers.finnhub import cluster_finnhub_candidates

BASE_DIR = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe Finnhub Company News for one symbol and date range.")
    parser.add_argument("--symbol", default="BA")
    parser.add_argument("--from-date", default="2026-09-28")
    parser.add_argument("--to-date", default="2026-09-29")
    parser.add_argument("--rank", action="store_true", help="Apply local subject gating and event ranking.")
    args = parser.parse_args(argv)

    load_dotenv(BASE_DIR / ".env")
    if not os.getenv("FINNHUB_API_KEY", "").strip():
        print("FINNHUB_CONFIG_MISSING")
        return 1

    provider = FinnhubProvider()
    articles = provider.search_company(args.symbol.upper(), args.from_date, args.to_date)
    print(f"Finnhub status: {provider.status}")
    print(f"HTTP status: {provider.last_http_status}")
    print(f"finnhub_cache_hit={provider.cache_hits > 0}")
    print(f"finnhub_cache_miss={provider.cache_misses > 0}")
    print(f"finnhub_live_request={provider.live_requests > 0}")
    print(f"cache_written={provider.cache_writes > 0}")
    print(f"cache_path={provider.last_cache_path or ''}")
    if provider.last_error:
        print(f"Provider error: {provider.last_error}")
    print(f"Raw article count: {provider.last_raw_count}")
    print(f"Normalized article count: {provider.last_normalized_count}")
    for index, article in enumerate(articles[:5], start=1):
        raw_datetime = article.raw_metadata.get("datetime", article.published_at)
        print(f"\n{index}. {article.title}")
        print(f"   source: {article.source_name}")
        print(f"   datetime: {raw_datetime}")
        print(f"   url: {article.source_url}")
        print(f"   symbols: {', '.join(article.affected_symbols) or 'none'}")
        print(f"   entities: {', '.join(article.entity_names) or 'none'}")
    if args.rank:
        print("\nRanked candidates:")
        for cluster in cluster_finnhub_candidates(articles, args.symbol.upper(), "Boeing" if args.symbol.upper() == "BA" else ""):
            print(f"- cluster_rank={cluster['cluster_rank']} canonical_event={cluster['canonical_event_title']} event_type={cluster['event_type']} target_role={cluster['target_role']} event_signature={cluster['event_signature']} topic_family={cluster['topic_family']} event_actions={','.join(cluster['event_actions'])} actors={','.join(cluster['actors'])} cluster_coherence_score={cluster['cluster_coherence_score']} independent_sources={cluster['independent_source_count']} article_count={cluster['article_count']} selection_score={cluster['selection_score']}")
            for article in cluster["articles"]:
                print(f"  supporting: {article.title}")
    return 0 if provider.status == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
