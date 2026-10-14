"""Manual Telegram report delivery entry point."""

from __future__ import annotations

import argparse
import logging
import sys

from delivery.telegram import run_delivery


def main() -> int:
    parser = argparse.ArgumentParser(description="Send the publishable daily market card to Telegram.")
    parser.add_argument("--dry-run", action="store_true", help="Perform local checks without sending Telegram")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s delivery - %(message)s")
    return run_delivery(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
