"""Validate the generated visual report and write its publish manifest."""

from __future__ import annotations

import logging

from visual.validate import validate_visual_report


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s visual-validation - %(message)s")
    result = validate_visual_report()
    logging.info("Visual publish validation: %s", "PASS" if result["valid"] else "FAIL")
    if result["errors"]:
        logging.error("Visual validation errors: %s", ";".join(result["errors"]))


if __name__ == "__main__":
    main()
