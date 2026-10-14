"""Run a short, request-free rate-limit wait diagnostic."""

from __future__ import annotations

import argparse
import logging

from collectors.rate_limit import CreditRateLimitController


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait-seconds", type=float, default=3.0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("debug_rate_limit")
    controller = CreditRateLimitController()
    controller.debug_wait(args.wait_seconds, logger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
