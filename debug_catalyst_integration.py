"""Exercise Movers -> Live News V3 -> Catalyst mapping without other stages."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from news.catalyst import build_catalyst_snapshot_from_live_news
from news.live import LiveNewsCoverage
from news.verification_v2 import public_story


BASE_DIR = Path(__file__).resolve().parent


def load_movers_snapshot_for_date(path: Path, requested_date: str) -> dict:
    """Load only a movers snapshot whose canonical date matches the request."""
    movers = json.loads(path.read_text(encoding="utf-8"))
    snapshot_date = str(movers.get("market_date") or "MISSING")
    if snapshot_date != requested_date:
        raise RuntimeError(f"DEBUG_MOVERS_DATE_MISMATCH\nrequested={requested_date}\nsnapshot={snapshot_date}")
    return movers


def validate_date_consistency(requested: str, movers: dict, live_news: dict, catalyst: dict) -> bool:
    """Require every debug-stage payload to use the requested market date."""
    movers_date = str(movers.get("market_date") or "MISSING")
    live_date = str(live_news.get("market_date") or "MISSING")
    catalyst_date = str(catalyst.get("market_date") or "MISSING")
    print("[CATALYST DEBUG] Date consistency:")
    print(f"requested={requested}")
    print(f"movers={movers_date}")
    print(f"live_news={live_date}")
    print(f"catalyst={catalyst_date}")
    passed = len({requested, movers_date, live_date, catalyst_date}) == 1
    print("PASS" if passed else "FAIL")
    return passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--force-live", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    logger = logging.getLogger("debug_catalyst_integration")
    load_dotenv(BASE_DIR / ".env")

    movers_path = BASE_DIR / "output" / "movers_snapshot.json"
    try:
        movers = load_movers_snapshot_for_date(movers_path, args.market_date)
    except (OSError, ValueError) as exc:
        logger.error("DEBUG_MOVERS_SNAPSHOT_INVALID: %s", type(exc).__name__)
        return 1
    except RuntimeError as exc:
        print(str(exc))
        return 1
    mover_symbols = list(dict.fromkeys([
        str(item.get("symbol", "")).upper()
        for group in ("top_gainers", "top_losers")
        for item in movers.get(group, [])
        if isinstance(item, dict) and item.get("symbol")
    ]))
    stories, budget = LiveNewsCoverage(
        logger,
        args.market_date,
        perform_verification=True,
        force_live=args.force_live,
    ).collect(symbols=[], mover_symbols=mover_symbols)
    live_news = {
        "market_date": args.market_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "request_budget": budget,
        "stories": [public_story(item) for item in stories],
    }
    catalyst_snapshot = build_catalyst_snapshot_from_live_news(movers, live_news, logger=logger)
    if not validate_date_consistency(args.market_date, movers, live_news, catalyst_snapshot):
        return 1
    stats = catalyst_snapshot["request_statistics"]
    print("CATALYST INTEGRATION DEBUG RESULT")
    print(f"movers_loaded={len(mover_symbols)}")
    print(f"live_stories={len(live_news['stories'])}")
    print(f"verified_catalysts={catalyst_snapshot['summary']['CONFIRMED']}")
    print(f"catalyst_network_requests={stats['catalyst_network_requests']}")
    print(f"duplicate_requests_avoided={stats['duplicate_requests_avoided']}")
    print(json.dumps(catalyst_snapshot, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
