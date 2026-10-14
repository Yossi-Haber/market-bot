"""Analyze historical Movers V2 shadow benchmark files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def analyze_history(history_dir: Path) -> dict[str, Any]:
    """Return aggregate recall and missed true-mover information."""
    files = sorted(history_dir.glob("movers_prefilter_benchmark_*.json"))
    records = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("candidate_pools"), dict):
            records.append(payload)
    pools: dict[str, Any] = {}
    for key in ("30", "40", "50"):
        values = [record["candidate_pools"][key] for record in records if key in record["candidate_pools"]]
        recalls = [float(value.get("recall", 0.0)) for value in values]
        pools[key] = {
            "trading_days_tested": len(values),
            "perfect_days": sum(1 for value in values if value.get("perfect") is True),
            "average_recall": round(sum(recalls) / len(recalls), 4) if recalls else 0.0,
            "worst_recall": min(recalls) if recalls else 0.0,
        }
    misses = []
    for record in records:
        true_movers = {item["symbol"]: item for item in record.get("true_movers", [])}
        discovery_ranks = {item["symbol"]: item.get("discovery_rank") for item in record.get("rank_error", [])}
        for pool_key in ("30", "40", "50"):
            candidates = record.get("candidate_pools", {}).get(pool_key, {}).get("symbols", [])
            for symbol, item in true_movers.items():
                if symbol not in candidates:
                    misses.append({
                        "market_date": record.get("market_date"),
                        "pool": pool_key,
                        "symbol": symbol,
                        "true_move_pct": item.get("change_pct"),
                        "discovery_rank": discovery_ranks.get(symbol),
                    })
    return {"history_dir": str(history_dir), "files_read": len(records), "pools": pools, "misses": misses, "pool_30_misses": [item for item in misses if item["pool"] == "30"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze Movers V2 shadow benchmark history.")
    parser.add_argument("--history-dir", default="output/history", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(analyze_history(args.history_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
