"""Build the offline Expanded Manual QA Wave 2 packet.

Reads the interim QA queues, merged Ground Truth V2, and the frozen offline
replay. Writes only the Wave 2 QA package; it never calls providers or writes
cache/label data.
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / "output" / "qa"
SOURCE = QA / "interim_29_day_manual_qa_v1"
GT = QA / "ground_truth_v2_wave1_merged" / "current_model_verdicts.csv"
REPLAY_PATH = ROOT / "output/evaluation_extended_v2/30_day_milestone/resume_after_dxy/replays/resume_20261005_160401/evaluation/market_driver_multiday.json"
OUT = QA / "expanded_manual_qa_wave2_v1"

# Deliberately curated, fixed P1 sample: nine market sessions and sixteen
# stock cases. The exact keys are checked against current source/ground-truth.
MARKET_P1 = [
    "2026-08-17", "2026-08-20", "2026-08-21", "2026-08-24", "2026-08-31",
    "2026-09-02", "2026-09-04", "2026-09-08", "2026-09-09",
]
STOCK_P1 = [
    ("2026-08-27", "SNPS"), ("2026-09-04", "KLAC"),
    ("2026-09-03", "NOW"), ("2026-09-08", "BKNG"),
    ("2026-08-18", "MRVL"), ("2026-08-27", "PANW"),
    ("2026-08-20", "DE"), ("2026-08-20", "MRVL"),
    ("2026-08-27", "NVDA"), ("2026-08-28", "MRVL"),
    ("2026-09-04", "TSLA"), ("2026-09-01", "PANW"),
    ("2026-09-04", "NFLX"),
    ("2026-09-01", "SNPS"), ("2026-09-10", "LRCX"),
    ("2026-09-10", "MU"),
]
HUMAN_FIELDS = ["human_driver_state", "human_confidence", "human_event_summary", "human_source_note"]
SECTORS = {
    "SNPS": "Information Technology / software", "PYPL": "Financials / payments",
    "SYK": "Health Care / medical devices", "BKNG": "Consumer Discretionary / travel",
    "PANW": "Information Technology / cybersecurity", "ETN": "Industrials / electrical equipment",
    "DE": "Industrials / machinery", "MRVL": "Information Technology / semiconductors",
    "NVDA": "Information Technology / semiconductors", "TSLA": "Consumer Discretionary / autos",
    "ACN": "Information Technology / IT services", "ADBE": "Information Technology / software",
    "LRCX": "Information Technology / semiconductor equipment", "MU": "Information Technology / semiconductors",
    "KLAC": "Information Technology / semiconductor equipment", "NOW": "Information Technology / enterprise software",
    "NFLX": "Communication Services / streaming",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def market_diagnostics(date: str, day: dict[str, Any], cache_root: Path, replay: dict[str, Any]) -> dict[str, Any]:
    metrics = day.get("market_candidate_metrics") or {}
    cached_file = cache_root / f"{date}.json"
    cache_doc: dict[str, Any] = {}
    if cached_file.is_file():
        try:
            cache_doc = json.loads(cached_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache_doc = {}
    # Presence, not inferred relevance: these booleans state that source-side
    # cached records exist for the date. Human adjudication decides relevance.
    day_stock = [r for r in replay.get("stock_records", []) if r.get("date") == date]
    symbol_evidence = any(as_int(r.get("candidate_count")) > 0 for r in day_stock)
    sector_evidence = bool(day.get("sector_drivers") or day.get("sector_driver_analysis"))
    coverage = day.get("cache_coverage") or {}
    other_evidence = any(coverage.get(k) == "AVAILABLE" for k in ("marketaux", "official", "tier2"))
    return {
        "market_discovery_candidate_count": as_int(metrics.get("raw_candidate_count", cache_doc.get("raw_candidate_count"))),
        "market_discovery_query_count": len(cache_doc["queries"]) if isinstance(cache_doc.get("queries"), list) else "",
        "market_discovery_status": day.get("market_discovery_cache_state") or ("HIT" if cached_file.exists() else "MISSING"),
        "symbol_scoped_cache": symbol_evidence,
        "sector_cache": sector_evidence,
        "other_cached_news": other_evidence,
        "market_discovery_cache_path": str(cached_file.relative_to(ROOT)) if cached_file.exists() else "",
    }


def stock_diagnostics(row: dict[str, str], record: dict[str, Any] | None) -> dict[str, Any]:
    record = record or {}
    candidates = record.get("candidate_stories") or []
    selected = row.get("selected_event") or record.get("selected_event") or ""
    rejected = record.get("best_rejected_candidate")
    if isinstance(rejected, dict):
        rejected_text = rejected.get("title") or rejected.get("canonical_event") or ""
    else:
        rejected_text = str(rejected or "")
    return {
        "cached_candidate_count": as_int(row.get("candidate_count"), len(candidates)),
        "selected_candidate": selected,
        "selected_event_type": row.get("event_type") or record.get("event_type") or "",
        "subject_role": (record.get("selected_subject_role") or record.get("subject_role") or ""),
        "causal_gate": row.get("causal_gate") or record.get("causal_gate") or "",
        "verification_status": row.get("verification_status") or record.get("verification_status") or "",
        "inheritance_source": row.get("inherited_sector_driver") or row.get("inherited_market_driver") or record.get("inherited_sector_driver") or record.get("inherited_market_driver") or "",
        "final_explanation": row.get("overall_explanation_status") or record.get("overall_explanation_status") or "",
        "best_existing_cached_candidate": rejected_text if (row.get("overall_explanation_status") == "UNEXPLAINED") else "",
        "best_candidate_rejection_reason": (record.get("rejection_reason") or row.get("causal_reason") or "") if (row.get("overall_explanation_status") == "UNEXPLAINED") else "",
    }


def main() -> None:
    market_source = read_csv(SOURCE / "market_review_queue.csv")
    stock_source = read_csv(SOURCE / "stock_review_queue.csv")
    reviewed = read_csv(GT)
    replay = json.loads(REPLAY_PATH.read_text(encoding="utf-8"))
    day_map = {day["market_date"]: day for day in replay["days"]}
    stock_map = {(r["date"], r["symbol"]): r for r in replay["stock_records"]}
    reviewed_market = {r["date"] for r in reviewed if r.get("scope") == "MARKET"}
    reviewed_stock = {(r["date"], r.get("symbol", "")) for r in reviewed if r.get("scope") == "STOCK"}

    market_pool = [r for r in market_source if r.get("review_status") == "NEW_UNLABELED" and r["date"] not in reviewed_market and r["date"] != "2026-09-22"]
    stock_pool = [r for r in stock_source if r.get("review_status") == "NEW_UNLABELED" and (r["date"], r["symbol"]) not in reviewed_stock and r["date"] != "2026-09-22"]
    market_index = {r["date"]: r for r in market_pool}
    stock_index = {(r["date"], r["symbol"]): r for r in stock_pool}

    selected_market = [market_index[d] for d in MARKET_P1 if d in market_index and market_index[d].get("integrity_status") == "PASS"]
    selected_stock = [stock_index[k] for k in STOCK_P1 if k in stock_index and stock_index[k].get("integrity_status") == "PASS"]
    if len(selected_market) != len(MARKET_P1) or len(selected_stock) != len(STOCK_P1):
        raise RuntimeError(f"Wave 2 curated key mismatch: market={len(selected_market)}/{len(MARKET_P1)} stock={len(selected_stock)}/{len(STOCK_P1)}")
    absent_replay_days = sorted({r["date"] for r in selected_market if r["date"] not in day_map} | {d for d, _ in STOCK_P1 if (d, _) not in stock_map})
    if absent_replay_days:
        raise RuntimeError(f"Curated Wave 2 keys missing from frozen replay: {absent_replay_days}")

    replay_cache = REPLAY_PATH.parents[1] / "cache" / "market_driver_news"
    selected: list[dict[str, Any]] = []
    rationale: list[dict[str, Any]] = []
    for row in selected_market:
        date = row["date"]
        day = day_map[date]
        diag = market_diagnostics(date, day, replay_cache, replay)
        driver = row.get("primary_market_driver") or row.get("selected_market_driver") or ""
        expl = "MARKET_EXPLAINED" if driver else "UNEXPLAINED"
        record = {
            "date": date, "scope": "MARKET", "symbol": "", "priority": "P1",
            "move_pct": row.get("SPY_change_pct"), "model_explanation_state": expl,
            "selected_candidate": driver, "selected_event_type": row.get("market_regimes", ""),
            "market_context": "; ".join(f"{k}={row.get(k, '')}" for k in ("SPY_change_pct", "QQQ_change_pct", "DIA_change_pct", "IWM_change_pct", "US10Y_change_bp", "VIX_change_pct", "WTI_change_pct", "breadth_up", "breadth_down")),
            "system_rationale": row.get("review_notes") or f"Regime={row.get('market_regimes')}; driver={driver or 'none selected'}; discovery={diag['market_discovery_candidate_count']} candidates.",
            "market_discovery_candidate_count": diag["market_discovery_candidate_count"],
            "market_discovery_query_count": diag["market_discovery_query_count"],
            "market_discovery_status": diag["market_discovery_status"],
            "symbol_scoped_cache": diag["symbol_scoped_cache"], "sector_cache": diag["sector_cache"],
            "other_cached_news": diag["other_cached_news"],
            "human_driver_state": "", "human_confidence": "", "human_event_type": "",
            "human_event_summary": "", "human_source_note": "",
        }
        selected.append(record)
        rationale.append({"date": date, "scope": "MARKET", "symbol": "", "priority": "P1", "selection_rationale": "Market recall information gain: broad-index/breadth and cross-asset context; balances selected-driver and no-driver sessions.", "diversity_dimension": f"{expl}; {row.get('market_regimes')}; discovery={diag['market_discovery_status']}/{diag['market_discovery_candidate_count']}"})

    for row in selected_stock:
        date, symbol = row["date"], row["symbol"]
        record = stock_map.get((date, symbol))
        diag = stock_diagnostics(row, record)
        category = row.get("overall_explanation_status", "")
        sector = row.get("sector") or SECTORS.get(symbol, "Sector not recorded in source queue")
        review = {
            "date": date, "scope": "STOCK", "symbol": symbol, "priority": "P1",
            "move_pct": row.get("move_pct"), "sector": sector,
            "model_explanation_state": category, "selected_candidate": diag["selected_candidate"],
            "selected_event_type": diag["selected_event_type"], "subject_role": diag["subject_role"],
            "causal_gate": diag["causal_gate"], "verification_status": diag["verification_status"],
            "inheritance_source": diag["inheritance_source"], "final_explanation": diag["final_explanation"],
            "cached_candidate_count": diag["cached_candidate_count"],
            "best_existing_cached_candidate": diag["best_existing_cached_candidate"],
            "best_candidate_rejection_reason": diag["best_candidate_rejection_reason"],
            "system_rationale": row.get("why_system_selected_it", ""),
            "human_driver_state": "", "human_confidence": "", "human_event_type": "",
            "human_event_summary": "", "human_source_note": "",
        }
        selected.append(review)
        rationale.append({"date": date, "scope": "STOCK", "symbol": symbol, "priority": "P1", "selection_rationale": f"Tests {category} attribution with {row.get('event_type')} signal; selected for explanation-state/event diversity, not move size alone.", "diversity_dimension": f"{sector}; {row.get('event_type')}; {category}; {'positive' if as_float(row.get('move_pct')) > 0 else 'negative'} move"})

    # Priority tiers describe the complete remaining unlabeled pool; only the
    # curated Wave 2 rows are P1. Integrity holds are P3 and never reviewed.
    p1_keys = {("MARKET", r["date"], "") for r in selected_market} | {("STOCK", r["date"], r["symbol"]) for r in selected_stock}
    queue: list[dict[str, Any]] = []
    for scope, rows in (("MARKET", market_pool), ("STOCK", stock_pool)):
        for row in rows:
            key = (scope, row["date"], row.get("symbol", ""))
            if key in p1_keys:
                tier, reason = "P1", "Selected into Wave 2 for high information value and coverage balance."
            elif row.get("integrity_status") != "PASS":
                tier, reason = "P3", "Integrity blocked; exclude from human causal review until repaired."
            elif scope == "STOCK" and (abs(as_float(row.get("move_pct"))) >= 6 or row.get("overall_explanation_status") in {"STOCK_SPECIFIC", "MARKET_EXPLAINED", "SECTOR_EXPLAINED"}):
                tier, reason = "P2", "Useful residual review case, but lower incremental diversity than the selected P1 set."
            elif scope == "MARKET" and (abs(as_float(row.get("SPY_change_pct"))) >= 0.6 or abs(as_float(row.get("QQQ_change_pct"))) >= 1.0 or row.get("primary_market_driver")):
                tier, reason = "P2", "Useful market regime/driver case not selected in this wave."
            else:
                tier, reason = "P3", "Lower incremental information for Wave 2; retain for later review."
            queue.append({**row, "scope": scope, "priority_v1": tier, "priority_reason": reason, **{f: "" for f in HUMAN_FIELDS}})

    OUT.mkdir(parents=True, exist_ok=True)
    review_fields = ["date", "scope", "symbol", "priority", "move_pct", "sector", "model_explanation_state", "selected_candidate", "selected_event_type", "subject_role", "causal_gate", "verification_status", "inheritance_source", "final_explanation", "cached_candidate_count", "best_existing_cached_candidate", "best_candidate_rejection_reason", "market_context", "system_rationale", "market_discovery_candidate_count", "market_discovery_query_count", "market_discovery_status", "symbol_scoped_cache", "sector_cache", "other_cached_news", *HUMAN_FIELDS]
    write_csv(OUT / "wave2_manual_review.csv", selected, review_fields)
    all_source_fields = list(dict.fromkeys(["priority_v1", "priority_reason", "scope"] + list(market_source[0]) + list(stock_source[0]) + HUMAN_FIELDS))
    queue.sort(key=lambda r: ({"P1": 0, "P2": 1, "P3": 2}[r["priority_v1"]], r["date"], r["scope"], r.get("symbol", "")))
    write_csv(OUT / "remaining_priority_queue.csv", queue, all_source_fields)
    write_csv(OUT / "wave2_selection_rationale.csv", rationale, ["date", "scope", "symbol", "priority", "selection_rationale", "diversity_dimension"])

    tier_counts = Counter(r["priority_v1"] for r in queue)
    stock_explanation_counts = Counter(r["model_explanation_state"] or "UNSPECIFIED" for r in selected if r["scope"] == "STOCK")
    market_explanation_counts = Counter(r["model_explanation_state"] or "UNSPECIFIED" for r in selected if r["scope"] == "MARKET")
    stock_event_counts = Counter(r["selected_event_type"] or "UNSPECIFIED" for r in selected if r["scope"] == "STOCK")
    market_regime_counts = Counter(r["selected_event_type"] or "UNSPECIFIED" for r in selected if r["scope"] == "MARKET")
    dates = sorted({r["date"] for r in selected})
    sectors = sorted({r["sector"] for r in selected if r["scope"] == "STOCK"})
    market_selected = [r for r in selected if r["scope"] == "MARKET"]
    stock_selected = [r for r in selected if r["scope"] == "STOCK"]
    summary = {
        "source_replay": str(REPLAY_PATH.relative_to(ROOT)),
        "remaining_unlabeled_rows": {"market": len(market_pool), "stock": len(stock_pool), "total": len(market_pool) + len(stock_pool)},
        "wave2": {"rows": len(selected), "market": len(market_selected), "stock": len(stock_selected), "distinct_dates": len(dates), "dates": dates, "distinct_sectors": len(sectors), "sectors": sectors, "stock_explanation_state_distribution": dict(stock_explanation_counts), "market_explanation_state_distribution": dict(market_explanation_counts), "stock_event_type_distribution": dict(stock_event_counts), "market_regime_distribution": dict(market_regime_counts), "market_rows_empty_market_discovery": sum(r["market_discovery_candidate_count"] == 0 for r in market_selected), "stock_rows_cached_but_rejected": sum(r["model_explanation_state"] == "UNEXPLAINED" and r["cached_candidate_count"] > 0 for r in stock_selected)},
        "remaining_priority_counts": {k: tier_counts.get(k, 0) for k in ("P1", "P2", "P3")},
        "excluded": {"already_reviewed_ground_truth_v2": len(reviewed), "sep_22": sum(r.get("date") == "2026-09-22" for r in market_source + stock_source), "integrity_blocked_remaining": sum(r.get("integrity_status") != "PASS" for r in market_pool + stock_pool), "duplicate_wave2_keys": 0},
        "label_safety": "All human label fields are blank. No automatic labels, network requests, or cache writes are performed.",
    }
    (OUT / "wave2_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md = ["# Expanded Manual QA Wave 2 — summary", "", f"Remaining unlabeled pool: **{summary['remaining_unlabeled_rows']['total']}** ({summary['remaining_unlabeled_rows']['market']} market, {summary['remaining_unlabeled_rows']['stock']}; includes {summary['excluded']['integrity_blocked_remaining']} integrity-blocked rows retained as P3).", f"Wave 2: **{len(selected)} P1 cases** — {len(market_selected)} market and {len(stock_selected)} stock, across {len(dates)} dates and {len(sectors)} stock-sector labels.", f"Stock explanation states: `{dict(stock_explanation_counts)}`; market explanation states: `{dict(market_explanation_counts)}`.", f"Stock event types: `{dict(stock_event_counts)}`.", f"Market cases with zero market-discovery candidates: **{summary['wave2']['market_rows_empty_market_discovery']}**; unexplained stock cases with cached candidates: **{summary['wave2']['stock_rows_cached_but_rejected']}**.", f"Remaining queue tiers: P1={tier_counts.get('P1',0)}, P2={tier_counts.get('P2',0)}, P3={tier_counts.get('P3',0)}.", "", "## Selection design", "The sample balances unexplained large moves with explicit stock-specific and inherited market-explained cases, and includes earnings, analyst, M&A, regulatory, and other signals across positive/negative moves and varied dates/sectors. Market cases mix sessions with and without a selected market driver and retain cross-asset/discovery-cache diagnostics.", "", "## Review safety", "Ground Truth V2 rows and Wave 1 rows are excluded by their merged date/symbol keys. Sep 22 and integrity-blocked rows are excluded from the review packet (integrity-blocked rows remain visible only in the priority queue as P3). All human label fields are blank; no provider, cache, or production files are modified.", "", "## Outputs"]
    md += [f"- `{name}`" for name in ("wave2_manual_review.csv", "wave2_summary.md", "wave2_summary.json", "remaining_priority_queue.csv", "wave2_selection_rationale.csv")]
    (OUT / "wave2_summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"OUTPUT_DIR={OUT}")


if __name__ == "__main__":
    main()
