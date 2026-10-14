"""Fast, non-persisting diagnostic for a small number of movers batches."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from collectors.market import TwelveDataClient
from movers import load_universe
from validation.validate import validate_market_result


def build_logger() -> logging.Logger:
    logger = logging.getLogger("debug_movers_batches")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s - %(message)s"))
    logger.addHandler(handler)
    return logger


def run(market_date: str, batches: int) -> int:
    logger = build_logger()
    base_dir = Path(__file__).resolve().parent
    load_dotenv(base_dir / ".env")
    api_key = os.getenv("TWELVE_DATA_API_KEY", "").strip()
    if not api_key or api_key == "PASTE_API_KEY_HERE":
        logger.error("DEBUG_MOVERS_CONFIG_MISSING: TWELVE_DATA_API_KEY")
        return 1
    if batches < 1:
        logger.error("Batches must be at least 1")
        return 2

    universe = load_universe()
    selected = universe[: batches * 8]
    client = TwelveDataClient(api_key)
    requested = returned = valid_count = 0
    for offset in range(0, len(selected), 8):
        batch = selected[offset : offset + 8]
        batch_number = offset // 8 + 1
        logger.info("DEBUG batch %d/%d prepared: %s", batch_number, batches, ",".join(item.symbol for item in batch))
        results = client.fetch_stock_batch(batch, logger, market_date=market_date)
        batch_returned = 0
        for instrument in batch:
            requested += 1
            result = results.get(instrument.symbol, {})
            if result.get("_provider_returned", bool(result.get("verified"))):
                returned += 1
                batch_returned += 1
            raw_dates = result.get("_raw_dates", [])
            selected_current = str(result.get("latest_bar_datetime", ""))[:10] or None
            selected_previous = result.get("_selected_previous_date")
            if not result.get("verified"):
                logger.info("%s raw_dates=%s selected_current=%s selected_previous=%s validation=FAIL reason=%s", instrument.symbol, raw_dates, selected_current, selected_previous, result.get("error", "MISSING_RESULT"))
                continue
            is_valid, messages = validate_market_result(result)
            if result.get("latest_bar_datetime", "")[:10] != market_date:
                is_valid = False
                messages = ["MOVER_DATE_MISMATCH"]
            if result.get("volume") is None or result.get("volume") <= 0:
                is_valid = False
                messages = ["MISSING_OR_INVALID_VOLUME"]
            if is_valid:
                valid_count += 1
                logger.info("%s raw_dates=%s selected_current=%s selected_previous=%s validation=PASS", instrument.symbol, raw_dates, selected_current, selected_previous)
            else:
                logger.info("%s raw_dates=%s selected_current=%s selected_previous=%s validation=FAIL reason=%s", instrument.symbol, raw_dates, selected_current, selected_previous, "; ".join(messages))
        logger.info("DEBUG batch %d/%d received: %d/%d symbols", batch_number, batches, batch_returned, len(batch))

    logger.info("DEBUG movers summary: batches=%d symbols_requested=%d symbols_returned=%d symbols_valid=%d symbols_failed=%d", batches, requested, returned, valid_count, requested - valid_count)
    logger.info("No production files written; cache, catalyst, news, LLM, visual, and Telegram were not run")
    return 0 if returned == requested and valid_count == requested else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a small real Twelve Data movers diagnostic without persistence.")
    parser.add_argument("--market-date", required=True, help="Canonical market date in YYYY-MM-DD format.")
    parser.add_argument("--batches", type=int, default=3, help="Number of 8-symbol batches to execute.")
    args = parser.parse_args(argv)
    return run(args.market_date, args.batches)


if __name__ == "__main__":
    raise SystemExit(main())
