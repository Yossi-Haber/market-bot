"""Executable entry point for daily report-data composition."""

from __future__ import annotations

import sys

from app import configure_logging
from report.composer import ReportCompositionError, compose_daily_report


if __name__ == "__main__":
    try:
        compose_daily_report(configure_logging())
    except ReportCompositionError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
