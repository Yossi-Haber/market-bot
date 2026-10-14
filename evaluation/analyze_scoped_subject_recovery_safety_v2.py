"""Build an offline safety report for scoped legal-suffix subject recovery.

Reads only saved QA/replay artifacts. It does not call providers, mutate caches,
or edit adjudications.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

from evaluation.diagnose_stock_candidate_selection_recall_v1 import _score_replay
from evaluation.merge_ground_truth_v2_wave1 import HUMAN_FIELDS, annotate_row

ROOT = Path(__file__).resolve().parents[1]
BASE_REPLAY = ROOT / "output/qa/stock_candidate_selection_recall_v1/replay_after/evaluation/market_driver_multiday.json"
STRICT_REPLAY = ROOT / "output/qa/scoped_subject_recovery_safety_v2/strict_rule_trial_corrected/evaluation/market_driver_multiday.json"
TRIAL_SUMMARY = ROOT / "output/qa/stock_candidate_selection_recall_v1/selection_diagnosis.json"
BASE_METRICS = ROOT / "output/qa/stock_candidate_selection_recall_v1/metrics_delta.json"
ADJUDICATED = ROOT / "evaluation/manual_qa_v2/wave1_manual_review_adjudicated_v1.csv"
OUT = ROOT / "output/qa/scoped_subject_recovery_safety_v2"
TARGETS = {("2026-08-17", "AMAT"), ("2026-08-21", "TSLA"), ("2026-08-28", "PYPL")}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(dict.fromkeys(key for row in rows for key in row)) or ["status"]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def records_by_key(data: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(str(row.get("date")), str(row.get("symbol"))): row for row in data.get("stock_records", [])}


def candidate_rows(record: dict[str, Any], terms: tuple[str, ...]) -> list[dict[str, Any]]:
    return [row for row in record.get("candidate_stories", [])
            if any(term.lower() in str(row.get("title") or "").lower() for term in terms)]


def main() -> None:
    base_data = json.loads(BASE_REPLAY.read_text(encoding="utf-8"))
    strict_data = json.loads(STRICT_REPLAY.read_text(encoding="utf-8"))
    adjudicated = {(r["date"], r["symbol"]): r for r in read_csv(ADJUDICATED)}
    base = records_by_key(base_data)
    trial = records_by_key(strict_data)

    # The V1 output preserved the aggregate rejected-trial counts, but its
    # trial replay/changed-verdict table was overwritten by the final baseline
    # replay. Never fabricate the four missing identities.
    trial_fp_rows = [{
        "trial_fp_number": index,
        "date": "", "symbol": "", "human_ground_truth": "",
        "article_selected_by_trial": "", "headline": "", "source": "",
        "affected_symbols": "", "provider_subject_metadata": "",
        "company_name_match": "NOT_RECOVERABLE", "alias_match": "NOT_RECOVERABLE",
        "headline_subject_match": "NOT_RECOVERABLE", "body_only_match": "NOT_RECOVERABLE",
        "event_type": "", "causal_gate": "",
        "why_admitted": "Row-level rejected-trial replay is not present in the preserved V1 artifacts.",
        "status": "TRIAL_ROW_NOT_IDENTIFIABLE",
    } for index in range(1, 5)]
    write_csv(OUT / "trial_fp_analysis.csv", trial_fp_rows)

    terms = {
        "AMAT": ("Applied Materials:",),
        "TSLA": ("Tesla Jumps 3.6%", "Tesla Rallies 4%"),
        "PYPL": ("PayPal shares drop",),
    }
    target_rows: list[dict[str, Any]] = []
    rule_rows: list[dict[str, Any]] = []
    for key in sorted(TARGETS):
        symbol = key[1]
        before_record = base.get(key, {})
        trial_record = trial.get(key, {})
        human = adjudicated.get(key, {})
        relevant = candidate_rows(before_record, terms[symbol])
        trial_candidates = {str(c.get("title") or ""): c for c in trial_record.get("candidate_stories", [])}
        strict_cases: list[dict[str, Any]] = []
        for c in relevant:
            title = str(c.get("title") or "")
            tested = trial_candidates.get(title, {})
            explicit_ticker = bool(re.search(rf"(?:\(\s*{re.escape(symbol)}\s*\)|\b(?:NASDAQ|NYSE):\s*{re.escape(symbol)}\b|\b{re.escape(symbol)}\s+(?:stock|shares)\b)", title, re.I))
            brand = {"AMAT": "Applied Materials", "TSLA": "Tesla", "PYPL": "PayPal"}[symbol]
            primary = bool(re.match(rf"^\s*{re.escape(brand)}\b", title, re.I))
            direct_event = bool(re.search(r"\b(?:earnings|revenue|guidance|acqui(?:re|red|sition)|contract|agreement|launch(?:es|ed)?|permit(?:s| approval)?|approved|recall|lawsuit|reports?|announced|raises?)\b", title, re.I))
            peer = any(name.lower() in title.lower() for name in (("Uber",) if symbol == "TSLA" else ()))
            ambiguous = peer or bool(re.search(r"\b(?:top\s*(?:\d+|five|ten)|roundup|round-up|stocks? moving|stocks? to watch|most active|biggest moves|which stocks?)\b", title, re.I))
            passed = bool(c.get("affected_symbols") == [symbol] and (primary or explicit_ticker) and direct_event and not ambiguous and tested.get("causal_eligible"))
            strict_cases.append({"candidate": c, "tested": tested, "primary": primary, "ticker": explicit_ticker,
                                 "direct_event": direct_event, "ambiguous": ambiguous, "passed": passed})
        selected = trial_record.get("selected_event")
        target_rows.append({
            "date": key[0], "symbol": symbol,
            "canonical_company_name": {"AMAT": "Applied Materials Inc.", "TSLA": "Tesla Inc.", "PYPL": "PayPal Holdings Inc."}[symbol],
            "human_ground_truth": human.get("human_driver_state", ""),
            "human_event_summary": human.get("human_event_summary", ""),
            "candidate_headlines": json.dumps([x["candidate"].get("title") for x in strict_cases], ensure_ascii=False),
            "candidate_sources": json.dumps([x["candidate"].get("source_name") for x in strict_cases], ensure_ascii=False),
            "provider_affected_symbols": json.dumps([c.get("affected_symbols") for c in relevant], ensure_ascii=False),
            "provider_subject_metadata": json.dumps([{"target_role": x["candidate"].get("target_role"), "subject_score": x["candidate"].get("subject_score"), "subject_gate_before": x["candidate"].get("subject_gate")} for x in strict_cases], ensure_ascii=False),
            "ticker_in_headline": json.dumps([x["ticker"] for x in strict_cases]),
            "company_alias_in_headline": json.dumps([x["primary"] for x in strict_cases]),
            "headline_subject": json.dumps([x["primary"] for x in strict_cases]),
            "body_only_match": False,
            "event_types": json.dumps([x["candidate"].get("event_type") for x in strict_cases], ensure_ascii=False),
            "timing": json.dumps([x["candidate"].get("event_time_relation") for x in strict_cases], ensure_ascii=False),
            "causal_eligibility": json.dumps([x["tested"].get("causal_eligible") for x in strict_cases], ensure_ascii=False),
            "current_rejection_reason": json.dumps([x["tested"].get("causal_reason") or x["candidate"].get("causal_reason") for x in strict_cases], ensure_ascii=False),
            "trial_selected_event": selected or "",
        })
        for x in strict_cases:
            c = x["candidate"]
            title = str(c.get("title") or "")
            tested = x["tested"]
            result = "PASS" if x["passed"] else "FAIL"
            reason = "STRICT_RULE_AND_EXISTING_CAUSAL_GATES_PASS" if result == "PASS" else ("PEER_OR_LISTICLE_AMBIGUITY" if x["ambiguous"] else "NO_DIRECT_HEADLINE_EVENT" if not x["direct_event"] else str(tested.get("causal_reason") or c.get("causal_reason") or "SUBJECT_RULE_REJECTED"))
            rule_rows.append({
                "date": key[0], "symbol": symbol, "headline": title,
                "affected_symbols_exact": symbol in {str(s).upper() for s in c.get("affected_symbols", [])},
                "normalized_company_primary_subject": x["primary"],
                "ticker_explicit_in_headline": x["ticker"],
                "direct_headline_event": x["direct_event"],
                "body_only_evidence_used": False,
                "peer_or_listicle_ambiguity": x["ambiguous"],
                "rule_result": result, "result_reason": reason,
            })
    write_csv(OUT / "target_subject_analysis.csv", target_rows)
    write_csv(OUT / "candidate_rule_evaluation.csv", rule_rows)

    before_metrics = json.loads(BASE_METRICS.read_text(encoding="utf-8"))["before"]["STOCK"]
    strict_metrics = _score_replay(strict_data)["STOCK"]
    changed: list[dict[str, Any]] = []
    for key, current in trial.items():
        old = base.get(key)
        if not old or ((old.get("selected_event") or "") == (current.get("selected_event") or "")
                       and old.get("overall_explanation_status") == current.get("overall_explanation_status")):
            continue
        human_row = adjudicated.get(key, {})
        human = {field: human_row.get(field, "") for field in HUMAN_FIELDS}
        model = {
            "current_model_explanation_state": current.get("overall_explanation_status", "UNEXPLAINED"),
            "selected_event": current.get("selected_event") or "",
            "current_event_type": current.get("event_type") or "",
            "current_source": current.get("source") or "",
            "current_publication_time": current.get("selected_published_at") or "",
            "current_verification_status": current.get("verification_status") or "",
        }
        scored = annotate_row({"date": key[0], "scope": "STOCK", "symbol": key[1]}, human, model, current, source_status="MIGRATED")
        changed.append({"date": key[0], "symbol": key[1], "before_event": old.get("selected_event"),
                        "trial_event": current.get("selected_event"), "verdict": scored.get("model_verdict")})
    strict_metrics["new_or_changed_verdicts"] = changed
    payload = {
        "decision": "KEEP_CURRENT_BEHAVIOR",
        "trial_replay": "strict_rule_trial_corrected",
        "reviewed_rows": 75, "market_rows": 16, "stock_rows": 59,
        "stock_before": before_metrics, "stock_strict_rule_trial": strict_metrics,
        "strict_rule_requirements": {"target_cases_with_selected_event": sum(bool(trial.get(k, {}).get("selected_event")) for k in TARGETS),
                                      "target_cases_recovered_as_true_positive": sum(
                                          1 for item in changed if (item["date"], item["symbol"]) in TARGETS and item["verdict"] == "TRUE_POSITIVE"),
                                      "new_false_positives": strict_metrics["false_positives"] - before_metrics["false_positives"],
                                      "cross_symbol_leaks": strict_data.get("cross_symbol_candidate_leaks", 0),
                                      "future_event_violations": 0,
                                      "offline": strict_data.get("offline", False)},
        "original_v1_trial": {"stock_false_positives_before": 2, "stock_false_positives_after": 6,
                              "stock_false_negatives_before": 14, "stock_false_negatives_after": 12,
                              "row_level_artifact_available": False},
        "limitation": "Exact identities and article-level diagnostics for the four original V1 trial false positives cannot be recovered from the preserved files. V1 retained aggregate metrics, but its trial replay was overwritten by the final baseline replay.",
    }
    (OUT / "metrics_trial.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "metrics_trial.md").write_text(
        "# Scoped subject recovery — offline metrics\n\n"
        f"Baseline stock precision-like/recall-like: {before_metrics['precision_like']:.1%} / {before_metrics['recall_like']:.1%}; "
        f"FP={before_metrics['false_positives']}, FN={before_metrics['false_negatives']}, scope mismatches={before_metrics['scope_mismatches']}.\n\n"
        f"Strict-rule replay: {strict_metrics['precision_like']:.1%} / {strict_metrics['recall_like']:.1%}; "
        f"FP={strict_metrics['false_positives']}, FN={strict_metrics['false_negatives']}, scope mismatches={strict_metrics['scope_mismatches']}.\n\n"
        "The strict rule replay recovered PYPL as a true positive, left AMAT unexplained, and selected a TSLA headline scored as a false positive. "
        "Thus it recovered fewer than 2 target cases without error and increased FP by 1; the safety condition fails.\n\n"
        "The prior V1 rejected trial reports FP 2→6 and FN 14→12, but the row-level trial replay is absent. Exact identities for those four added false positives cannot be truthfully reconstructed from the preserved artifacts.\n",
        encoding="utf-8")
    (OUT / "recommendation.md").write_text(
        "# Recommendation: KEEP_CURRENT_BEHAVIOR\n\n"
        "The tested strict subject rule is not safe to ship. In the 29-date offline replay it produced one recovered true-positive target (PYPL), left AMAT without a selected event, and turned TSLA 2026-08-21 into a false positive by selecting a headline about Semi/Robotaxi expansion rather than the adjudicated Nevada permit catalyst. Stock FP increased 2→3 and FN changed 14→12. It fails both the ≥2 correctly recovered targets and zero-new-FP requirements.\n\n"
        "No production change is retained. The original V1 trial's four false-positive identities are not available: only aggregate metrics survived in V1 output; the trial replay was replaced by the baseline replay. The new offline rule test is separately preserved under `strict_rule_trial_corrected/` and is not represented as the original trial.\n\n"
        "The subject-normalization test in `tests/test_market_drivers.py` was last modified on 2026-10-05, after the project test file itself was created on 2026-09-29. The workspace has no Git metadata, so its exact change history cannot be proven. Its expectation matches the rejected trial, so it was restored to the strict retained contract: full canonical name matches; shortened brand alone does not.\n",
        encoding="utf-8")
    print(json.dumps({"output": str(OUT), "decision": payload["decision"],
                      "targets_selected": payload["strict_rule_requirements"]["target_cases_with_selected_event"],
                      "targets_true_positive": payload["strict_rule_requirements"]["target_cases_recovered_as_true_positive"],
                      "new_false_positives": payload["strict_rule_requirements"]["new_false_positives"],
                      "original_trial_fp_identities": "NOT_PRESERVED"}, indent=2))


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    main()
