"""Yahoo-only historical mover prefilter recovery and cached-truth audit."""

from __future__ import annotations

import argparse
import csv
import json
import logging
from pathlib import Path
from typing import Any

import movers
from movers import _prefilter_candidate_pool
from movers_prefilter import collect_historical_discovery_diagnostics

ROOT = Path(__file__).resolve().parents[1]
MILESTONE = ROOT / "output/evaluation_extended_v2/30_day_milestone"
OUTPUT_DIR = MILESTONE / "historical_yahoo_prefilter_recovery_v1"
SAMPLE_DATES = ["2026-08-20", "2026-08-21"]
DIAG_SYMBOLS = {"AAPL", "NVDA", "META", "AMD", "JPM", "XOM", "LRCX", "HUM", "BRK.B", "MMC"}


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _load_truth(market_date: str) -> list[dict[str, Any]]:
    checkpoint = MILESTONE / "history" / market_date / ".movers_build" / ".resume" / "mover_checkpoint.json"
    try:
        payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        rows = payload.get("valid_results", [])
        if payload.get("market_date") != market_date or len(rows) < 110:
            raise ValueError("CACHED_TWELVE_DATA_GROUND_TRUTH_INCOMPLETE")
        return [row for row in rows if row.get("verified") is True and row.get("latest_bar_datetime", "")[:10] == market_date]
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"CACHED_TWELVE_DATA_GROUND_TRUTH_UNAVAILABLE:{market_date}:{exc}") from exc


