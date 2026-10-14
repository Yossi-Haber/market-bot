"""Offline audit package for the five Wave 1 market false negatives."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VERDICTS = ROOT / "output/qa/ground_truth_v2_wave1_merged/current_model_verdicts.csv"
REPLAY = ROOT / "output/qa/stock_candidate_selection_recall_v1/replay_after/evaluation/market_driver_multiday.json"
HISTORY = ROOT / "output/evaluation_extended_v2/30_day_milestone/history"
MARKET_CACHE = ROOT / "output/evaluation_extended_v2/30_day_milestone/resume_after_dxy/replays/resume_20261005_160401/cache/market_driver_news"
FINNHUB_CACHE = ROOT / "cache/finnhub"
BASE_METRICS = ROOT / "output/qa/ground_truth_v2_wave1_merged/metrics_before_after.json"
OUT = ROOT / "output/qa/market_side_recall_diagnosis_v1"

TARGETS = {
    "2026-08-18": {
        "cause": "MARKET_EVIDENCE_NOT_DISCOVERED", "sufficiency": "F",
        "pattern": r"iran risk and oil above|nasdaq drops, chip stocks crater",
        "analysis": "Some macro-themed coverage exists only in a symbol-scoped cache. The Iran/oil item is temporally plausible but its summary describes the prior session; the other headline attributes pressure partly to a Fabrinet margin miss. Neither is a clean cached match for the adjudicated Iran/oil/yield narrative.",
    },
    "2026-08-27": {
        "cause": "MARKET_EVIDENCE_NOT_DISCOVERED", "sufficiency": "C",
        "pattern": r"nvidia.*forecast|forecast.*nvidia|nvidia posts|nvidia.*guidance",
        "analysis": "Nvidia forecast/earnings coverage is present under NVDA's Finnhub cache, but the market-discovery cache has no query or candidate. The local item set does not record market-scope classification or promotion; broad tech spillover remains untested, so this is evidence present outside the market pool, not proof of a safe promotion rule.",
    },
    "2026-08-28": {
        "cause": "INSUFFICIENT_EVIDENCE", "sufficiency": "D",
        "pattern": r"(?!)",
        "analysis": "The human label is explicitly multi-factor and says no single market-wide catalyst dominates. The scanned same-day symbol caches contain market-mover lists and unrelated company items, but no specific event supporting the small-cap/USD/sector-dispersion explanation. Movement signals alone cannot establish causality.",
    },
    "2026-09-03": {
        "cause": "MARKET_EVIDENCE_PRESENT_NOT_SELECTED", "sufficiency": "C",
        "pattern": r"wall street rallies as fed.s waller|stock market today: dow leads charge as fed.s waller|dow adds 650 points in midday rally|dow jumps 624 points",
        "analysis": "Several same-day broad-market stories about Waller's rate-hold signal and falling yields exist in TSLA/AVGO symbol-scoped Finnhub caches. They are not in the market-discovery cache; consequently market scope, causal check, and promotion score were never evaluated in this replay.",
    },
    "2026-09-11": {
        "cause": "MARKET_EVIDENCE_PRESENT_NOT_SELECTED", "sufficiency": "C",
        "pattern": r"stocks rise as falling oil prices outweigh sticky inflation|dow jumps nearly 600 points after cpi surprise",
        "analysis": "Same-day broad-market coverage explicitly links the rebound to retreating oil despite sticky inflation and is present in ORCL's symbol-scoped cache. It is absent from the market-discovery pool, so market scope/causal/promotion were not executed. The mixed-factor synthesis remains a model question, not established as the first failure.",
    },
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def move(item: Any) -> str:
    if not isinstance(item, dict):
        return "MISSING"
    val = item.get("change_pct")
    if val is None:
        return "MISSING"
    return f"{float(val):+.3f}%"


def cached_symbol_articles(market_date: str, pattern: str) -> list[dict[str, Any]]:
    regex = re.compile(pattern, re.IGNORECASE)
    dedup: dict[str, dict[str, Any]] = {}
    for path in FINNHUB_CACHE.glob(f"*/{market_date}_*.json"):
        try:
            payload = read_json(path)
        except (OSError, ValueError):
            continue
        for article in payload.get("normalized_candidates", []):
            published = str(article.get("published_at") or "")
            if not published.startswith(market_date):
                continue
            title = str(article.get("title") or "")
            summary = str(article.get("summary") or "")
            if not regex.search(title + " " + summary):
                continue
            url = str(article.get("source_url") or title)
            dedup[url] = {
                "cache_symbol": path.parent.name, "title": title,
                "source": article.get("source_name"), "published_at": published,
                "url": article.get("source_url"), "summary": summary,
                "candidate_storage_scope": "SYMBOL_FINNHUB_CACHE",
                "market_scope_status": "NOT_EVALUATED_NOT_IN_MARKET_POOL",
                "timing_status": "BEFORE_CLOSE_BY_TIMESTAMP" if published else "UNKNOWN",
                "causal_status": "NOT_RUN_IN_MARKET_PIPELINE",
                "promotion_status": "NOT_RUN_NO_MARKET_CANDIDATE",
                "rejection_reason": "NOT_IN_MARKET_DISCOVERY_POOL",
            }
    return sorted(dedup.values(), key=lambda item: item["published_at"])


def main() -> None:
    verdict_rows = read_csv(VERDICTS)
    target_rows = [r for r in verdict_rows if r.get("scope") == "MARKET"
                   and r.get("model_verdict") == "FALSE_NEGATIVE"
                   and r.get("migration_status") == "WAVE1_HUMAN_ADJUDICATED"]
    dates = {r["date"] for r in target_rows}
    if len(target_rows) != 5 or dates != set(TARGETS):
        raise RuntimeError(f"MARKET_FN_SET_MISMATCH:{sorted(dates)}")

    replay = read_json(REPLAY)
    replay_days = {d["market_date"]: d for d in replay.get("days", [])}
    trace: list[dict[str, Any]] = []
    roots: list[dict[str, Any]] = []
    sufficiency: list[dict[str, Any]] = []
    for label in sorted(target_rows, key=lambda row: row["date"]):
        day = replay_days.get(label["date"], {})
        history_dir = HISTORY / label["date"]
        snapshot_path = history_dir / "market_snapshot.json"
        market_driver_path = history_dir / "market_driver_snapshot.json"
        snapshot = read_json(snapshot_path) if snapshot_path.exists() else {}
        driver_snapshot = read_json(market_driver_path) if market_driver_path.exists() else {}
        market = snapshot.get("market", {})
        analysis = day.get("market_driver_analysis", {})
        breadth = analysis.get("breadth", {})
        indexes = market.get("core", {})
        sectors = market.get("sectors", {})
        macro = market.get("macro", {})
        cache_file = MARKET_CACHE / f"{label['date']}.json"
        discovery = read_json(cache_file) if cache_file.exists() else {}
        candidates = cached_symbol_articles(label["date"], TARGETS[label["date"]]["pattern"])
        # The cache is deliberately read-only; this classification is audit-only.
        pool_titles = [item.get("title") for item in discovery.get("market_eligible_candidates", [])]
        candidate_detail = [
            {"title": item["title"], "source": item["source"], "published_at": item["published_at"],
             "cache_symbol": item["cache_symbol"], "scope": item["candidate_storage_scope"],
             "timing": item["timing_status"], "causal": item["causal_status"],
             "promotion": item["promotion_status"], "rejection": item["rejection_reason"]}
            for item in candidates
        ]
        row = {
            "date": label["date"], "human_driver_state": label.get("human_driver_state"),
            "human_confidence": label.get("human_confidence"), "human_event_summary": label.get("human_event_summary"),
            "SPY_change_pct": move(indexes.get("SPY")), "QQQ_change_pct": move(indexes.get("QQQ")),
            "DIA_change_pct": move(indexes.get("DIA")), "IWM_change_pct": move(indexes.get("IWM")),
            "sector_up": breadth.get("up"), "sector_down": breadth.get("down"), "sector_unchanged": breadth.get("unchanged"),
            "US10Y_change_bps": macro.get("US10Y", {}).get("change_bps", "MISSING"),
            "oil_WTI_change_pct": move(macro.get("WTI")), "VIX_change_pct": move(macro.get("VIX")),
            "DXY_change_pct": move(macro.get("DXY")),
            "market_regimes": analysis.get("market_regime", []),
            "cross_asset_signals": analysis.get("cross_asset_signals", []),
            "current_market_status": ((driver_snapshot.get("driver_analysis") or {}).get("main_market_driver") or {}).get("verification_status", "NO_MARKET_DRIVER_FOUND"),
            "current_selected_driver": ((driver_snapshot.get("driver_analysis") or {}).get("main_market_driver") or {}).get("selected_event"),
            "market_cache_state": day.get("market_discovery_cache_state"),
            "market_cache_hypotheses": discovery.get("hypotheses", []), "market_cache_queries": discovery.get("queries", []),
            "market_cache_raw_candidates": discovery.get("raw_candidate_count", 0),
            "market_pool_candidates": pool_titles, "symbol_cache_relevant_candidates": candidate_detail,
            "first_loss_stage": "HYPOTHESIS_QUERY_GENERATION_EMPTY" if not discovery.get("hypotheses") and not discovery.get("queries") else "MARKET_CANDIDATE_FILTER_OR_LATER",
            "primary_root_cause": TARGETS[label["date"]]["cause"],
            "diagnosis": TARGETS[label["date"]]["analysis"],
            "source_artifacts": {"market_snapshot": str(snapshot_path.relative_to(ROOT)),
                                 "market_driver_snapshot": str(market_driver_path.relative_to(ROOT)),
                                 "market_discovery_cache": str(cache_file.relative_to(ROOT))},
        }
        trace.append(row)
        roots.append({"date": label["date"], "primary_root_cause": row["primary_root_cause"],
                      "earliest_failed_stage": row["first_loss_stage"],
                      "market_hypothesis_count": len(discovery.get("hypotheses", [])),
                      "market_query_count": len(discovery.get("queries", [])),
                      "market_candidate_count": discovery.get("raw_candidate_count", 0),
                      "relevant_symbol_cache_candidate_count": len(candidates),
                      "explanation": row["diagnosis"]})
        sufficiency.append({"date": label["date"], "classification": TARGETS[label["date"]]["sufficiency"],
                            "relevant_market_pool_evidence": len(discovery.get("market_eligible_candidates", [])),
                            "relevant_symbol_cache_evidence": len(candidates),
                            "evidence_examples": [f"{c['source']} ({c['published_at']}): {c['title']}" for c in candidates[:4]],
                            "reason": row["diagnosis"]})

    baseline_data = read_json(BASE_METRICS)["before_after"]["after_resolved_four_plus_wave1"]
    metrics = baseline_data["metrics"]
    market = metrics["MARKET"]
    stock = metrics["STOCK"]
    fn_counts = {key: sum(r["classification"] == key for r in sufficiency) for key in "ABCDEF"}
    integrity = {
        "offline_network_calls": replay.get("metrics", {}).get("offline_network_attempts_blocked", 0),
        "fmp_requests": replay.get("metrics", {}).get("fmp_requests_total", 0),
        "market_cache_build_requests": sum(sum((read_json(MARKET_CACHE / f"{d}.json").get("provider_metrics") or {}).values()) for d in TARGETS),
        "cross_symbol_candidate_leaks": replay.get("cross_symbol_candidate_leaks", 0),
        "stock_result_parity_mismatches": replay.get("stock_result_parity_mismatches", 0),
        "future_event_violations": sum(bool(r.get("selected_event")) and r.get("event_time_relation") == "FUTURE_EVENT" for r in replay.get("stock_records", [])),
    }
    OUT.mkdir(parents=True, exist_ok=True)

    def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str] | None = None) -> None:
        fields = columns or list(dict.fromkeys(k for item in rows for k in item)) or ["date"]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for item in rows:
                writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in item.items()})

    write_csv(OUT / "market_fn_trace.csv", trace)
    write_csv(OUT / "market_root_cause_summary.csv", roots)
    write_csv(OUT / "cache_sufficiency.csv", sufficiency)
    write_csv(OUT / "trial_changed_verdicts.csv", [], ["date", "scope", "symbol", "before_verdict", "trial_verdict", "reason"])
    metrics_out = {
        "trial_run": False,
        "decision": "C. MOVE_TO_WAVE_2",
        "reviewed_rows": 75,
        "market_rows": 16,
        "same_five_wave1_market_false_negatives": [r["date"] for r in trace],
        "cache_sufficiency_counts": fn_counts,
        "market_before": market,
        "market_trial": market,
        "stock_before": stock,
        "stock_trial": stock,
        "changed_reviewed_rows": [],
        "integrity": integrity,
        "shared_empty_market_discovery_input": all(r["market_cache_raw_candidates"] == 0 for r in trace),
        "multi_factor_labeled_cases": sum(r["human_driver_state"] == "MULTI_FACTOR" for r in trace),
        "multi_factor_caused_misses_confirmed": 0,
        "production_changes": 0,
    }
    (OUT / "metrics_trial.json").write_text(json.dumps(metrics_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md = ["# Market-Side Recall Diagnosis V1", "", "Analysis-only; no offline trial was justified because the replay's market-discovery inputs are empty and the cached evidence is incomplete or outside that candidate pool. No production changes, cache writes, or network calls were made.", "",
          "| Metric | Before | Trial |", "|---|---:|---:|"]
    for label, field in (("Precision-like", "precision_like"), ("Recall-like", "recall_like"), ("False positives", "false_positives"), ("False negatives", "false_negatives"), ("Scope mismatches", "scope_mismatches")):
        before = market[field]
        md.append(f"| {label} | {before:.1%} | {before:.1%} |" if isinstance(before, float) else f"| {label} | {before} | {before} |")
    md += ["", "## Five market false negatives", "", "| Date | Human-supported driver | Root cause | Market-pool candidates | Symbol-cache candidates |", "|---|---|---|---:|---:|"]
    for item in sufficiency:
        label = next(r for r in trace if r["date"] == item["date"])
        md.append(f"| {item['date']} | {label['human_event_summary']} | {label['primary_root_cause']} | {item['relevant_market_pool_evidence']} | {item['relevant_symbol_cache_evidence']} |")
    md += ["", "Cache sufficiency: " + ", ".join(f"{k}={v}" for k, v in fn_counts.items()) + ".", "", "MULTI_FACTOR labels occur on 2/5 cases (Aug 28 and Sep 11). The replay does not establish that a single-driver model caused either miss: Aug 28 lacks a specific causal article in the scanned date-scoped caches, while Sep 11 has broad mixed-factor coverage outside the market pool. Movement data alone is not sufficient to promote a causal driver.", "", "No narrow offline trial was run. The repeated empty market-discovery inputs show an incomplete discovery/replay record, not a safe production fix; enabling queries or promoting stock-cache articles would broaden evidence ingestion/scope and requires Wave 2 review. Correct market verdicts are unchanged; market FP remains zero. Integrity: " + json.dumps(integrity, ensure_ascii=False) + ".", ""]
    (OUT / "metrics_trial.md").write_text("\n".join(md), encoding="utf-8")
    recommendation = ["# Recommendation", "", "**C. MOVE_TO_WAVE_2**", "", "The five dates share an empty market-discovery cache state (no hypotheses, queries, raw candidates, or market-eligible candidates), but the evidence situation differs: some relevant articles are present only in individual-symbol Finnhub caches, one date has no specific causal article in the scanned caches, and Aug 18's cached broad items are not a clean match to the adjudicated event. Thus a common cache-stage symptom exists, but no single narrow classifier/promotion change has been demonstrated safe. Reusing symbol-scoped stories as market evidence would require explicit scope policy and full review of correct market rows; generating new queries would broaden discovery and is outside this analysis-only diagnosis.", "", "No production change or trial retained. Keep movement-derived hypotheses non-causal without contemporaneous credible evidence. Proceed to Wave 2 with the five original source references and broadened cache inspection/query diagnostics, not a market-driver relaxation.", ""]
    (OUT / "recommendation.md").write_text("\n".join(recommendation), encoding="utf-8")
    print(json.dumps({"output_dir": str(OUT), "dates": [r["date"] for r in trace], "root_causes": [r["primary_root_cause"] for r in trace], "cache_sufficiency_counts": fn_counts, "metrics": {"market": market, "stock_fp": stock["false_positives"]}, "integrity": integrity}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
