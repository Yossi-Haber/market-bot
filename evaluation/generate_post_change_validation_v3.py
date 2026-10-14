"""Generate offline-only manual validation artifacts for the analyst-action replay."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/qa/post_change_validation_v3"
REPLAY = ROOT / "output/qa/analyst_action_subject_recovery_v1/replay_20261003/evaluation/market_driver_multiday.json"
DELTA = ROOT / "output/qa/analyst_action_subject_recovery_v1/analyst_action_delta.json"
LABELS = ROOT / "output/evaluation/manual_qa_v1/stock_review_queue_manual_qa_v1.csv"
RESIDUAL = ROOT / "output/qa/residual_qa_v2/remaining_false_negatives.csv"
NY = ZoneInfo("America/New_York")


NEW_OUTCOMES = [
    {
        "date": "2026-09-25", "symbol": "HUM", "analyst_firm": "Barclays",
        "action_type": "UPGRADE", "rating_change": "Equal-Weight → Overweight",
        "price_target_change": "$407 → $515", "subject_confidence": "DIRECT_TARGET",
        "direction_compatibility": "PASS: upgrade/target raise aligns with +4.630306%",
        "manual_label": "FALSE_NEGATIVE", "classification": "LIKELY_VALID_BUT_UNVERIFIED",
        "defensible": True,
        "assessment": "Premarket Benzinga report directly names Humana/HUM, states a fresh Barclays upgrade and target increase. Causal gate passes; verifier status remains PLAUSIBLE_NOT_VERIFIED.",
    },
    {
        "date": "2026-09-28", "symbol": "PANW", "analyst_firm": "BTIG",
        "action_type": "REITERATION_WITH_TARGET_CHANGE", "rating_change": "Buy maintained",
        "price_target_change": "$404 → $425", "subject_confidence": "DIRECT_TARGET",
        "direction_compatibility": "PASS: target raise aligns with +4.629878%",
        "manual_label": "FALSE_NEGATIVE", "classification": "LIKELY_VALID_BUT_UNVERIFIED",
        "defensible": True,
        "assessment": "Premarket Benzinga report directly names Palo Alto Networks/PANW and records a Buy reiteration plus target increase. Causal gate passes; verifier status remains PLAUSIBLE_NOT_VERIFIED.",
    },
    {
        "date": "2026-09-18", "symbol": "NFLX", "analyst_firm": "Wells Fargo",
        "action_type": "DOWNGRADE", "rating_change": "Equal-Weight → Underweight",
        "price_target_change": "$80 → $57", "subject_confidence": "DIRECT_TARGET",
        "direction_compatibility": "PASS: downgrade/target cut aligns with -4.674014%",
        "manual_label": "UNCLEAR", "classification": "LIKELY_VALID_BUT_UNVERIFIED",
        "defensible": True,
        "assessment": "The cached Benzinga item is an explicit action headline and summary, identifies Netflix/NFLX as the target, and reports a downgrade and target cut. Published 03:30 ET before the regular session. No evidence in the cached item suggests it is merely a recap; however the manual UNCLEAR label warrants review. PLAUSIBLE_NOT_VERIFIED is not independent confirmation.",
        "gold_label_review_recommended": True,
    },
    {
        "date": "2026-09-24", "symbol": "SNPS", "analyst_firm": "BNP Paribas",
        "action_type": "UPGRADE", "rating_change": "Underperform → Neutral",
        "price_target_change": "No prior target stated → $420 target announced",
        "subject_confidence": "DIRECT_TARGET",
        "direction_compatibility": "PASS: upgrade to Neutral aligns with +2.868833%",
        "manual_label": "UNLABELED", "classification": "LIKELY_VALID_BUT_UNVERIFIED",
        "defensible": True,
        "assessment": "Same-day Benzinga item directly names Synopsys/SNPS and describes a move from Underperform to Neutral with a $420 target. Published 10:28 ET during the session. It is present in the original review queue but below the high-move subset represented by the existing FALSE_NEGATIVE/UNCLEAR labels; it is material enough for a manual QA decision. PLAUSIBLE_NOT_VERIFIED.",
        "manual_label_recommended": True,
    },
]


FN_ASSESSMENTS = {
    ("2026-09-17", "AMD"): {
        "cached_evidence": "Present: broad tech/semiconductor rally material and a plausible technology-sector movement driver; dated market-discovery cache itself has zero candidates.",
        "usable_evidence": "No sufficiently explicit sector-scoped causal item establishing that a broad technology/AI theme explains AMD specifically. Replay rejects inheritance as not sector-scoped.",
        "pipeline_failure_stage": "sector inheritance / scope validation",
        "root_cause": "SECTOR_INHERITANCE_MISS; secondary SECTOR_ASSOCIATION_MISS",
        "cache_classification": "UNCERTAIN",
        "recommended_action": "Do not broaden semiconductor evidence to all technology names. Keep the label unchanged; require explicit sector/theme evidence or revisit the gold rationale.",
    },
    ("2026-09-17", "ORCL"): {
        "cached_evidence": "Present: QQQ +1.731186%, technology-sector movement +2.245424%, and Oracle articles; market-discovery cache has zero candidates.",
        "usable_evidence": "The Oracle-specific cached headline is incidental/OTHER and fails concrete-event gating. Market/sector movement supports the manual broad-risk-on rationale but does not establish an Oracle catalyst or fully account for its +5.189997% move.",
        "pipeline_failure_stage": "event classification / gold-label reconciliation",
        "root_cause": "OTHER; GOLD_LABEL_REVIEW_REQUIRED",
        "cache_classification": "GOLD_LABEL_REVIEW_REQUIRED",
        "recommended_action": "Keep the manual label unchanged pending review. Do not promote the incidental Oracle article or semiconductor-only coverage. Existing evidence does not justify removing ORCL from the FN set automatically.",
    },
    ("2026-09-18", "LRCX"): {
        "cached_evidence": "Inspected dated LRCX Finnhub and market-discovery caches; no matching JPMorgan WFE/AI-capex research found.",
        "usable_evidence": "No point-in-time evidence for the cited semiconductor-equipment research theme. Generic movers recaps are not causal evidence.",
        "pipeline_failure_stage": "news/theme discovery",
        "root_cause": "SECTOR_THEME_DISCOVERY_MISS",
        "cache_classification": "NOT_FIXABLE_WITH_EXISTING_CACHE",
        "recommended_action": "Do not implement a code fix from absent evidence. A future, separately authorized dataset/provider expansion would be needed.",
    },
    ("2026-09-18", "AMAT"): {
        "cached_evidence": "Inspected dated AMAT Finnhub and market-discovery caches; no matching JPMorgan WFE/AI-capex research found.",
        "usable_evidence": "No point-in-time evidence for the cited semiconductor-equipment research theme.",
        "pipeline_failure_stage": "news/theme discovery",
        "root_cause": "SECTOR_THEME_DISCOVERY_MISS",
        "cache_classification": "NOT_FIXABLE_WITH_EXISTING_CACHE",
        "recommended_action": "Do not implement a code fix from absent evidence. A future, separately authorized dataset/provider expansion would be needed.",
    },
    ("2026-09-18", "KLAC"): {
        "cached_evidence": "Inspected dated KLAC Finnhub and market-discovery caches; no matching JPMorgan WFE/AI-capex research found.",
        "usable_evidence": "No point-in-time evidence for the cited research or KLA preferred-name mention.",
        "pipeline_failure_stage": "news/theme discovery",
        "root_cause": "SECTOR_THEME_DISCOVERY_MISS",
        "cache_classification": "NOT_FIXABLE_WITH_EXISTING_CACHE",
        "recommended_action": "Do not implement a code fix from absent evidence. A future, separately authorized dataset/provider expansion would be needed.",
    },
    ("2026-09-23", "PANW"): {
        "cached_evidence": "Present and point-in-time eligible: Yahoo item published 14:57 ET, ‘Why Palo Alto Networks (PANW) Stock Is Trading Up Today’, with a same-day summary explicitly stating the Unit 42 Continuous Frontier AI Defense launch. Another same-day cached story names the productized AI pen-testing subscription. The dedicated next-day launch item at 19:11 ET is after the 2026-09-23 close and must not be used.",
        "usable_evidence": "The same-day in-session article directly names Palo Alto/PANW and explicitly attributes the move to the Unit 42 launch. It is cached but absent from the selected stock-catalyst candidates; the surfaced candidates are OTHER, INCIDENTAL_MENTION/score 0 and fail the concrete-event gate.",
        "pipeline_failure_stage": "candidate inclusion / event classification / subject selection",
        "root_cause": "EVENT_CLASSIFICATION_MISS; secondary STOCK_SELECTION_MISS",
        "cache_classification": "FIXABLE_WITH_EXISTING_CACHE",
        "recommended_action": "If ever addressed, narrow it to preserving a dated, direct-subject cache item whose summary explicitly states a product launch and contemporaneous move; classify that exact evidence as PRODUCT/LAUNCH and rerun existing subject/causal gates. Do not use the next-day dedicated article or broaden generic AI-security wording.",
    },
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_article(symbol: str, date: str, title: str) -> dict[str, object]:
    cache = ROOT / "cache/finnhub" / symbol / f"{date}_{(datetime.fromisoformat(date).date()).fromordinal(datetime.fromisoformat(date).date().toordinal() + 1).isoformat()}.json"
    payload = json.loads(cache.read_text(encoding="utf-8"))
    article = next(
        item for item in payload.get("normalized_candidates", [])
        if item.get("title") == title
    )
    published = datetime.fromisoformat(str(article["published_at"]))
    return {
        "title": title,
        "published_at": published.isoformat(),
        "published_at_new_york": published.astimezone(NY).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "source": article.get("source_name"),
        "summary": article.get("summary"),
        "affected_symbols": article.get("affected_symbols", []),
    }


def main() -> None:
    replay = json.loads(REPLAY.read_text(encoding="utf-8"))
    delta = json.loads(DELTA.read_text(encoding="utf-8"))
    labels = {(r["date"], r["symbol"]): r for r in read_csv(LABELS)}
    residual = {(r["date"], r["symbol"]): r for r in read_csv(RESIDUAL)}
    explanation = {
        (day["market_date"], row["symbol"]): row
        for day in replay["days"] for row in day["stock_explanations"]
    }

    expected_fns = {
        ("2026-09-17", "AMD"), ("2026-09-17", "ORCL"),
        ("2026-09-18", "AMAT"), ("2026-09-18", "KLAC"),
        ("2026-09-18", "LRCX"), ("2026-09-23", "PANW"),
    }
    replay_fns = {
        tuple(item[3:].split(":", 2)[1:])
        for item in delta["after"]["residual_errors"] if item.startswith("FN:")
    }
    # Residual token form is FN:date:symbol; avoid silently accepting any drift.
    replay_fns = {
        (item.split(":")[1], item.split(":")[2])
        for item in delta["after"]["residual_errors"] if item.startswith("FN:")
    }
    if replay_fns != expected_fns:
        raise RuntimeError(f"Unexpected post-change FN set: {sorted(replay_fns)}")

    for item in NEW_OUTCOMES:
        row = explanation[(item["date"], item["symbol"])]
        item.update({
            "move_pct": row["move_pct"], "selected_event": row["selected_event"],
            "publication_time_utc": row["selected_published_at"],
            "publication_time_new_york": datetime.fromisoformat(row["selected_published_at"]).astimezone(NY).strftime("%Y-%m-%d %H:%M:%S %Z"),
            "causal_gate": row["causal_gate"], "verification_status": row["verification_status"],
        })
    article_specs = {
        ("2026-09-25", "HUM"): "Barclays Upgrades Humana to Overweight, Raises Price Target to $515",
        ("2026-09-28", "PANW"): "BTIG Maintains Buy on Palo Alto Networks, Raises Price Target to $425",
        ("2026-09-18", "NFLX"): "Wells Fargo Downgrades Netflix to Underweight, Lowers Price Target to $57",
        ("2026-09-24", "SNPS"): "BNP Paribas Upgrades Synopsys to Neutral, Announces $420 Price Target",
    }
    for item in NEW_OUTCOMES:
        item["cached_article"] = read_article(item["symbol"], item["date"], article_specs[(item["date"], item["symbol"])])

    fn_rows: list[dict[str, object]] = []
    for date, symbol in sorted(expected_fns):
        row = explanation[(date, symbol)]
        label = labels.get((date, symbol), {})
        old = residual.get((date, symbol), {})
        assessment = FN_ASSESSMENTS[(date, symbol)]
        fn_rows.append({
            "date": date, "symbol": symbol, "move_pct": row["move_pct"],
            "current_explanation": row["overall_explanation_status"],
            "selected_event": row.get("selected_event"), "manual_label": label.get("manual_label", ""),
            "manual_label_rationale": label.get("review_notes", "") or old.get("manual_qa_rationale", ""),
            "cached_evidence_available": assessment["cached_evidence"],
            "usable_evidence_assessment": assessment["usable_evidence"],
            "pipeline_failure_stage": assessment["pipeline_failure_stage"],
            "root_cause": assessment["root_cause"],
            "cache_classification": assessment["cache_classification"],
            "fixable_without_new_provider": assessment["cache_classification"] == "FIXABLE_WITH_EXISTING_CACHE",
            "recommended_action": assessment["recommended_action"],
        })

    tp, fp, fn = 18, 2, 6
    nflx_valid = {"true_positive": tp + 1, "false_positives": fp, "false_negatives": fn,
                  "precision_like": (tp + 1) / (tp + 1 + fp), "recall_like": (tp + 1) / (tp + 1 + fn)}
    nflx_false_positive = {"true_positive": tp, "false_positives": fp + 1, "false_negatives": fn,
                           "precision_like": tp / (tp + fp + 1), "recall_like": tp / (tp + fn)}
    counts: dict[str, int] = {}
    for row in fn_rows:
        kind = str(row["cache_classification"])
        counts[kind] = counts.get(kind, 0) + 1

    summary = {
        "title": "Post-Change Manual Validation V3",
        "mode": "analysis_only_offline",
        "sources": {"replay": str(REPLAY.relative_to(ROOT)), "unchanged_manual_labels": str(LABELS.relative_to(ROOT)), "baseline_delta": str(DELTA.relative_to(ROOT))},
        "scope_dates": [day["market_date"] for day in replay["days"]],
        "excluded_integrity_fail_dates": delta["excluded_integrity_fail_dates"],
        "integrity": {"stock_result_parity_mismatches": delta["stock_result_parity_mismatches"], "cross_symbol_candidate_leaks": delta["cross_symbol_candidate_leaks"], "offline_network_calls": delta["offline_network_calls"], "fmp_requests": delta["fmp_requests"]},
        "current_metrics": {"precision_like": delta["after"]["precision_like"], "recall_like": delta["after"]["recall_like"], "false_positives": delta["after"]["false_positives"], "false_negatives": delta["after"]["false_negatives"], "fully_unexplained_rate": delta["after"]["fully_unexplained_rate"]},
        "remaining_false_negative_count_by_cache_classification": counts,
        "fixable_with_existing_cache_count": counts.get("FIXABLE_WITH_EXISTING_CACHE", 0),
        "new_stock_specific_outcomes": NEW_OUTCOMES,
        "remaining_false_negatives": fn_rows,
        "nflx_gold_label_review_recommended": True,
        "orcl_label_review_recommended": True,
        "orcl_label_removal_supported_by_current_evidence": False,
        "snps_manual_label_recommended": True,
        "sensitivity": {"NFLX_counted_as_valid": nflx_valid, "NFLX_counted_as_false_positive": nflx_false_positive,
                        "current_labeled_metrics_exclude_unclear_NFLX_and_unlabeled_SNPS": True},
        "next_engineering_decision": "STOP_AND_EXPAND_EVALUATION_DATASET",
        "decision_reason": "Only one residual appears narrowly recoverable from existing cache, while three require absent point-in-time evidence and two need cautious gold/scope review. The residuals span distinct mechanisms, and further tuning against this 10-date sample risks overfitting. Preserve the current production behavior and expand an independently labeled evaluation set before another fix.",
        "production_behavior_changed_by_this_task": False,
        "manual_labels_changed_by_this_task": False,
        "cache_files_changed_by_this_task": False,
        "network_calls": 0,
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "validation_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "validation_summary.md").write_text(render_summary(summary), encoding="utf-8")
    write_csv(OUT / "new_stock_specific_review.csv", NEW_OUTCOMES, [
        "date", "symbol", "move_pct", "selected_event", "publication_time_utc", "publication_time_new_york", "analyst_firm", "action_type", "rating_change", "price_target_change", "subject_confidence", "causal_gate", "verification_status", "direction_compatibility", "manual_label", "classification", "defensible", "gold_label_review_recommended", "manual_label_recommended", "assessment",
    ])
    write_csv(OUT / "remaining_false_negatives.csv", fn_rows, list(fn_rows[0]))
    gold = [
        {"date": "2026-09-18", "symbol": "NFLX", "current_label": "UNCLEAR", "reason": "A direct, premarket Wells Fargo downgrade and $80-to-$57 target cut aligns with the decline; replay is STOCK_SPECIFIC / PLAUSIBLE_NOT_VERIFIED.", "recommended_review": "Reassess UNCLEAR against this cached same-day primary-action evidence; do not auto-relabel."},
        {"date": "2026-09-17", "symbol": "ORCL", "current_label": "FALSE_NEGATIVE", "reason": "Manual broad-tech rationale is directionally consistent with QQQ +1.73% and technology-sector +2.25%, but no usable Oracle-specific cached catalyst explains +5.19%.", "recommended_review": "Review whether market/sector participation qualifies under the gold-label definition; do not remove from FN set automatically."},
        {"date": "2026-09-17", "symbol": "AMD", "current_label": "FALSE_NEGATIVE", "reason": "Manual broad tech/AI rationale has market/sector movement support, but cached evidence is not explicitly scoped enough for AMD inheritance.", "recommended_review": "Review the evidence standard and gold rationale; do not widen semiconductor evidence to all technology stocks."},
    ]
    write_csv(OUT / "gold_label_review_candidates.csv", gold, list(gold[0]))
    (OUT / "next_step_recommendation.md").write_text(
        "# Next-step recommendation\n\n"
        "## STOP_AND_EXPAND_EVALUATION_DATASET\n\n"
        "Do not implement another narrow production fix against this sample. Of the six remaining false negatives, one (PANW, 2026-09-23) has a plausible narrow recovery path from already-cached, point-in-time evidence; three (LRCX, AMAT, KLAC) lack the cited WFE research in inspected caches; AMD has evidence but insufficient scope; and ORCL needs gold-label review. The errors span discovery, event classification, subject/scope association, and label interpretation rather than a single broad defect.\n\n"
        "The sample covers ten dates and has only six residual FNs; optimizing further against these cases risks overfitting. Preserve current labels and production behavior, review the flagged gold cases without automatic edits, then evaluate on a broader independently labeled date range before choosing another implementation change.\n",
        encoding="utf-8",
    )


def render_summary(data: dict[str, object]) -> str:
    metrics = data["current_metrics"]
    lines = [
        "# Post-Change Manual Validation V3", "",
        "Offline analysis of the Cached Analyst-Action Subject Recovery V1 replay. No live APIs were called; this task changed no production behavior, caches, or labels.", "",
        "## Replay integrity and current baseline", "",
        f"- Dates: {', '.join(data['scope_dates'])}",
        f"- Integrity-fail date excluded: {', '.join(data['excluded_integrity_fail_dates'])}",
        f"- Parity mismatches: {data['integrity']['stock_result_parity_mismatches']}; cross-symbol leaks: {data['integrity']['cross_symbol_candidate_leaks']}; offline network calls: {data['integrity']['offline_network_calls']}; FMP requests: {data['integrity']['fmp_requests']}.",
        f"- Current labeled metrics: precision-like {metrics['precision_like']:.1%}, recall-like {metrics['recall_like']:.1%}; FP={metrics['false_positives']}, FN={metrics['false_negatives']}; fully unexplained={metrics['fully_unexplained_rate']:.1%}.", "",
        "## New STOCK_SPECIFIC outcomes", "",
        "| Date | Symbol | Move | Event | Time (New York) | Analyst / action | Direction | Label | Assessment |", "|---|---|---:|---|---|---|---|---|---|",
    ]
    for item in NEW_OUTCOMES:
        lines.append(f"| {item['date']} | {item['symbol']} | {item['move_pct']:+.2f}% | {item['selected_event']} | {item['publication_time_new_york']} | {item['analyst_firm']} — {item['action_type']}; {item['rating_change']}; target {item['price_target_change']} | {item['direction_compatibility']} | {item['manual_label']} | {item['classification']} |")
    lines += ["", "- **HUM:** defensible as a new cached premarket analyst-action explanation, but still PLAUSIBLE_NOT_VERIFIED.",
              "- **PANW (Sep 28):** defensible premarket target-raise explanation, still PLAUSIBLE_NOT_VERIFIED.",
              "- **NFLX:** direct, apparently fresh Wells Fargo downgrade; target change and timing align. The existing UNCLEAR label should be reviewed, not edited automatically.",
              "- **SNPS:** move was +2.868833%; it was present in the original manual review queue but remains unlabeled. Contemporaneous, direct, direction-compatible analyst action makes a manual label decision worthwhile.", "",
              "## Six remaining false negatives", "",
              "| Date | Symbol | Move | Cache classification | Main finding |", "|---|---|---:|---|---|"]
    for row in data["remaining_false_negatives"]:
        lines.append(f"| {row['date']} | {row['symbol']} | {row['move_pct']:+.2f}% | {row['cache_classification']} | {row['root_cause']} — {row['pipeline_failure_stage']} |")
    lines += ["", "The exact residual set is AMD (Sep 17), ORCL (Sep 17), AMAT (Sep 18), KLAC (Sep 18), LRCX (Sep 18), and PANW (Sep 23). One is narrowly fixable using existing cache; three have no suitable cited research in the inspected cache; AMD has present-but-insufficiently-scoped evidence; ORCL needs gold-label review.", "",
              "### PANW Sep 23 point-in-time trace", "",
              "The exact dedicated Unit 42 launch story published Sep 24 at 19:11 ET is future evidence and is excluded. However, the Sep 23 cache includes a Yahoo story at 14:57 ET whose summary explicitly says PANW shares jumped after the Unit 42 Continuous Frontier AI Defense launch. That item is point-in-time eligible and direct-subject, but the replay did not surface it as a candidate; surfaced PANW candidates were OTHER/incidental with subject score 0 and failed the concrete-event gate. This is a narrow candidate/classification recovery opportunity, not permission to use future evidence.", "",
              "### AMD and ORCL evidence scope", "",
              "AMD’s broad tech/semiconductor material does not establish a sufficiently scoped causal link; do not widen semiconductor evidence to all technology stocks. ORCL’s +5.19% move occurred on a strong QQQ/technology day, consistent with the manual broad-tech rationale, but its cached stock-specific items are incidental and no Oracle-specific event is usable. Keep its label pending manual review; current evidence does not support automatic removal from the FN set.", "",
              "## NFLX sensitivity", "",
              "Current labeled metrics exclude NFLX (UNCLEAR) and SNPS (unlabeled). If NFLX is reviewed as a valid gold positive: precision-like **90.5%** (19/21), recall-like **76.0%** (19/25). If it is instead adjudicated a false positive: precision-like **85.7%** (18/21), recall-like **75.0%** (18/24). SNPS remains outside these metrics pending a manual label.", "",
              "## Recommended next engineering decision", "",
              "**STOP_AND_EXPAND_EVALUATION_DATASET.** The residual error mechanisms are heterogeneous, only one FN has a narrow cache-backed path, and three lack the cited point-in-time evidence. With ten dates and six residual FNs, another tweak risks overfitting. Review NFLX/ORCL/AMD labels or rationales manually, then expand to an independently labeled date range before another production change.", "",
              "Artifacts: `new_stock_specific_review.csv`, `remaining_false_negatives.csv`, `gold_label_review_candidates.csv`, and `next_step_recommendation.md`.", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
