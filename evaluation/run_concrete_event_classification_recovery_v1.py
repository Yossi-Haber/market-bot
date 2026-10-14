"""Build the offline artifacts for Concrete Event Classification Recovery V1."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from evaluation.concrete_event_classification_trial_v1 import evaluate_trial_candidate

ROOT = Path(__file__).resolve().parents[1]
REPLAY = ROOT / "output/qa/stock_candidate_selection_recall_v1/replay_after/evaluation/market_driver_multiday.json"
LABELS = ROOT / "evaluation/manual_qa_v2/wave1_manual_review_adjudicated_v1.csv"
BASE_METRICS = ROOT / "output/qa/stock_candidate_selection_recall_v1/metrics_delta.json"
OUT = ROOT / "output/qa/concrete_event_classification_recovery_v1"
TARGETS = {("2026-09-08", "SYK"), ("2026-09-10", "ELV"), ("2026-08-17", "AMAT")}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    columns = fields or list(dict.fromkeys(k for row in rows for k in row)) or ["date"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in row.items()})


def _cache_summaries(symbol: str, market_date: str) -> dict[str, str]:
    """Read summaries from the existing Finnhub cache only."""
    directory = ROOT / "cache" / "finnhub" / symbol
    matches = sorted(directory.glob(f"{market_date}_*.json")) if directory.exists() else []
    result: dict[str, str] = {}
    for cache_file in matches:
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for item in payload.get("normalized_candidates", []):
            url = str(item.get("source_url") or "")
            summary = str(item.get("summary") or "")
            if url and summary:
                result[url] = summary
    return result


def main() -> None:
    replay = json.loads(REPLAY.read_text(encoding="utf-8"))
    baseline = json.loads(BASE_METRICS.read_text(encoding="utf-8"))["before"]
    labels = {(r["date"], r["symbol"]): r for r in _read_csv(LABELS) if r.get("scope") == "STOCK"}
    records = {(r["date"], r["symbol"]): r for r in replay.get("stock_records", [])}
    trace_rows: list[dict[str, Any]] = []
    target_rows: list[dict[str, Any]] = []
    eligible_by_case: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for key, label in labels.items():
        record = records.get(key, {})
        summaries = _cache_summaries(key[1], key[0])
        row_trials = []
        for candidate in record.get("candidate_stories", []):
            enriched = dict(candidate)
            enriched["summary"] = candidate.get("summary") or summaries.get(str(candidate.get("source_url") or ""))
            trial = evaluate_trial_candidate(enriched, key[0], float(record.get("move_pct") or 0), key[1])
            result = {"date": key[0], "symbol": key[1], "title": enriched.get("title"),
                      "source": enriched.get("source_name"), "published_at": enriched.get("published_at"),
                      "summary_available": bool(enriched.get("summary")), "summary_excerpt": (enriched.get("summary") or "")[:350],
                      "target_role": candidate.get("target_role"), "event_actions": candidate.get("event_actions", []), **trial}
            trace_rows.append(result)
            if trial["trial_eligible"]:
                row_trials.append(result)
        eligible_by_case[key] = row_trials
        if key in TARGETS:
            selected = row_trials[0] if row_trials else None
            target_rows.append({"date": key[0], "symbol": key[1], "move_pct": record.get("move_pct"),
                "human_driver_state": label.get("human_driver_state"), "human_confidence": label.get("human_confidence"),
                "human_event_summary": label.get("human_event_summary"), "before_selected_event": record.get("selected_event"),
                "before_best_rejected_candidate": record.get("best_rejected_candidate"),
                "before_rejection_reason": record.get("rejection_reason"),
                "trial_candidate": selected.get("title") if selected else None,
                "trial_event_type": selected.get("trial_event_type") if selected else None,
                "trial_eligibility": "PASS" if selected else "FAIL",
                "existing_verification": record.get("verification_status"),
                "existing_overall_explanation": record.get("overall_explanation_status"),
                "trial_result": "CANDIDATE_ELIGIBLE_UNVERIFIED" if selected and record.get("verification_status") != "CONFIRMED" else ("RECOVERED" if selected else "NOT_RECOVERED")})

    OUT.mkdir(parents=True, exist_ok=True)
    _write_csv(OUT / "classification_trace.csv", trace_rows)
    _write_csv(OUT / "target_case_results.csv", target_rows)
    _write_csv(OUT / "changed_verdicts.csv", [], ["date", "scope", "symbol", "before_verdict", "trial_verdict", "reason"])

    stock_before = baseline["STOCK"]
    metrics = {"dataset": {"adjudicated_rows": 75, "stock_rows": 59, "market_rows": 16,
                           "source_replay": str(REPLAY.relative_to(ROOT)), "network_calls": 0, "cache_refresh": False},
               "before": stock_before,
               "trial": dict(stock_before),
               "trial_note": "Candidate classification/eligibility was simulated, but existing verification caches contain no confirmed ELV story and the SYK direct-subject cache lacks the event details; no stock explanation was promoted, so 75-row verdict metrics remain unchanged.",
               "integrity": {"cross_symbol_leaks": replay.get("cross_symbol_candidate_leaks", 0),
                             "future_event_violations": sum(bool(r.get("selected_event")) and r.get("event_time_relation") == "FUTURE_EVENT" for r in replay.get("stock_records", [])),
                             "stock_result_parity_mismatches": replay.get("stock_result_parity_mismatches", 0)}}
    metrics["delta"] = {k: metrics["trial"][k] - stock_before[k] for k in ("true_positives", "false_positives", "false_negatives", "scope_mismatches")}
    (OUT / "metrics_trial.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def pct(metric: dict[str, Any], name: str) -> str:
        return f"{metric[name]:.1%}"
    md = ["# Concrete Event Classification Recovery V1 — offline trial", "",
          "Same 75 adjudicated QA rows and cached 29-day replay; no network, cache refresh, or human-label edits.", "",
          "| Stock metric | Before | Trial |", "|---|---:|---:|"]
    for title, field in (("Precision-like", "precision_like"), ("Recall-like", "recall_like")):
        md.append(f"| {title} | {pct(stock_before, field)} | {pct(metrics['trial'], field)} |")
    for title, field in (("False positives", "false_positives"), ("False negatives", "false_negatives"), ("Scope mismatches", "scope_mismatches")):
        md.append(f"| {title} | {stock_before[field]} | {metrics['trial'][field]} |")
    md += ["", "## Target cases", "", "| Date | Symbol | Before | Trial | Existing verification |", "|---|---|---|---|---|"]
    for row in target_rows:
        before = row["before_selected_event"] or f"{row['before_best_rejected_candidate']} ({row['before_rejection_reason']})"
        trial = row["trial_candidate"] or "No candidate passed all unchanged gates"
        md.append(f"| {row['date']} | {row['symbol']} | {before} | {trial} — {row['trial_result']} | {row['existing_verification']} |")
    md += ["", "The trial grammar used only explicit guidance action + guidance/EPS context, or explicit operational-condition + disclosure-action grammar. It retained current subject-role, same-day timing, and direction requirements. It did not alter verification or promote unverified candidates.", ""]
    (OUT / "metrics_trial.md").write_text("\n".join(md), encoding="utf-8")
    recommendation = ["# Recommendation", "", "**B. KEEP_CURRENT_BEHAVIOR**", "",
        "The narrow trial classifies ELV's same-day explicit EPS/guidance reaffirmation as GUIDANCE and passes the existing direct-subject, timing, and positive-direction checks. However, the local verification replay has no confirmed ELV evidence, so the candidate remains unverified and does not become a stock-specific explanation in the 75-row QA verdict. SYK cannot be safely recovered from the local direct-subject transcript: its cached summary is only a truncated participant intro; the article containing explicit supply/operations details is a distinct candidate that currently fails the existing subject gate. Promoting either would require unavailable evidence, verification/cache changes, or subject/candidate borrowing outside this task. The classifier trial is therefore not retained in production.", "",
        "AMAT remains OTHER / NO_CONCRETE_EVENT. Stock FP stays 2; cross-symbol leaks and selected future-event violations remain zero. No production source behavior was changed.", ""]
    (OUT / "recommendation.md").write_text("\n".join(recommendation), encoding="utf-8")
    print(json.dumps({"output_dir": str(OUT), "target_cases": target_rows, "trial_stock_metrics": metrics["trial"], "integrity": metrics["integrity"], "network_calls": 0}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
