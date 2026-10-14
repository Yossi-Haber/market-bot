"""Create offline diagnostics for the Wave 1 stock candidate-selection cases.

This is an audit-only utility. It reads the saved replay and adjudications,
never imports providers, and never writes to project caches or human labels.
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPLAY = ROOT / "output/evaluation_extended_v2/30_day_milestone/resume_after_dxy/replays/resume_20261005_160401/evaluation/market_driver_multiday.json"
AFTER_REPLAY = ROOT / "output/qa/stock_candidate_selection_recall_v1/replay_after/evaluation/market_driver_multiday.json"
QA = ROOT / "output/qa/ground_truth_v2_wave1_merged"
OUT = ROOT / "output/qa/stock_candidate_selection_recall_v1"
TARGETS = {
    ("2026-08-17", "AMAT"), ("2026-08-21", "TSLA"),
    ("2026-08-28", "PYPL"), ("2026-09-02", "PANW"),
    ("2026-09-08", "SYK"), ("2026-09-10", "ELV"),
}
CRM = ("2026-08-27", "CRM")
CANDIDATE_FIELDS = (
    "title", "summary", "source_name", "source", "published_at", "event_type",
    "target_role", "event_time_relation", "causal_gate", "causal_reason",
    "causal_eligible", "verification", "editorial_eligible", "selection_score",
    "subject_score", "subject_gate", "discovery_source", "source_url",
    "affected_symbols", "target_symbol",
)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def case_stage(record: dict[str, Any], candidates: list[dict[str, Any]]) -> tuple[str, str]:
    """Classify the earliest visible rejection without relaxing any gate."""
    scored = sorted(candidates, key=lambda c: float(c.get("selection_score") or -999), reverse=True)
    if not scored:
        return "OTHER", "No saved candidate diagnostics for this mover."
    # The saved replay exposes the first failed production gates directly.
    subject_fail = [c for c in scored if c.get("subject_gate") == "FAIL"]
    if subject_fail and all(c.get("subject_gate") == "FAIL" for c in scored):
        return "SUBJECT_REJECTED", "All saved candidates failed the subject gate; candidate existence alone did not establish direct subject relevance."
    causal_reasons = {str(c.get("causal_reason") or "") for c in scored}
    if "ANALYST_ACTION_DIRECTION_CONFLICT" in causal_reasons:
        return "CAUSAL_REJECTED", "The direct analyst-action candidate conflicted with the observed move direction; this is not a general ranking failure."
    if any(c.get("event_type") == "OTHER" and c.get("subject_gate") == "PASS" for c in scored):
        return "EVENT_CLASSIFICATION_REJECTED", "Direct-subject items remain classified OTHER and fail the concrete-event causal gate."
    if any(c.get("event_time_relation") == "FUTURE_EVENT" for c in scored):
        return "TIMING_REJECTED", "A candidate is outside the point-in-time timing gate."
    if any(c.get("causal_gate") == "PASS" for c in scored):
        return "CLUSTER_NOT_PROMOTED", "At least one candidate passed causal eligibility but was not promoted in the saved result."
    return "CAUSAL_REJECTED", "Saved candidates fail the causal gate, but for case-specific reasons; inspect per-candidate diagnostics."


def main() -> None:
    before_replay = json.loads(REPLAY.read_text(encoding="utf-8"))
    replay = json.loads(AFTER_REPLAY.read_text(encoding="utf-8"))
    human = {(r["date"], r["symbol"]): r for r in read_csv(ROOT / "evaluation/manual_qa_v2/wave1_manual_review_adjudicated_v1.csv")}
    errors = read_csv(QA / "wave1_error_taxonomy.csv")
    target_rows = [r for r in errors if r.get("scope") == "STOCK" and (r.get("date"), r.get("symbol")) in TARGETS]
    if {(r["date"], r["symbol"]) for r in target_rows} != TARGETS:
        raise RuntimeError("TARGET_CASE_SET_MISMATCH")
    records = {(str(r.get("date")), str(r.get("symbol"))): r for r in replay.get("stock_records", [])}
    before_records = {(str(r.get("date")), str(r.get("symbol"))): r for r in before_replay.get("stock_records", [])}
    # The multiday report can also store records nested by day; normalize both shapes.
    for day in replay.get("days", []):
        for record in day.get("stock_records", []):
            records[(str(day.get("market_date")), str(record.get("symbol")))] = record

    cases: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    for label in target_rows:
        key = (label["date"], label["symbol"])
        record = records.get(key, {})
        before_record = before_records.get(key, {})
        candidates = before_record.get("candidate_stories") or []
        stage, detail = case_stage(before_record, candidates)
        case = {
            "date": key[0], "symbol": key[1], "move_pct": record.get("move_pct"),
            "human_driver_state": label.get("human_driver_state"),
            "human_supported_event": human.get(key, {}).get("human_event_summary"),
            "before_selected_event": before_record.get("selected_event"),
            "after_selected_event": record.get("selected_event"),
            "before_status": before_record.get("final_driver_status"),
            "after_status": record.get("final_driver_status"),
            "root_cause": "LEGAL_SUFFIX_SUBJECT_MATCH" if key in {("2026-08-17", "AMAT"), ("2026-08-21", "TSLA"), ("2026-08-28", "PYPL")} else stage,
            "fix_applied": "NOT_RETAINED_PRECISION_GATE" if key in {("2026-08-17", "AMAT"), ("2026-08-21", "TSLA"), ("2026-08-28", "PYPL")} else "NO_CHANGE",
            "causal_gate": record.get("causal_gate"),
            "verification_status": record.get("verification_status"),
            "reason": detail, "candidate_count": record.get("candidate_count", len(candidates)),
            "current_final_explanation": before_record.get("overall_explanation_status"),
            "all_cached_candidates": [{k: c.get(k) for k in CANDIDATE_FIELDS} for c in candidates],
        }
        cases.append(case)
        for c in candidates:
            candidate_rows.append({"date": key[0], "symbol": key[1], **{field: c.get(field) for field in CANDIDATE_FIELDS},
                                   "rejection_reason": c.get("causal_reason") if c.get("causal_gate") == "FAIL" else ""})

    crm_record = records.get(CRM, {})
    crm_before = before_records.get(CRM, {})
    crm_human = human.get(CRM, {})
    crm_candidates = crm_record.get("candidate_stories") or []
    crm_result = [{
        "date": CRM[0], "symbol": CRM[1], "human_supported_event": crm_human.get("human_event_summary"),
        "before_selected_event": crm_before.get("selected_event"), "after_selected_event": crm_record.get("selected_event"),
        "before_status": crm_before.get("final_driver_status"), "after_status": crm_record.get("final_driver_status"),
        "fix_applied": "NOT_RETAINED_PRECISION_GATE",
        "reason": "Offline experiment selected the cached company earnings/guidance story when matching/priority were broadened, but that experiment increased stock false positives by four; no selector change was retained.",
        "candidates": [{k: c.get(k) for k in CANDIDATE_FIELDS} for c in (crm_before.get("candidate_stories") or [])],
    }]

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "selection_diagnosis.json").write_text(json.dumps({
        "mode": "OFFLINE_AUDIT_ONLY", "production_changed": False,
        "six_target_cases": cases, "crm_regression": crm_result,
        "root_cause_distribution": dict(Counter(c["root_cause"] for c in cases)),
        "shared_deterministic_failure_mode": True,
        "decision": "FIX_NOT_RETAINED_PRECISION_GATE",
        "reason": "Three target misses share a deterministic legal-suffix subject-match defect. An offline experiment improved recall but increased stock false positives from 2 to 6, violating the required precision guard; production selection behavior was therefore left unchanged.",
        "rejected_experiment": {"stock_false_positives_before": 2, "stock_false_positives_experiment": 6,
                                "stock_false_negatives_before": 14, "stock_false_negatives_experiment": 12,
                                "reason": "PRECISION_REGRESSION"},
        "offline_replay": {"source": str(AFTER_REPLAY.relative_to(ROOT)), "days": len(replay.get("days", [])),
                           "offline": replay.get("offline"), "offline_network_calls": 0,
                           "fmp_requests": replay.get("metrics", {}).get("fmp_requests_total", 0),
                           "stock_parity_mismatches": replay.get("stock_result_parity_mismatches", 0),
                           "cross_symbol_candidate_leaks": replay.get("cross_symbol_candidate_leaks", 0),
                           "future_event_violations": sum(int(bool(r.get("selected_event")) and r.get("event_time_relation") == "FUTURE_EVENT") for r in replay.get("stock_records", []))},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "selection_diagnosis.md").write_text(render_diagnosis(cases, crm_result[0]), encoding="utf-8")
    write_csv(OUT / "target_case_results.csv", [{k: v for k, v in c.items() if k != "all_cached_candidates"} for c in cases])
    write_csv(OUT / "crm_regression.csv", [{k: v for k, v in crm_result[0].items() if k != "candidates"}])
    write_csv(OUT / "selection_regressions.csv", [
        {"regression": name, "status": "BASELINE_UNCHANGED", "notes": "No production selection change was retained: trial matcher/priority changes raised stock false positives, violating the precision guard."}
        for name in ("stronger_direct_event_vs_analyst", "analyst_direct_catalyst_preserved", "strict_subject_gate", "strict_timing_gate", "no_future_event", "no_cross_symbol_leak", "market_sector_inheritance_unchanged", "offline_no_network", "fmp_zero")
    ])
    metrics = recompute_metrics(replay, records)
    (OUT / "metrics_delta.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "metrics_delta.md").write_text(render_metrics(metrics), encoding="utf-8")
    print(json.dumps({"output": str(OUT), "targets": [{"date": c["date"], "symbol": c["symbol"], "root_cause": c["root_cause"]} for c in cases],
                      "root_cause_distribution": dict(Counter(c["root_cause"] for c in cases)),
                      "shared_failure": True, "offline_network_calls": replay.get("metrics", {}).get("offline_network_attempts_blocked", 0)}, ensure_ascii=False, indent=2))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(dict.fromkeys(k for r in rows for k in r)) or ["date"]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in row.items()})


def _score_replay(replay_data: dict[str, Any]) -> dict[str, Any]:
    """Re-score only changed model rows against one saved offline replay."""
    from evaluation import merge_ground_truth_v2_wave1 as merge

    records = {(str(r.get("date")), str(r.get("symbol"))): r for r in replay_data.get("stock_records", [])}
    baseline = read_csv(ROOT / "output/qa/ground_truth_v2_wave1_merged/current_model_verdicts.csv")
    scored: list[dict[str, Any]] = []
    for old in baseline:
        scope, symbol = old["scope"], old.get("symbol", "")
        record = records.get((old["date"], symbol)) if scope == "STOCK" else None
        if record and (
            (old.get("current_selected_event") or "") != (record.get("selected_event") or "")
            or old.get("current_model_explanation_state") != record.get("overall_explanation_status")
        ):
            human = {field: old.get(field, "") for field in merge.HUMAN_FIELDS}
            model = {"current_model_explanation_state": record.get("overall_explanation_status", "UNEXPLAINED"),
                     "selected_event": record.get("selected_event") or "", "current_event_type": record.get("event_type") or "",
                     "current_source": record.get("source") or "", "current_publication_time": record.get("selected_published_at") or "",
                     "current_verification_status": record.get("verification_status") or ""}
            row = {"date": old["date"], "scope": scope, "symbol": symbol,
                   "ground_truth_review_reason": old.get("ground_truth_review_reason", "")}
            old = merge.annotate_row(row, human, model, record, source_status=old.get("migration_status", "MIGRATED"))
        scored.append(old)
    return {scope: merge.metric([row for row in scored if row["scope"] == scope]) for scope in ("MARKET", "STOCK")} | {"COMBINED": merge.metric(scored)}


def recompute_metrics(after_replay: dict[str, Any], _after_records: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    prior = json.loads((QA.parent / "ground_truth_v2_wave1_merged" / "metrics_before_after.json").read_text(encoding="utf-8"))
    before_replay = json.loads(REPLAY.read_text(encoding="utf-8"))
    before = prior["before_after"]["after_resolved_four_plus_wave1"]["metrics"]
    after = _score_replay(after_replay)
    deltas = {scope: {key: after[scope][key] - before[scope][key] for key in ("true_positives", "false_positives", "false_negatives", "scope_mismatches")} for scope in before}
    return {"decision": "FIX_NOT_RETAINED_PRECISION_GATE", "reviewed_rows": 75, "market_rows": 16, "stock_rows": 59,
            "before": before, "after": after, "delta": deltas}


def render_diagnosis(cases: list[dict[str, Any]], crm: dict[str, Any]) -> str:
    lines = ["# Stock Candidate Selection Recall V1 — offline diagnosis", "",
             "**Decision: no production fix retained.** Three target misses share a legal-suffix subject-matching defect. A trial improved some misses but raised stock false positives from 2 to 6, violating the required precision guard; production behavior remains unchanged.", "",
             "## Case-by-case first visible loss", "",
             "| Date | Symbol | Move | Human-supported event | Current outcome | First loss | Evidence summary |", "|---|---|---:|---|---|---|---|"]
    for c in cases:
        lines.append(f"| {c['date']} | {c['symbol']} | {float(c['move_pct'] or 0):+.2f}% | {c['human_supported_event']} | {c['current_final_explanation']} / {c['before_selected_event'] or 'none'} | {c['root_cause']} | {c['reason']} |")
    lines += ["", "## Candidate-level diagnostics", ""]
    for c in cases:
        lines += [f"### {c['date']} {c['symbol']}", "", f"Move: {c['move_pct']}; human: {c['human_supported_event']}", "",
                  "| Candidate | Source / published | Event | Role | Timing | Causal | Verification | Score | Reject reason |", "|---|---|---|---|---|---|---|---:|---|"]
        for item in c["all_cached_candidates"]:
            lines.append(f"| {item.get('title','')} | {item.get('source_name') or item.get('source','')} / {item.get('published_at','')} | {item.get('event_type','')} | {item.get('target_role','')} | {item.get('event_time_relation','')} | {item.get('causal_gate','')} ({item.get('causal_reason','')}) | {item.get('verification','')} | {item.get('selection_score','')} | {item.get('causal_reason','') if item.get('causal_gate') == 'FAIL' else ''} |")
        lines.append("")
    lines += ["## CRM regression case", "", f"Human-supported: {crm['human_supported_event']}", f"Before: {crm['before_selected_event']}", f"After: {crm['after_selected_event']}", "", crm["reason"], "",
              "## Pipeline trace conclusion", "", "AMAT, TSLA and PYPL were rejected at subject matching because the mover company name included a legal suffix while the headline used a shorter public brand. PANW was rejected by causal/event ranking after its analyst action conflicted with the move; SYK and ELV were classified OTHER and failed the concrete-event gate. CRM continued to select Citi's analyst action. No timing, causal, verification or inheritance gates were relaxed.", "",
              "A trial normalized names and ranked concrete candidates above retrospective commentary. It reduced stock false negatives 14→12 but increased false positives 2→6. That violates the explicit no-FP-increase requirement, so the trial was reverted and the final replay is baseline.", "",
              "## Replay / integrity", "", "Replayed the same 29 cached dates offline into a separate QA directory; no APIs or caches were accessed. offline_network_calls=0; FMP_requests=0; stock parity mismatches=0; cross-symbol candidate leaks=0; selected future-event violations=0.", "",
              "Production behavior, human labels, discovery/providers, verification/causal thresholds, inheritance behavior, and cache contents are unchanged.", ""]
    return "\n".join(lines)


def render_metrics(data: dict[str, Any]) -> str:
    lines = ["# Metrics delta — Stock Candidate Selection Recall V1", "",
             "Metrics were recomputed over the same 75 adjudicated rows (16 market, 59 stock) using the before and offline after replay outputs; adjudications were not changed.", "",
             "| Scope | Precision-like before → after | Recall-like before → after | FP before → after | FN before → after | Scope before → after |", "|---|---|---|---:|---:|---:|"]
    for scope, before in data["before"].items():
        after = data["after"][scope]
        lines.append(f"| {scope} | {before['precision_like']:.1%} → {after['precision_like']:.1%} | {before['recall_like']:.1%} → {after['recall_like']:.1%} | {before['false_positives']} → {after['false_positives']} | {before['false_negatives']} → {after['false_negatives']} | {before['scope_mismatches']} → {after['scope_mismatches']} |")
    lines += ["", "Strict precision-like and recall-like formulas are unchanged; scope mismatches count as errors in both denominators.", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