def _rank_and_compare(market_date: str, rows: list[dict[str, Any]], truth: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    valid = [row for row in rows if row.get("status") == "PASS" and row.get("target_date_bar") == market_date and row.get("previous_valid_session_bar", "") < market_date]
    discovery = [{"symbol": row["symbol"], "discovery_change_pct": row["move_pct"], "observation_date": market_date} for row in valid]
    universe = movers.load_universe()
    metric = {"symbols_requested": len(universe), "symbols_discovered": len(valid), "critical_failure": False}
    selected, reason = _prefilter_candidate_pool(universe, discovery, metric, market_date)
    selected_symbols = [item.symbol for item in selected] if selected else []
    ranked_up = sorted(valid, key=lambda row: row["move_pct"], reverse=True)
    ranked_down = sorted(valid, key=lambda row: row["move_pct"])
    true_up = sorted(truth, key=lambda row: float(row["change_pct"]), reverse=True)[:5]
    true_down = sorted(truth, key=lambda row: float(row["change_pct"]))[:5]
    candidate_set = set(selected_symbols)
    captured_up = [row["symbol"] for row in true_up if row["symbol"] in candidate_set]
    captured_down = [row["symbol"] for row in true_down if row["symbol"] in candidate_set]
    comparison = {
        "market_date": market_date,
        "yahoo_valid_symbols": len(valid),
        "coverage_pct": round(100 * len(valid) / max(1, len(universe)), 2),
        "candidate_count": len(selected_symbols),
        "fallback_required": reason is not None,
        "fallback_reason": reason or "",
        "true_top5_gainers": ",".join(row["symbol"] for row in true_up),
        "captured_top5_gainers": ",".join(captured_up),
        "gainers_captured": len(captured_up),
        "gainers_recall": round(len(captured_up) / max(1, len(true_up)), 4),
        "true_top5_losers": ",".join(row["symbol"] for row in true_down),
        "captured_top5_losers": ",".join(captured_down),
        "losers_captured": len(captured_down),
        "losers_recall": round(len(captured_down) / max(1, len(true_down)), 4),
        "overall_top_mover_recall": round((len(captured_up) + len(captured_down)) / max(1, len(true_up) + len(true_down)), 4),
        "missed_true_movers": ",".join([row["symbol"] for row in true_up + true_down if row["symbol"] not in candidate_set]),
        "yahoo_top20_gainers": ",".join(row["symbol"] for row in ranked_up[:20]),
        "yahoo_bottom20_losers": ",".join(row["symbol"] for row in ranked_down[:20]),
    }
    candidate_rows = []
    for side, ranked in (("GAINER", ranked_up), ("LOSER", ranked_down)):
        candidate_rows.extend({**row, "rank_from_extreme": rank, "side": side, "candidate_selected": row["symbol"] in candidate_set} for rank, row in enumerate(ranked, 1))
    return comparison, candidate_rows, selected_symbols


def run_recovery(market_dates: list[str] = SAMPLE_DATES, *, output_dir: Path = OUTPUT_DIR) -> dict[str, Any]:
    logger = logging.getLogger("historical_yahoo_prefilter_recovery")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    universe = movers.load_universe()
    rows_by_date, diagnostics, request_count = collect_historical_discovery_diagnostics(universe, market_dates, logger)
    failure_rows: list[dict[str, Any]] = []
    comparison_rows: list[dict[str, Any]] = []
    day_summaries: dict[str, Any] = {}
    ready = True
    diag_by_symbol = {item["symbol"]: item for item in diagnostics}
    for market_date in market_dates:
        day_rows = rows_by_date[market_date]
        truth = _load_truth(market_date)
        comparison, ranked_rows, selected = _rank_and_compare(market_date, day_rows, truth)
        comparison_rows.append(comparison)
        valid_count = sum(row.get("status") == "PASS" for row in day_rows)
        coverage_pct = round(100 * valid_count / len(universe), 2)
        passes = coverage_pct >= movers.PREFILTER_MIN_VALID_RATIO * 100 and not comparison["fallback_required"] and len(selected) <= 40 and comparison["overall_top_mover_recall"] >= 0.8
        ready = ready and passes
        day_summaries[market_date] = {
            **comparison, "valid_symbols": valid_count, "failed_symbols": len(universe) - valid_count,
            "selected_symbols": selected,
            "top_gainers": comparison["yahoo_top20_gainers"].split(",") if comparison["yahoo_top20_gainers"] else [],
            "top_losers": comparison["yahoo_bottom20_losers"].split(",") if comparison["yahoo_bottom20_losers"] else [],
            "ready": passes,
        }
        for row in day_rows:
            diag = diag_by_symbol[row["symbol"]]
            selection = diag.get("date_selections", {}).get(market_date, {})
            failure_rows.append({
                "market_date": market_date, "symbol": row["symbol"], "yahoo_symbol": diag["yahoo_symbol"],
                "request_type": diag["request_type"], "requested_start": diag["requested_start"],
                "requested_end": diag["requested_end"], "http_status": diag.get("http_status"),
                "raw_dates_returned": ",".join(diag.get("raw_dates", [])), "exchange_timezone": diag.get("exchange_timezone"),
                "target_date_bar": selection.get("target_date_bar"), "previous_valid_session_bar": selection.get("previous_valid_session_bar"),
                "close": selection.get("close"), "previous_close": selection.get("previous_close"), "move_pct": selection.get("move_pct"),
                "status": row.get("status"), "error_class": diag.get("error_class", ""),
                "error_message": diag.get("error_message", row.get("reason", "")),
            })
        _write_csv(output_dir / f"prefilter_{market_date}.csv", ranked_rows)
    _write_csv(output_dir / "symbol_failure_breakdown.csv", failure_rows)
    _write_csv(output_dir / "twelve_data_recall_comparison.csv", comparison_rows)
    failure_counts: dict[str, int] = {}
    for row in failure_rows:
        if row["status"] != "PASS":
            kind = row["error_class"] or row["error_message"] or "UNKNOWN"
            failure_counts[kind] = failure_counts.get(kind, 0) + 1
    selected_dates_file = MILESTONE / "selected_dates.csv"
    remaining_dates = 0
    if selected_dates_file.is_file():
        with selected_dates_file.open(encoding="utf-8-sig", newline="") as handle:
            remaining_dates = max(0, sum(1 for _ in csv.DictReader(handle)) - len(market_dates))
    average_candidates = round(sum(day["candidate_count"] for day in day_summaries.values()) / max(1, len(day_summaries)))
    summary = {
        "dates": day_summaries,
        "failure_class_counts": failure_counts,
        "yahoo_http_requests": request_count,
        "twelve_data_live_requests": 0,
        "twelve_data_live_credits": 0,
        "cost_estimate": {
            "credits_before_per_day": 120, "credits_after_per_day": average_candidates,
            "credits_saved_per_day": 120 - average_candidates,
            "remaining_milestone_dates_estimated": remaining_dates,
            "credits_saved_remaining_estimate": (120 - average_candidates) * remaining_dates,
        },
        "root_cause_class": "HISTORICAL_RANGE_TOO_SHORT; symbol normalization is an additional BRK.B issue where confirmed by diagnostics",
        "root_cause": "The historical mover prefilter requested range=10d relative to execution time, then discarded every observation newer than the requested historical date. That left too few bars to determine both the target close and the preceding valid trading-session close, causing FEWER_THAN_TWO_DISCOVERY_BARS and a full-universe coverage fallback. It is the same failure class as the earlier DXY current-relative-range defect, but in the separate movers_prefilter acquisition path.",
        "ready_for_phase_b_resume": ready,
        "blocker": "" if ready else "Yahoo coverage, safety-gate fallback, candidate size, or cached-truth recall failed acceptance.",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    detail_lines = [
        "# Historical Yahoo Prefilter Recovery V1 — Diagnosis", "", summary["root_cause"], "",
        "Historical requests use bounded period1/period2 daily chart bounds; period2 is exclusive, so the request ends at D+1. Bars are normalized using Yahoo exchangeTimezoneName, later bars are discarded, and the immediately preceding valid returned session is used as previous close. A 21-calendar-day lookback tolerates weekends, holidays and occasional missing sessions.", "",
        "## Targeted symbols", "",
    ]
    for symbol in sorted(DIAG_SYMBOLS):
        diag = diag_by_symbol.get(symbol)
        if not diag:
            continue
        detail_lines.append(f"- {symbol} -> {diag['yahoo_symbol']}; request={diag['request_type']} {diag['requested_start']}..{diag['requested_end']}; timezone={diag.get('exchange_timezone')}; raw_dates={','.join(diag.get('raw_dates', []))}; result={diag['status']}; error={diag.get('error_class', '')}: {diag.get('error_message', '')}")
        for target, selection in diag.get("date_selections", {}).items():
            detail_lines.append(f"  - {target}: current={selection.get('target_date_bar')} previous={selection.get('previous_valid_session_bar')} close={selection.get('close')} previous_close={selection.get('previous_close')} move_pct={selection.get('move_pct')} status={selection.get('status')} reason={selection.get('reason', '')}")
    detail_lines.extend(["", "## Failure classes across both dates", "", json.dumps(failure_counts, ensure_ascii=False, sort_keys=True), "", "See symbol_failure_breakdown.csv for every ticker/date: exact requested range, raw sessions, exchange timezone, selected target/previous bars, values and any provider exception/status.", ""])
    (output_dir / "diagnosis.md").write_text("\n".join(detail_lines), encoding="utf-8")
    summary_lines = ["# Historical Yahoo Prefilter Recovery V1 — Summary", "", "| Date | Valid | Failed | Coverage | Candidates | Fallback | Gainer recall | Loser recall | Ready |", "|---|---:|---:|---:|---:|---|---:|---:|---|"]
    for market_date, result in day_summaries.items():
        summary_lines.append(f"| {market_date} | {result['valid_symbols']} | {result['failed_symbols']} | {result['coverage_pct']:.2f}% | {result['candidate_count']} | {result['fallback_required']} | {result['gainers_recall']:.0%} | {result['losers_recall']:.0%} | {result['ready']} |")
    summary_lines.extend(["", f"Yahoo requests: {request_count}. Twelve Data requests/credits used: 0.", f"Estimated savings: {120-average_candidates} credits per date; {(120-average_candidates)*remaining_dates} across an estimated {remaining_dates} remaining selected dates, assuming candidate count remains similar.", f"READY_FOR_PHASE_B_RESUME = {str(ready).lower()}.", ""])
    (output_dir / "summary.md").write_text("\n".join(summary_lines), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dates", default=",".join(SAMPLE_DATES))
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    summary = run_recovery([value.strip() for value in args.dates.split(",") if value.strip()], output_dir=args.output_dir)
    print(json.dumps({"output_dir": str(args.output_dir), "dates": summary["dates"], "ready_for_phase_b_resume": summary["ready_for_phase_b_resume"], "twelve_data_live_credits": 0}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
