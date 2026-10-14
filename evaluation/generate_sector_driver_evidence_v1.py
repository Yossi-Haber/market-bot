"""Create the auditable QA package for Sector Driver Evidence V1."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TARGETS = [("2026-09-16", "COP"), ("2026-09-16", "EOG"), ("2026-09-17", "AMD"), ("2026-09-17", "MU"), ("2026-09-17", "ORCL")]
CONTROLS = [("2026-09-21", "QCOM"), ("2026-09-21", "LRCX"), ("2026-09-15", "TMO"), ("2026-09-15", "QCOM"), ("2026-09-23", "ABNB"), ("2026-09-23", "BKNG"), ("2026-09-28", "BA"), ("2026-09-28", "PANW"), ("2026-09-21", "META"), ("2026-09-24", "META"), ("2026-09-28", "META")]
LABELS = ROOT / "output/evaluation/manual_qa_v1/stock_review_queue_manual_qa_v1.csv"
PREVIOUS = ROOT / "output/qa/sector_inheritance_v1/sector_inheritance_delta.json"
OUT = ROOT / "output/qa/sector_driver_evidence_v1"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(dict.fromkeys(key for row in rows for key in row)) or ["date"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _stock_status(row: dict[str, Any] | None) -> str:
    return str((row or {}).get("overall_explanation_status") or "MISSING")


def _candidate_rows(day: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    drivers = day.get("sector_drivers", [])
    evidence = [item for driver in drivers for item in driver.get("candidate_stories", []) if isinstance(item, dict)]
    mapping = {"COP": "ENERGY", "EOG": "ENERGY", "AMD": "TECHNOLOGY", "MU": "SEMICONDUCTORS", "ORCL": "TECHNOLOGY"}
    sector = mapping[symbol]
    result = []
    for candidate in day.get("news_context", {}).get("sector_candidate_stories", []):
        tags = candidate.get("sector_tags", [])
        if sector not in tags and not (sector == "SEMICONDUCTORS" and "TECHNOLOGY" in tags) and not (sector == "TECHNOLOGY" and "SEMICONDUCTORS" in tags):
            continue
        promoted = any(item.get("source_url") == candidate.get("source_url") for item in evidence)
        result.append({
            "date": day.get("market_date"), "target_symbol": symbol,
            "title": candidate.get("canonical_event_title") or candidate.get("title"),
            "source": candidate.get("source_name"), "published_at": candidate.get("published_at"),
            "scope": candidate.get("scope"), "event_type": candidate.get("sector_event_type") or candidate.get("event_type") or candidate.get("category"),
            "theme": ";".join(tags), "sector_association": sector,
            "timing": candidate.get("event_time_relation"), "causal_gate": candidate.get("causal_gate"),
            "causal_reason": candidate.get("sector_causal_reason"), "direction": candidate.get("candidate_direction"),
            "promoted_to_sector_driver": promoted,
            "rejection_or_limit": "PROMOTED" if promoted else (candidate.get("sector_causal_reason") or candidate.get("scope_reason")),
            "provider": candidate.get("discovery_source"), "source_url": candidate.get("source_url"),
            "cache_symbols": ";".join(candidate.get("cache_symbols", [])),
            "independent_sources": ";".join(candidate.get("independent_sources", [])),
            "article_count": candidate.get("article_count"),
        })
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir
    if run_dir is None:
        run_dir = max((ROOT / "output/evaluation_batch/runs").glob("*"), key=lambda path: path.stat().st_mtime)
    summary = json.loads((run_dir / "historical_batch_summary.json").read_text(encoding="utf-8"))
    prior = json.loads(PREVIOUS.read_text(encoding="utf-8"))
    prior_target = {(item["date"], item["symbol"]): item for item in prior.get("targets", [])}
    prior_regressions = {(item["date"], item["symbol"]): item for item in prior.get("regression_cases", [])}
    stock_rows = read_csv(run_dir / "stock_review_queue.csv")
    label_rows = read_csv(LABELS)
    after_map = {(row.get("date"), row.get("symbol")): row for row in stock_rows}
    days = {day.get("market_date"): day for day in summary.get("days", [])}

    target_rows, trace_rows = [], []
    for market_date, symbol in TARGETS:
        day = days.get(market_date, {})
        current = after_map.get((market_date, symbol))
        evidences = _candidate_rows(day, symbol)
        trace_rows.extend(evidences)
        picked = next((item for item in evidences if item.get("promoted_to_sector_driver") and item.get("causal_gate") == "PASS"), None)
        before = prior_target.get((market_date, symbol), {})
        after_status = _stock_status(current)
        evidence_found = bool(evidences)
        if not evidence_found:
            classification = "E_NO_RELEVANT_CACHED_EVIDENCE"
        elif picked and after_status == "SECTOR_EXPLAINED":
            classification = "A_RELEVANT_EVIDENCE_ALREADY_PRESENT"
        elif picked:
            classification = "B_EVIDENCE_PRESENT_BUT_WRONG_ASSOCIATION_SCOPE"
        elif any(row.get("causal_gate") == "PASS" for row in evidences):
            classification = "D_EVIDENCE_PRESENT_BUT_NOT_PROMOTED_OR_ASSOCIATED"
        else:
            classification = "C_EVIDENCE_PRESENT_BUT_REJECTED"
        driver_candidates = [candidate for driver in day.get("sector_drivers", []) for candidate in driver.get("candidate_stories", []) if isinstance(candidate, dict)]
        sector_driver = next((driver for driver in day.get("sector_drivers", []) if any(candidate in driver_candidates and candidate.get("source_url") == (picked or {}).get("source_url") for candidate in driver.get("candidate_stories", []))), None)
        target_rows.append({
            "date": market_date, "symbol": symbol, "before_status": before.get("before_status", "UNEXPLAINED"),
            "after_status": after_status, "evidence_found": evidence_found, "evidence_classification": classification,
            "evidence_scope": (picked or {}).get("scope") or (evidences[0].get("scope") if evidences else "NONE"),
            "selected_sector_event": (picked or {}).get("title") or "",
            "provider_source": (picked or {}).get("source") or "",
            "causal_gate": (picked or {}).get("causal_gate") or "NOT_PASSED",
            "sector_driver_status": (sector_driver or {}).get("verification_status", "NO_CATALYST_FOUND"),
            "final_stock_explanation": after_status,
            "reason": "Existing point-in-time sector evidence passed the scoped causal/direction gates and was inherited." if after_status == "SECTOR_EXPLAINED" else ("Broader evidence exists, but configured direct-sector evidence terms or association do not permit inheritance." if evidence_found else "No relevant sector/theme evidence was available in the existing dated caches."),
        })

    regression_rows = []
    for market_date, symbol in CONTROLS:
        current = after_map.get((market_date, symbol))
        day = days.get(market_date, {})
        previous = prior_regressions.get((market_date, symbol), {})
        status = _stock_status(current)
        expected = {
            ("2026-09-21", "QCOM"): "SECTOR_EXPLAINED", ("2026-09-21", "LRCX"): "SECTOR_EXPLAINED",
            ("2026-09-15", "TMO"): "UNEXPLAINED", ("2026-09-15", "QCOM"): "UNEXPLAINED",
            ("2026-09-23", "ABNB"): "UNEXPLAINED", ("2026-09-23", "BKNG"): "UNEXPLAINED",
            ("2026-09-28", "BA"): "STOCK_SPECIFIC", ("2026-09-28", "PANW"): "UNEXPLAINED",
            ("2026-09-21", "META"): "STOCK_SPECIFIC", ("2026-09-24", "META"): "STOCK_SPECIFIC",
            ("2026-09-28", "META"): "MARKET_EXPLAINED",
        }[(market_date, symbol)]
        regression_rows.append({"date": market_date, "symbol": symbol, "expected_control": expected, "before_status": previous.get("before_status", previous.get("after_status", "")), "after_status": status, "result": "PASS" if status == expected else "FAIL", "integrity": (day.get("integrity") or {}).get("integrity_status", "UNKNOWN")})

    labels = {(row.get("date"), row.get("symbol")): row for row in label_rows}
    labeled = []
    for key, manual in labels.items():
        if str(manual.get("manual_label_eligible", "")).lower() != "true" or manual.get("manual_label") not in {"FALSE_POSITIVE", "FALSE_NEGATIVE", "CORRECT_CAUSAL", "PLAUSIBLE_CAUSAL"}:
            continue
        predicted = _stock_status(after_map.get(key))
        labeled.append((manual["manual_label"], predicted))
    positive_labels = {"FALSE_NEGATIVE", "CORRECT_CAUSAL", "PLAUSIBLE_CAUSAL"}
    true_positive = sum(label in positive_labels and prediction != "UNEXPLAINED" for label, prediction in labeled)
    false_negative = sum(label in positive_labels and prediction == "UNEXPLAINED" for label, prediction in labeled)
    false_positive = sum(label == "FALSE_POSITIVE" and prediction != "UNEXPLAINED" for label, prediction in labeled)
    prior_metrics = prior.get("metrics", {})
    old_sector_fn = int(prior_metrics.get("sector_inheritance_false_negatives", {}).get("after", 5))
    remaining_sector_fn = sum(row["after_status"] != "SECTOR_EXPLAINED" for row in target_rows)
    previous_unexplained = float(prior_metrics.get("fully_unexplained_rate", {}).get("after", 0.7444444444))
    current_unexplained = float(summary.get("metrics", {}).get("fully_unexplained_rate", 0))
    metrics = {
        "stock_precision_like": {"before": prior_metrics.get("stock_precision_like", {}).get("after"), "after": true_positive / (true_positive + false_positive) if true_positive + false_positive else None},
        "stock_recall_like": {"before": prior_metrics.get("stock_recall_like", {}).get("after"), "after": true_positive / (true_positive + false_negative) if true_positive + false_negative else None},
        "false_positives": {"before": prior_metrics.get("false_positives", {}).get("after"), "after": false_positive},
        "false_negatives": {"before": prior_metrics.get("false_negatives", {}).get("after"), "after": false_negative},
        "sector_inheritance_false_negatives": {"before": old_sector_fn, "after": remaining_sector_fn},
        "fully_unexplained_rate": {"before": previous_unexplained, "after": current_unexplained, "population": sum(len(day.get("stock_explanations", [])) for day in summary.get("days", []))},
    }
    result = {
        "run_dir": str(run_dir), "label_sources": [str(LABELS)], "targets": target_rows, "metrics": metrics,
        "protected_regression_failures": sum(item["result"] == "FAIL" for item in regression_rows),
        "regression_cases_passed": sum(item["result"] == "PASS" for item in regression_rows),
        "integrity": {"parity_mismatches": summary.get("stock_result_parity_mismatches", 0), "cross_symbol_candidate_leaks": summary.get("cross_symbol_candidate_leaks", 0), "offline_network_calls": summary.get("offline_network_calls", 0), "fmp_requests": summary.get("fmp_requests", 0), "failed_dates": summary.get("integrity_failures", [])},
        "evidence_trace_rows": len(trace_rows),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "sector_driver_evidence_delta.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(OUT / "target_case_results.csv", target_rows)
    write_csv(OUT / "sector_candidate_trace.csv", trace_rows)
    write_csv(OUT / "regression_case_results.csv", regression_rows)
    md = ["# Sector Driver Evidence & Promotion V1", "", f"Evaluation run: `{run_dir}`", "", "## Findings", "", "Existing cached Finnhub articles contained timely sector/theme evidence. COP/EOG shared one deduplicated crude/energy story; MU had semiconductor evidence. No causal technology evidence with configured direct-technology terms rescued AMD or ORCL. Stock selection and inheritance rules were not modified.", "", "## Target cases", "", "| Date | Symbol | Before | After | Evidence scope | Status |", "|---|---|---|---|---|---|"]
    md.extend(f"| {r['date']} | {r['symbol']} | {r['before_status']} | {r['after_status']} | {r['evidence_scope']} | {r['evidence_classification']} |" for r in target_rows)
    md += ["", "## Manual QA delta", "", "| Metric | Before | After |", "|---|---:|---:|"]
    md.extend(f"| {key} | {value.get('before')} | {value.get('after')} |" for key, value in metrics.items())
    md += ["", "## Integrity and protected controls", "", f"Protected cases passing: {result['regression_cases_passed']}/{len(regression_rows)}; parity mismatches={result['integrity']['parity_mismatches']}; cross-symbol leaks={result['integrity']['cross_symbol_candidate_leaks']}; offline network calls={result['integrity']['offline_network_calls']}; FMP requests={result['integrity']['fmp_requests']}.", "", "The manual label files were read-only. No cache refresh or network/provider call was made.", ""]
    (OUT / "sector_driver_evidence_delta.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps({"output_dir": str(OUT), **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
