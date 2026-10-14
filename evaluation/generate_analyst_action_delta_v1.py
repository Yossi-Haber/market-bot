"""Create the analysis-only before/after report for analyst-action recovery."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BASE_RUN = ROOT / "output/evaluation_batch/runs/2026-09-15_to_2026-09-28_integrity_20261003_074305"
LABELS = ROOT / "output/evaluation/manual_qa_v1/stock_review_queue_manual_qa_v1.csv"
REPLAY = ROOT / "output/qa/analyst_action_subject_recovery_v1/replay_20261003/evaluation/market_driver_multiday.json"
OUT = ROOT / "output/qa/analyst_action_subject_recovery_v1"
PROTECTED = {
    ("2026-09-21", "META"), ("2026-09-28", "BA"),
    ("2026-09-21", "QCOM"), ("2026-09-21", "LRCX"),
    ("2026-09-16", "COP"), ("2026-09-16", "EOG"),
    ("2026-09-17", "MU"), ("2026-09-28", "PANW"),
    ("2026-09-24", "META"), ("2026-09-22", "ALL"),
    ("2026-09-23", "PANW"),
}
EXPLAINED = {"STOCK_SPECIFIC", "MARKET_EXPLAINED", "SECTOR_EXPLAINED", "MULTI_FACTOR"}


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _status(row: dict[str, Any] | None) -> str:
    return str((row or {}).get("overall_explanation_status") or "")


def build() -> dict[str, Any]:
    baseline_rows = _csv(BASE_RUN / "stock_review_queue.csv")
    labels = _csv(LABELS)
    after_doc = json.loads(REPLAY.read_text(encoding="utf-8"))
    after_rows = after_doc["stock_records"]
    baseline = {(r["date"], r["symbol"].upper()): r for r in baseline_rows}
    after = {(r["date"], r["symbol"].upper()): r for r in after_rows}
    label_by_key = {(r["date"], r["symbol"].upper()): r for r in labels}
    excluded_fail_dates = {
        day["market_date"] for day in after_doc["days"]
        if day.get("integrity", {}).get("integrity_status") == "FAIL"
    }
    base_summary = json.loads((BASE_RUN / "historical_batch_summary.json").read_text(encoding="utf-8"))
    excluded_fail_dates.update(
        item["market_date"] for item in base_summary.get("days", [])
        if item.get("integrity", {}).get("integrity_status") == "FAIL"
    )
    valid_keys = {
        key for key, row in label_by_key.items()
        if row.get("manual_label_eligible", "").lower() == "true"
        and row.get("manual_label_stale", "").lower() != "true"
        and key[0] not in excluded_fail_dates
        and key in baseline and key in after
    }

    def residuals(rows: dict[tuple[str, str], dict[str, Any]]) -> tuple[int, int, list[str]]:
        fn, fp, cases = 0, 0, []
        for key in sorted(valid_keys):
            gold = label_by_key[key].get("manual_label")
            status = _status(rows.get(key))
            if gold == "FALSE_NEGATIVE" and status not in EXPLAINED:
                fn += 1
                cases.append(f"FN:{key[0]}:{key[1]}")
            elif gold == "FALSE_POSITIVE" and status in EXPLAINED:
                fp += 1
                cases.append(f"FP:{key[0]}:{key[1]}")
        return fn, fp, cases

    fn_before, fp_before, before_errors = residuals(baseline)
    fn_after, fp_after, after_errors = residuals(after)
    if (fn_before, fp_before) != (8, 2) or (fn_after, fp_after) != (6, 2):
        raise AssertionError(f"Unexpected QA delta: before={(fn_before, fp_before)}, after={(fn_after, fp_after)}")

    target_keys = [("2026-09-25", "HUM"), ("2026-09-28", "PANW")]
    target_rows = []
    for key in target_keys:
        old, new = baseline[key], after[key]
        chosen = next(
            (candidate for candidate in new.get("candidate_stories", [])
             if (candidate.get("canonical_event_title") or candidate.get("title")) == new.get("selected_event")),
            {},
        )
        action = chosen.get("analyst_action") or {}
        target_rows.append({
            "date": key[0], "symbol": key[1],
            "before_status": old.get("overall_explanation_status"),
            "after_status": new.get("overall_explanation_status"),
            "selected_event": new.get("selected_event"),
            "analyst_firm": action.get("analyst_firm"),
            "analyst_action": action.get("action_type"),
            "rating_from": action.get("rating_from"),
            "rating_to": action.get("rating_to"),
            "price_target_from": action.get("price_target_from"),
            "price_target_to": action.get("price_target_to"),
            "subject_confidence": action.get("subject_confidence"),
            "causal_gate": new.get("causal_gate"),
            "verification_status": new.get("verification_status"),
            "final_explanation": new.get("overall_explanation_status"),
            "reason": new.get("causal_reason"),
        })
    _write_csv(OUT / "target_case_results.csv", target_rows, list(target_rows[0]))

    new_specific = []
    for key, current in sorted(after.items()):
        old = baseline.get(key, {})
        if current.get("overall_explanation_status") != "STOCK_SPECIFIC" or old.get("overall_explanation_status") == "STOCK_SPECIFIC":
            continue
        label = label_by_key.get(key, {})
        specific_row = {
            "date": key[0], "symbol": key[1], "selected_event": current.get("selected_event"),
            "manual_label": label.get("manual_label", ""),
            "before_status": old.get("overall_explanation_status"),
            "after_status": current.get("overall_explanation_status"),
            "move_pct": current.get("move_pct"),
            "causal_gate": current.get("causal_gate"),
            "verification_status": current.get("verification_status"),
        }
        if key == ("2026-09-25", "HUM"):
            specific_row["why_changed"] = "Cached premarket Barclays upgrade directly matches HUM; parsed rating upgrade and $407-to-$515 target raise pass direction/timing gates."
        elif key == ("2026-09-28", "PANW"):
            specific_row["why_changed"] = "Cached premarket BTIG maintained-Buy action directly matches despite the Inc. suffix variation; $404-to-$425 target increase aligns with the positive move."
        elif key == ("2026-09-18", "NFLX"):
            specific_row["why_changed"] = "Independent valid case: same-day Wells Fargo downgrade and $80-to-$57 target cut align with the 4.67% decline; manual label is UNCLEAR."
        elif key == ("2026-09-24", "SNPS"):
            specific_row["why_changed"] = "Independent, unlabeled case: same-day BNP Paribas upgrade from Underperform to Neutral with a $420 target; positive move aligns. No missing target values were inferred."
        else:
            specific_row["why_changed"] = "Review required: newly selected direct analyst-action evidence."
        new_specific.append(specific_row)
    _write_csv(OUT / "new_stock_specific_results.csv", new_specific, [
        "date", "symbol", "selected_event", "manual_label", "before_status", "after_status",
        "move_pct", "causal_gate", "verification_status", "why_changed",
    ])

    regression_rows = []
    selected_keys = set(PROTECTED)
    selected_keys.update(key for key in valid_keys if label_by_key[key].get("manual_label"))
    selected_keys.update(key for key in baseline if _status(baseline[key]) != _status(after.get(key)))
    for key in sorted(selected_keys):
        old, new = baseline.get(key), after.get(key)
        label = label_by_key.get(key, {})
        old_status, new_status = _status(old), _status(new)
        new_fp = (
            label.get("manual_label") in {"FALSE_POSITIVE", "NO_PUBLIC_CATALYST"}
            and old_status not in EXPLAINED and new_status in EXPLAINED
        )
        regression_rows.append({
            "date": key[0], "symbol": key[1],
            "manual_label": label.get("manual_label", ""),
            "before_status": old_status or ("INTEGRITY_FAIL" if key[0] in excluded_fail_dates else "NOT_PRESENT"),
            "after_status": new_status or ("INTEGRITY_FAIL" if key[0] in excluded_fail_dates else "NOT_PRESENT"),
            "changed": old_status != new_status,
            "new_false_positive": new_fp,
            "selected_event": (new or {}).get("selected_event"),
            "note": (
                "Valid stock-specific analyst event supersedes market/sector inheritance conflict."
                if key == ("2026-09-28", "PANW") else
                "Positive target revision conflicts with META's negative move; the previous stock-specific opinion is now abstained."
                if key == ("2026-09-25", "META") else
                "Protected regression case; status remains stable." if key in PROTECTED else
                "Manual-label comparison."
            ),
        })
    if any(row["new_false_positive"] for row in regression_rows):
        raise AssertionError("New false positive detected against FALSE_POSITIVE/NO_PUBLIC_CATALYST labels")
    _write_csv(OUT / "regression_case_results.csv", regression_rows, list(regression_rows[0]))

    fully_before = sum(_status(row) == "UNEXPLAINED" for row in baseline_rows) / len(baseline_rows)
    fully_after = float(after_doc["metrics"]["all_available_metrics"]["fully_unexplained_rate"])
    positives = sum(
        label_by_key[key].get("manual_label") in {"FALSE_NEGATIVE", "CORRECT_CAUSAL", "PLAUSIBLE_CAUSAL"}
        for key in valid_keys
    )
    tp_before, tp_after = positives - fn_before, positives - fn_after
    precision_before = tp_before / (tp_before + fp_before) if tp_before + fp_before else 0.0
    precision_after = tp_after / (tp_after + fp_after) if tp_after + fp_after else 0.0
    recall_before = tp_before / positives if positives else 0.0
    recall_after = tp_after / positives if positives else 0.0
    offline_network_calls = sum(
        int(day.get("news_context", {}).get("verification", {}).get("offline_network_attempts_blocked", 0) or 0)
        for day in after_doc["days"]
    )
    fmp_requests = int(after_doc.get("metrics", {}).get("fmp_requests_total", 0) or 0)
    if not after_doc.get("offline") or offline_network_calls or fmp_requests:
        raise AssertionError(f"Offline guarantees failed: offline={after_doc.get('offline')}, network={offline_network_calls}, fmp={fmp_requests}")

    summary = {
        "before": {"precision_like": precision_before, "recall_like": recall_before, "false_positives": fp_before, "false_negatives": fn_before, "fully_unexplained_rate": fully_before, "residual_errors": before_errors},
        "after": {"precision_like": precision_after, "recall_like": recall_after, "false_positives": fp_after, "false_negatives": fn_after, "fully_unexplained_rate": fully_after, "residual_errors": after_errors},
        "excluded_integrity_fail_dates": sorted(excluded_fail_dates),
        "stock_result_parity_mismatches": after_doc.get("stock_result_parity_mismatches"),
        "cross_symbol_candidate_leaks": after_doc.get("cross_symbol_candidate_leaks"),
        "offline_network_calls": offline_network_calls,
        "fmp_requests": fmp_requests,
        "new_stock_specific_count": len(new_specific),
        "new_stock_specific": new_specific,
        "production_behavior_modified": True,
        "manual_labels_modified": False,
        "source_run": "output/evaluation_batch/runs/2026-09-15_to_2026-09-28_integrity_20261003_074305",
        "replay_artifacts": "output/qa/analyst_action_subject_recovery_v1/replay_20261003/evaluation",
    }
    (OUT / "analyst_action_delta.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md = [
        "# Cached Analyst-Action Subject Recovery V1 — QA Delta", "",
        "Offline-only ten-date replay against existing dated caches and the unchanged manual labels.", "",
        "## Before / after", "",
        "| Metric | Before | After |", "|---|---:|---:|",
        f"| Stock precision-like | {precision_before:.1%} | {precision_after:.1%} |",
        f"| Stock recall-like | {recall_before:.1%} | {recall_after:.1%} |",
        f"| False positives | {fp_before} | {fp_after} |",
        f"| False negatives | {fn_before} | {fn_after} |",
        f"| Fully unexplained rate | {fully_before:.1%} | {fully_after:.1%} |", "",
        "## Target cases", "",
        "| Date | Symbol | Before | After | Analyst action | Target change | Gate | Verification |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in target_rows:
        change = f"{row['price_target_from']}→{row['price_target_to']}"
        md.append(f"| {row['date']} | {row['symbol']} | {row['before_status']} | {row['after_status']} | {row['analyst_firm']} {row['analyst_action']} | {change} | {row['causal_gate']} | {row['verification_status']} |")
    md += [
        "", "## Trace findings", "",
        "- HUM 2026-09-25: Benzinga, 2026-09-25 11:38:18Z (07:38 ET), “Barclays Upgrades Humana to Overweight, Raises Price Target to $515”; affected symbol HUM; UPGRADE, Equal-Weight→Overweight, $407→$515. It previously scored incidental/0 and did not reach causal or verification selection.",
        "- PANW 2026-09-28: Benzinga, 2026-09-28 06:16:14Z (02:16 ET), “BTIG Maintains Buy on Palo Alto Networks, Raises Price Target to $425”; affected symbol PANW; REITERATION_WITH_TARGET_CHANGE, $404→$425. The legal “Inc.” suffix mismatch previously made the headline subject incidental.",
        "",
        "## Exact recovery rules", "",
        "Recovery requires a direct target company/ticker in the headline, a concrete analyst-action grammar attached to that headline entity, and exact provider affected-symbol association. Direct/strong confidence is assigned only then. Body-only mentions, listicles, roundups, peer mentions, and ambiguous subjects abstain. Legal suffixes are normalized only for phrase matching.",
        "",
        "The parser extracts analyst firm, action type, rating from/to, price-target from/to, and target symbol only when present. Promotion additionally requires point-in-time eligibility and action direction compatible with the mover direction; conflict abstains. Existing verification standards and causal timing thresholds are unchanged.",
        "",
        "## New STOCK_SPECIFIC outcomes", "",
        "See new_stock_specific_results.csv. HUM and PANW are the intended recoveries. NFLX is independently directionally supported by a same-day Wells Fargo downgrade and $80→$57 target cut against a decline; its unchanged manual label is UNCLEAR. SNPS is an unlabeled same-day BNP Paribas upgrade from Underperform to Neutral with a $420 target and a positive move; keep it for manual review. No new false positive was created in labeled FALSE_POSITIVE or NO_PUBLIC_CATALYST rows.",
        "",
        "META 2026-09-25 changed from STOCK_SPECIFIC to UNEXPLAINED because a positive Deutsche Bank target revision conflicted with META’s negative move. This is the direction-conflict guard, not a new false negative in the unchanged label set.",
        "",
        "## Integrity and artifacts", "",
        f"- parity mismatches: {summary['stock_result_parity_mismatches']}; cross-symbol leaks: {summary['cross_symbol_candidate_leaks']}; offline network calls: {offline_network_calls}; FMP requests: {fmp_requests}.",
        f"- Integrity-FAIL date excluded from QA: {', '.join(summary['excluded_integrity_fail_dates']) or 'none'}; labels were not edited.",
        "- Replay output is in replay_20261003/evaluation and does not overwrite the baseline package.", "",
    ]
    (OUT / "analyst_action_delta.md").write_text("\n".join(md), encoding="utf-8")
    return summary


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
