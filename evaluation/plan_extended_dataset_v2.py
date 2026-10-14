"""Produce the cache-only Phase A plan for Extended Historical Dataset V2."""

from __future__ import annotations

import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import build_historical_evaluation_batch as batch
import build_historical_market_cache as historical


OUT = ROOT / "output/evaluation_extended_v2"
START = date(2026, 7, 6)
END = date(2026, 9, 28)
# NYSE was closed on Labor Day. July 3 is before this selected window.
NYSE_CLOSED = {"2026-09-07"}


def selected_dates() -> list[str]:
    return [
        (START + timedelta(days=offset)).isoformat()
        for offset in range((END - START).days + 1)
        if (START + timedelta(days=offset)).weekday() < 5
        and (START + timedelta(days=offset)).isoformat() not in NYSE_CLOSED
    ]


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    dates = selected_dates()
    if len(dates) != 60 or dates[0] != START.isoformat() or dates[-1] != END.isoformat():
        raise RuntimeError(f"EXPECTED_60_CONTIGUOUS_SESSIONS_GOT:{len(dates)}")

    plans: list[dict[str, object]] = []
    coverage_rows: list[dict[str, object]] = []
    component_counts: dict[str, dict[str, int]] = {}
    for market_date in dates:
        state = batch.inspect_components(market_date)
        details = historical.inspect_date(market_date)
        driver_path = details.get("market_driver_snapshot")
        driver_hit = bool(driver_path and Path(str(driver_path)).is_file())
        row = {
            "date": market_date,
            "market_data": state["market_data"],
            "movers": state["movers"],
            "market_news": state["market_news"],
            "stock_news": state["stock_news"],
            "verification": state["verification"],
            "market_driver_snapshot": "HIT" if driver_hit else "MISS",
            "expected_network_work": "YES" if state["expected_network_work"] else "NO",
            "actions_needed": state["actions_needed"],
            "deferred": state["deferred"],
            "readiness_expectation": "READY_IF_BUILD_SUCCEEDS" if state["expected_network_work"] else "READY_FROM_CACHE" if state["plan_ready"] else "REVIEW_DEPENDENCIES",
        }
        plans.append(row)
        cache = state["coverage"]
        coverage = {
            "date": market_date,
            "market_snapshot_valid": state["market_data"],
            "market_cache_reported": cache.get("market_data", "MISSING"),
            "movers_snapshot_valid": state["movers"],
            "movers_cache_reported": cache.get("movers", "MISSING"),
            "mover_symbol_news_finnhub": cache.get("finnhub", "MISSING"),
            "marketaux": cache.get("marketaux", "MISSING"),
            "official_news": cache.get("official", "MISSING"),
            "tier2_news": cache.get("tier2", "MISSING"),
            "verification_cache": cache.get("verification", "MISSING"),
            "market_driver_snapshot": "HIT" if driver_hit else "MISS",
            "mover_symbols_known": str(state.get("coverage", {}).get("movers", "MISSING")),
        }
        coverage_rows.append(coverage)
        for component in ("market_data", "movers", "market_news", "stock_news", "verification", "market_driver_snapshot"):
            value = str(row[component])
            component_counts.setdefault(component, {})[value] = component_counts.setdefault(component, {}).get(value, 0) + 1

    OUT.mkdir(parents=True, exist_ok=True)
    write_csv(OUT / "dataset_plan.csv", plans, list(plans[0]))
    write_csv(OUT / "cache_coverage.csv", coverage_rows, list(coverage_rows[0]))
    missing_by_component = {
        key: sum(value in {"MISS", "DEFERRED", "PARTIAL"} for value in (str(row[key]) for row in plans))
        for key in ("market_data", "movers", "market_news", "stock_news", "verification", "market_driver_snapshot")
    }
    payload = {
        "phase": "A_PLAN_ONLY",
        "date_range": {"from": dates[0], "to": dates[-1]},
        "trading_dates": dates,
        "trading_days_planned": len(dates),
        "holiday_exclusions": sorted(NYSE_CLOSED),
        "cache_state_counts": component_counts,
        "dates_needing_network_or_build": sum(row["expected_network_work"] == "YES" for row in plans),
        "dates_ready_from_cache": sum(row["readiness_expectation"] == "READY_FROM_CACHE" for row in plans),
        "missing_or_deferred_date_components": missing_by_component,
        "network_calls_made": 0,
        "fmp_requests": 0,
        "provider_build_started": False,
        "note": "Phase B is not started by this planner. The plan exposes the missing market/mover inputs before any collection.",
    }
    (OUT / "dataset_summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Extended Historical Evaluation Dataset V2 — Phase A Plan", "",
        f"Window: **{dates[0]} through {dates[-1]}**, {len(dates)} NYSE sessions.",
        "Excluded weekends and the September 7, 2026 Labor Day closure. This is a cache-only plan; no provider or network requests were made.", "",
        f"Dates needing build/network work: **{payload['dates_needing_network_or_build']}**.",
        f"Dates already READY from cache: **{payload['dates_ready_from_cache']}**.", "",
        "## Component states", "",
        "| Component | State counts | Dates missing/deferred |", "|---|---|---:|",
    ]
    for component in ("market_data", "movers", "market_news", "stock_news", "verification", "market_driver_snapshot"):
        counts = component_counts.get(component, {})
        formatted = ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
        lines.append(f"| {component} | {formatted} | {missing_by_component[component]} |")
    lines += ["", "The complete per-date dependency plan is `dataset_plan.csv`; detailed source/cache availability is `cache_coverage.csv`.", "",
              "**Phase B is not started.** The plan shows that most of the selected historical window lacks valid dated market/mover snapshots. Building those gaps would entail substantial Twelve Data collection, so the missing-component plan is surfaced before collection.", ""]
    (OUT / "dataset_summary.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
