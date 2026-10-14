"""Offline market-driver evaluation harness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from drivers import build_driver_analysis

BASE_DIR = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--report-path", type=Path, help="explicit local report fixture/input for isolated offline runs")
    args = parser.parse_args()
    if not args.offline:
        parser.error("--offline is required; this harness never performs live discovery")
    report_path = args.report_path or BASE_DIR / "output" / "daily_report_data.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("market_date") != args.market_date:
        print(f"DRIVER_MARKET_DATE_MISMATCH requested={args.market_date} report={report.get('market_date')}")
        return 1
    result = build_driver_analysis(report)
    print(json.dumps({"market_date": args.market_date, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
