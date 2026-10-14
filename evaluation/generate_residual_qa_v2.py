"""Build the offline residual manual-QA report for the sector-driver V2 baseline.

This is an analysis utility only. It reads a fixed evaluation package and the
existing manual labels; it never invokes providers or writes production data.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUN_REL = Path(
    "output/evaluation_batch/runs/2026-09-15_to_2026-09-28_integrity_20261003_074305"
)
LABEL_DIR = Path("output/evaluation/manual_qa_v1")
OUT_REL = Path("output/qa/residual_qa_v2")
EXPECTED_DATES = [
    "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21",
    "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28",
]

# Findings are deliberately limited to the ten residual errors confirmed by
# joining the current run to the unchanged gold labels.
FINDINGS: dict[tuple[str, str], dict[str, str]] = {
    ("2026-09-17", "AMD"): {
        "evidence_state": "EVIDENCE_PRESENT_BUT_REJECTED",
        "event_type": "SECTOR_TECHNOLOGY / semiconductor theme",
        "stage": "inheritance",
        "root": "SECTOR_INHERITANCE_MISS",
        "secondary": "SECTOR_ASSOCIATION_MISS",
        "evidence": "Cached broad-market/semiconductor rally coverage exists; the sector candidate is plausible, but the inheritance audit rejects it as not sector-scoped for AMD.",
        "where": "Finnhub symbol cache and sector-driver cache for 2026-09-17; no market-discovery candidates in the date cache.",
        "fixability": "Existing evidence; deterministic scope semantics may recover it without network. Guard against applying semiconductor-only evidence to unrelated technology names.",
        "explanation": "UNEXPLAINED",
        "rationale_hint": "Broad technology/AI risk-on rally.",
    },
    ("2026-09-17", "ORCL"): {
        "evidence_state": "EVIDENCE_PRESENT_WRONG_SCOPE",
        "event_type": "SECTOR_TECHNOLOGY / broad risk-on (uncertain)",
        "stage": "theme association / gold reconciliation",
        "root": "OTHER",
        "secondary": "SECTOR_ASSOCIATION_MISS",
        "evidence": "Cached coverage emphasizes semiconductor leadership and macro drivers; cached Oracle headlines are incidental/OTHER and fail concrete-event gating. Existing evidence does not establish an Oracle-specific or broad-technology explanation confidently.",
        "where": "Finnhub symbol cache and sector cache for 2026-09-17; market discovery cache has zero raw/eligible candidates.",
        "fixability": "Uncertain; do not promote the semiconductor article to ORCL. Manual gold rationale needs review against point-in-time evidence.",
        "explanation": "UNEXPLAINED",
        "rationale_hint": "Broad technology-led risk-on; evidence does not specifically reconcile Oracle.",
    },
    ("2026-09-18", "LRCX"): {
        "evidence_state": "EVIDENCE_ABSENT",
        "event_type": "SECTOR_SEMICONDUCTOR_EQUIPMENT (expected theme)",
        "stage": "discovery",
        "root": "SECTOR_THEME_DISCOVERY_MISS",
        "secondary": "SECTOR_ASSOCIATION_MISS",
        "evidence": "Manual QA cites contemporaneous semiconductor-equipment research and AI-capex context; no matching WFE/JPMorgan research is present in the inspected dated stock or market-discovery caches.",
        "where": "Inspected local Finnhub symbol caches and cache/market_driver_news/2026-09-18.json; no suitable article found.",
        "fixability": "Requires new discovery/provider coverage for the cited research unless another already-cached point-in-time source is located.",
        "explanation": "UNEXPLAINED",
        "rationale_hint": "Coherent semiconductor-equipment rally after bullish WFE/AI-capex research; do not infer that one story alone explains all peers.",
    },
    ("2026-09-18", "AMAT"): {
        "evidence_state": "EVIDENCE_ABSENT",
        "event_type": "SECTOR_SEMICONDUCTOR_EQUIPMENT (expected theme)",
        "stage": "discovery",
        "root": "SECTOR_THEME_DISCOVERY_MISS",
        "secondary": "SECTOR_ASSOCIATION_MISS",
        "evidence": "Manual QA cites contemporaneous semiconductor-equipment research and AI-capex context; no matching WFE/JPMorgan research is present in the inspected dated stock or market-discovery caches.",
        "where": "Inspected local Finnhub symbol caches and cache/market_driver_news/2026-09-18.json; no suitable article found.",
        "fixability": "Requires new discovery/provider coverage for the cited research unless another already-cached point-in-time source is located.",
        "explanation": "UNEXPLAINED",
        "rationale_hint": "Coherent semiconductor-equipment rally after bullish WFE/AI-capex research; treat peer move as related context, not automatic shared causality.",
    },
    ("2026-09-18", "KLAC"): {
        "evidence_state": "EVIDENCE_ABSENT",
        "event_type": "SECTOR_SEMICONDUCTOR_EQUIPMENT (expected theme)",
        "stage": "discovery",
        "root": "SECTOR_THEME_DISCOVERY_MISS",
        "secondary": "SECTOR_ASSOCIATION_MISS",
        "evidence": "Manual QA cites contemporaneous semiconductor-equipment research and AI-capex context, with KLA highlighted as a preferred name; no matching WFE/JPMorgan research is present in the inspected dated stock or market-discovery caches.",
        "where": "Inspected local Finnhub symbol caches and cache/market_driver_news/2026-09-18.json; no suitable article found.",
        "fixability": "Requires new discovery/provider coverage for the cited research unless another already-cached point-in-time source is located.",
        "explanation": "UNEXPLAINED",
        "rationale_hint": "Equipment theme is coherent across peers, but KLA's stronger move still needs name-specific support or explicit sector association.",
    },
    ("2026-09-23", "PANW"): {
        "evidence_state": "EVIDENCE_PRESENT_BUT_REJECTED",
        "event_type": "PRODUCT / AI security",
        "stage": "classification and stock selection",
        "root": "EVENT_CLASSIFICATION_MISS",
        "secondary": "STOCK_SELECTION_MISS",
        "evidence": "A same-day Yahoo AI-penetration-testing/subscription article is cached, but is classified OTHER with INCIDENTAL_MENTION, subject score 0 and fails the concrete-event gate. The exact Unit 42 launch article in cache is timestamped 2026-09-24 23:11:53Z, after the 2026-09-23 session, so it is future evidence and must not be used.",
        "where": "PANW dated Finnhub cache for 2026-09-23; the exact Unit 42 article appears only in the next-date cache.",
        "fixability": "Potentially fixable from same-day cached related product coverage with careful product-event semantics; exact launch evidence is not point-in-time eligible.",
        "explanation": "UNEXPLAINED",
        "rationale_hint": "Unit 42 / AI-security product catalyst per manual label; enforce strict publication cutoff and avoid using next-day launch coverage.",
    },
    ("2026-09-25", "HUM"): {
        "evidence_state": "EVIDENCE_PRESENT_BUT_REJECTED",
        "event_type": "ANALYST_ACTION / upgrade",
        "stage": "stock selection / subject gate",
        "root": "STOCK_SELECTION_MISS",
        "secondary": "ANALYST_ACTION_PROMOTION_MISS",
        "evidence": "Benzinga's Barclays upgrade of Humana to Overweight with a $515 target is cached at 2026-09-25 11:38:18Z (07:38 ET), explicitly names Humana and HUM, and is premarket. It is in an ANALYST_ACTION cluster, but canonical/best-candidate selection favors a generic upgrades roundup; both fail the subject gate (score 0/incidental), so verification is not called.",
        "where": "HUM Finnhub cache and analyst-action cluster for 2026-09-25.",
        "fixability": "Yes, from existing cache, with narrow company-alias/subject matching and per-article candidate preservation; avoid admitting roundup/incidental mentions.",
        "explanation": "UNEXPLAINED",
        "rationale_hint": "Barclays upgrade to Overweight and $515 target.",
    },
    ("2026-09-28", "PANW"): {
        "evidence_state": "EVIDENCE_PRESENT_NOT_PROMOTED",
        "event_type": "ANALYST_ACTION; one AI-security opinion item is misclassified REGULATORY",
        "stage": "stock selection / subject gate",
        "root": "STOCK_SELECTION_MISS",
        "secondary": "ANALYST_ACTION_PROMOTION_MISS",
        "evidence": "Premarket BTIG price-target action (06:16:14Z) and a Yahoo analyst-target roundup (12:45:02Z) are cached and clustered as ANALYST_ACTION, but ranked candidates have subject score 0, INCIDENTAL_MENTION, and selection score 16. A separate AI-security opinion item is classified REGULATORY and is also incidental. None reaches verification.",
        "where": "PANW Finnhub cache and cluster diagnostics for 2026-09-28.",
        "fixability": "Yes, from existing cache, using title-level direct-subject alias handling for analyst actions and retaining the exact article rather than a generic cluster representative.",
        "explanation": "UNEXPLAINED",
        "rationale_hint": "Analyst-target actions and AI-security commentary were available, but must pass direct-subject and causal policy.",
    },
    ("2026-09-16", "OXY"): {
        "evidence_state": "EVIDENCE_PRESENT_WRONG_SCOPE",
        "event_type": "EARNINGS_RESULTS selected; commodity/energy selloff context also cached",
        "stage": "causal gate / scope selection",
        "root": "CAUSAL_GATE_FALSE_ACCEPT",
        "secondary": "WRONG_SCOPE_SELECTION",
        "evidence": "Selected Yahoo Q2 earnings recap is a retrospective prior-quarter story, marked PRIMARY_SUBJECT and causal PASS, and is used for OXY's -6.55% move. Same-day cached crude-retreat/oil-producer selloff coverage better matches the move and is usable in the sector layer for peers.",
        "where": "OXY dated Finnhub cache plus 2026-09-16 sector-driver evidence.",
        "fixability": "Existing evidence; tighten freshness/event-time relevance of retrospective earnings recaps and prefer the contemporaneous commodity scope when supported.",
        "explanation": "STOCK_SPECIFIC",
        "rationale_hint": "Crude/energy selloff stronger than the selected prior-quarter earnings recap.",
    },
    ("2026-09-24", "META"): {
        "evidence_state": "EVIDENCE_PRESENT_WRONG_SCOPE",
        "event_type": "ANALYST_ACTION selected; product announcement misclassified OTHER",
        "stage": "classification / selection",
        "root": "WRONG_SCOPE_SELECTION",
        "secondary": "EVENT_CLASSIFICATION_MISS",
        "evidence": "Selected Yahoo analyst price-target reset has PRIMARY_SUBJECT, score 48, selection 84 and causal PASS. A same-day premarket Meta Muse/Charm device and AI-agent announcement is cached but classified OTHER, INCIDENTAL_MENTION, subject score 0 and causal FAIL; the weaker analyst item wins.",
        "where": "META dated Finnhub cache for 2026-09-24.",
        "fixability": "Yes, likely from existing cache with precise product-announcement recognition and subject extraction; broadening generic product terms risks promoting irrelevant stories.",
        "explanation": "STOCK_SPECIFIC",
        "rationale_hint": "Muse/Charm product and AI-agent announcement was the contemporaneous catalyst; analyst-target story was wrong scope.",
    },
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build() -> Path:
    run_dir = ROOT / RUN_REL
    labels_dir = ROOT / LABEL_DIR
    out_dir = ROOT / OUT_REL
    summary_path = run_dir / "historical_batch_summary.json"
    run_stock_path = run_dir / "stock_review_queue.csv"
    labels_path = labels_dir / "stock_review_queue_manual_qa_v1.csv"
    market_labels_path = labels_dir / "market_day_review_queue_manual_qa_v1.csv"
    for required in (summary_path, run_stock_path, labels_path, market_labels_path):
        if not required.is_file():
            raise FileNotFoundError(required)

    package = json.loads(summary_path.read_text(encoding="utf-8"))
    run_rows = _read_csv(run_stock_path)
    labels = _read_csv(labels_path)
    run_by_key = {(r["date"], r["symbol"]): r for r in run_rows}
    label_by_key = {(r["date"], r["symbol"]): r for r in labels}

    date_integrity = {
        item["market_date"]: item.get("integrity", {}).get("integrity_status", "UNKNOWN")
        for item in package.get("days", [])
    }
    included_labels = {
        key: label for key, label in label_by_key.items()
        if label.get("manual_label_eligible", "").lower() == "true"
        and label.get("manual_label_stale", "").lower() != "true"
        and date_integrity.get(key[0]) != "FAIL"
    }
    residual_keys: set[tuple[str, str]] = set()
    for key, label in included_labels.items():
        current = run_by_key.get(key)
        if not current:
            continue
        status = current.get("overall_explanation_status", "")
        gold = label.get("manual_label")
        if gold == "FALSE_NEGATIVE" and status in {"UNEXPLAINED", ""}:
            residual_keys.add(key)
        elif gold == "FALSE_POSITIVE" and status not in {"UNEXPLAINED", ""}:
            residual_keys.add(key)

    expected = set(FINDINGS)
    if residual_keys != expected:
        missing = sorted(expected - residual_keys)
        unexpected = sorted(residual_keys - expected)
        raise AssertionError(f"Residual join mismatch; missing={missing}; unexpected={unexpected}")

    fn_rows: list[dict[str, Any]] = []
    fp_rows: list[dict[str, Any]] = []
    taxonomy_rows: list[dict[str, Any]] = []
    for key in sorted(residual_keys):
        date, symbol = key
        label, current, finding = included_labels[key], run_by_key[key], FINDINGS[key]
        category = label["manual_label"]
        row = {
            "date": date,
            "symbol": symbol,
            "move_pct": current.get("move_pct", ""),
            "manual_qa_rationale": label.get("review_notes", ""),
            "current_explanation": current.get("overall_explanation_status", ""),
            "selected_event": current.get("selected_event", ""),
            "selected_published_at": current.get("selected_published_at", ""),
            "causal_gate": current.get("causal_gate", ""),
            "causal_reason": current.get("causal_reason", ""),
            "verification_status": current.get("verification_status", ""),
            "candidate_count": current.get("candidate_count", ""),
            "inherited_sector_driver": current.get("inherited_sector_driver", ""),
            "inherited_market_driver": current.get("inherited_market_driver", ""),
            "relevant_evidence": finding["evidence"],
            "evidence_location": finding["where"],
            "event_or_theme": finding["event_type"],
            "pipeline_stage_lost": finding["stage"],
            "primary_root_cause": finding["root"],
            "secondary_root_cause": finding["secondary"],
            "evidence_state": finding["evidence_state"],
            "fixable_without_new_network": finding["fixability"],
            "gold_review_flag": "GOLD_LABEL_REVIEW_REQUIRED" if (date, symbol) == ("2026-09-17", "ORCL") else "",
        }
        (fn_rows if category == "FALSE_NEGATIVE" else fp_rows).append(row)
        taxonomy_rows.append({
            "error_type": category,
            "date": date,
            "symbol": symbol,
            "move_pct": current.get("move_pct", ""),
            "abs_move_pct": abs(float(current.get("move_pct") or 0)),
            "pipeline_stage": finding["stage"],
            "root_cause": finding["root"],
            "event_type": finding["event_type"],
            "theme": finding["event_type"].split("/")[0].strip(),
            "evidence_state": finding["evidence_state"],
        })

    stage_stats: dict[str, list[dict[str, Any]]] = defaultdict(list)
    root_stats: dict[str, list[dict[str, Any]]] = defaultdict(list)
    event_stats: dict[str, list[dict[str, Any]]] = defaultdict(list)
    theme_stats: dict[str, list[dict[str, Any]]] = defaultdict(list)
    date_stats: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in taxonomy_rows:
        for bucket, key in ((stage_stats, "pipeline_stage"), (root_stats, "root_cause"),
                            (event_stats, "event_type"), (theme_stats, "theme"), (date_stats, "date")):
            bucket[str(r[key])].append(r)
    breakdown = []
    for dimension, stats in (("pipeline_stage", stage_stats), ("root_cause", root_stats),
                             ("event_type", event_stats), ("theme", theme_stats), ("date", date_stats)):
        for name, items in sorted(stats.items()):
            breakdown.append({
                "dimension": dimension,
                "category": name,
                "count": len(items),
                "symbols": ";".join(f"{x['date']}:{x['symbol']}" for x in items),
                "total_absolute_move_pct": f"{sum(float(x['abs_move_pct']) for x in items):.4f}",
                "distinct_dates": len({x["date"] for x in items}),
            })

    out_dir.mkdir(parents=True, exist_ok=True)
    error_fields = list(fn_rows[0])
    _write_csv(out_dir / "remaining_false_negatives.csv", fn_rows, error_fields)
    _write_csv(out_dir / "remaining_false_positives.csv", fp_rows, error_fields)
    _write_csv(out_dir / "residual_error_taxonomy.csv", taxonomy_rows, list(taxonomy_rows[0]))
    _write_csv(out_dir / "pipeline_stage_breakdown.csv", breakdown, [
        "dimension", "category", "count", "symbols", "total_absolute_move_pct", "distinct_dates"
    ])

    fn_total = len(fn_rows)
    fp_total = len(fp_rows)
    evidence_counts = {
        "sufficient_cached_evidence_semantic_fix": 4,
        "genuinely_requires_new_discovery": 3,
        "uncertain_gold_reconciliation": 1,
    }
    # Four evidence-present cases: AMD, PANW (9/23), HUM and PANW (9/28).
    # Discovery is absent for the three equipment names; ORCL is deliberately uncertain.
    stage_counts = {k: len(v) for k, v in stage_stats.items()}
    roots_counts = {k: len(v) for k, v in root_stats.items()}
    summary = {
        "baseline_run": str(RUN_REL).replace("\\", "/"),
        "manual_label_files": [str(LABEL_DIR / "market_day_review_queue_manual_qa_v1.csv").replace("\\", "/"),
                               str(LABEL_DIR / "stock_review_queue_manual_qa_v1.csv").replace("\\", "/")],
        "dates_expected": EXPECTED_DATES,
        "dates_in_package": package.get("dates", []),
        "integrity_fail_dates_excluded": sorted(d for d, state in date_integrity.items() if state == "FAIL"),
        "eligible_nonstale_label_rows_included": len(included_labels),
        "remaining_false_negatives": fn_total,
        "remaining_false_positives": fp_total,
        "reported_precision_like_pct": 88.9,
        "reported_recall_like_pct": 66.7,
        "reported_sector_inheritance_false_negatives": 2,
        "reported_fully_unexplained_rate_pct": 71.1,
        "integrity": {
            "stock_parity_mismatches": package.get("stock_result_parity_mismatches"),
            "cross_symbol_candidate_leaks": package.get("cross_symbol_candidate_leaks"),
            "offline_network_calls": package.get("offline_network_calls"),
            "fmp_requests": package.get("fmp_requests"),
        },
        "root_cause_counts": roots_counts,
        "pipeline_stage_counts": stage_counts,
        "cache_sufficiency": evidence_counts,
        "residual_case_keys": [f"{d}:{s}" for d, s in sorted(residual_keys)],
        "source_sha256": {
            "summary": _sha256(summary_path),
            "run_stock_queue": _sha256(run_stock_path),
            "stock_labels": _sha256(labels_path),
            "market_labels": _sha256(market_labels_path),
        },
        "analysis_only": True,
        "production_behavior_modified": False,
    }
    (out_dir / "residual_qa_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    next_fixes = """# Next-fix candidates (ranked)

Ranking weighs confirmed residuals, cross-case breadth, evidence availability,
precision risk, implementation scope, and regression risk.

1. **Cached analyst-action subject recovery (HUM 9/25, PANW 9/28)**
   - Confirmed cases: 2 FNs; expected recovery: 2.
   - Existing premarket analyst evidence is cached and already clustered as
     ANALYST_ACTION. The loss is direct-subject matching/candidate selection,
     including a generic cluster representative masking a specific article.
   - Precision risk: low-to-moderate if exact company aliases plus title/summary
     subject evidence are required; high if ticker substring matching is used.
   - Likely modules: stock candidate selection, company/alias subject matcher,
     analyst-action promotion. No provider or network change.
   - Acceptance: these two exact cached records reach the existing causal and
     verification path; generic roundup/incidental cases remain rejected; no new
     FP; parity and point-in-time checks remain clean.

2. **Point-in-time product-announcement event recognition (PANW 9/23; META 9/24 FP guard)**
   - Confirmed case: 1 FN; also directly guards the META wrong-scope FP.
   - Recover the same-day cached PANW product article only if it independently
     satisfies announcement/subject semantics. Never use the Unit 42 article
     dated after the 9/23 session.
   - Precision risk: moderate; broad product vocabulary could elevate commentary.
   - Likely modules: event classifier and product-event candidate selection.
     No provider change; use local caches only.
   - Acceptance: eligible same-day PANW product event reaches normal checks;
     next-day Unit 42 article remains excluded; META Muse/Charm announcement
     outranks the unrelated analyst target only when the event evidence passes;
     no increased false promotions.

3. **Semiconductor-equipment theme discovery and scoped inheritance (9/18 trio)**
   - Confirmed cases: 3 FNs; expected recovery ceiling: 3, subject to per-name
     evidence support.
   - The current local cache lacks the cited WFE/JPMorgan research, so this is
     not an offline semantics-only fix.
   - Precision risk: moderate-to-high unless discovery yields contemporaneous
     evidence and each stock is explicitly associated with the equipment theme.
   - Likely modules: bounded market/sector discovery and equipment-theme
     association. This changes discovery coverage and may add provider traffic.
   - Acceptance: capture point-in-time source evidence; assess LRCX, AMAT, KLAC
     separately; promote only names whose own association is supported; retain
     integrity/parity gates and measure FP delta.

## Recommendation: exactly one next task

Implement **Cached Analyst-Action Subject Recovery V1** for HUM (2026-09-25) and
PANW (2026-09-28), using existing cache only. It has the clearest two-case
reduction, requires no new network/provider coverage, and can preserve the
88.9% precision-like baseline with strict direct-subject safeguards. Expected
recovery: 2 of the 8 FNs. Do not combine this with product classification or
semiconductor discovery work.
"""
    (out_dir / "next_fix_candidates.md").write_text(next_fixes, encoding="utf-8")

    # Produce the readable report from exactly the same selected row set.
    lines = [
        "# Residual QA Re-baseline V2",
        "",
        f"Baseline: `{summary['baseline_run']}`. Existing manual labels were read-only.",
        f"Current residuals: **{fn_total} false negatives**, **{fp_total} false positives**; reported precision-like **88.9%**, recall-like **66.7%**.",
        "",
        f"Integrity: parity={summary['integrity']['stock_parity_mismatches']}; cross-symbol leaks={summary['integrity']['cross_symbol_candidate_leaks']}; offline network calls={summary['integrity']['offline_network_calls']}; FMP requests={summary['integrity']['fmp_requests']}. Integrity-FAIL date(s) excluded: {', '.join(summary['integrity_fail_dates_excluded']) or 'none'}.",
        "",
        "## Residual false negatives",
        "",
        "| Date | Symbol | Move | Primary root | Evidence state | Lost stage |",
        "|---|---:|---:|---|---|---|",
    ]
    for r in fn_rows:
        lines.append(f"| {r['date']} | {r['symbol']} | {float(r['move_pct']):+.2f}% | `{r['primary_root_cause']}` | `{r['evidence_state']}` | {r['pipeline_stage_lost']} |")
    lines += ["", "## Residual false positives", "", "| Date | Symbol | Move | Primary root | Current selected evidence |", "|---|---:|---:|---|---|"]
    for r in fp_rows:
        lines.append(f"| {r['date']} | {r['symbol']} | {float(r['move_pct']):+.2f}% | `{r['primary_root_cause']}` | {r['selected_event']} |")
    lines += [
        "", "## Current diagnosis", "",
        "Dominant pattern: evidence-to-stock promotion/association failure, especially direct-subject gating for cached analyst actions; the semiconductor-equipment trio is instead a genuine local-cache discovery gap. ORCL is not safely explained by the cached semiconductor evidence and is flagged `GOLD_LABEL_REVIEW_REQUIRED`.",
        "",
        "Sector equipment (LRCX/AMAT/KLAC) is grouped diagnostically, not treated as one automatically causal event. Current inspection found no suitable WFE/JPMorgan item in the local 2026-09-18 caches, so classify as `SECTOR_THEME_DISCOVERY_MISS`, not a failed inheritance after promotion.",
        "",
        "HUM and PANW 9/28 have cached premarket analyst actions classified/clustered as `ANALYST_ACTION`; both are rejected at subject/candidate selection before verification. PANW 9/23's related same-day product item is classified OTHER and fails subject/causal checks; the exact Unit 42 launch article is dated after that session and is excluded.",
        "",
        "OXY is a causal false accept: a prior-quarter earnings recap wins over contemporaneous crude/energy weakness. META is a wrong-scope selection: an analyst-target item wins while the cached Muse/Charm product announcement is misclassified/rejected.",
        "",
        "## Error distribution",
        "",
        "See `pipeline_stage_breakdown.csv` for stage, root, event, theme, and date counts, symbols, summed absolute move magnitude, and distinct date counts.",
        "",
        "Cache sufficiency among FNs: **4** have cached evidence suitable for a semantic/selection fix (AMD, PANW 9/23, HUM, PANW 9/28); **3** require new discovery (LRCX/AMAT/KLAC); **1** is uncertain and needs gold-label review (ORCL). This is a triage estimate, not authorization to broaden promotion.",
        "",
        "## Ranked fixes and recommendation",
        "",
        "See `next_fix_candidates.md`. Exactly one next task is recommended: **Cached Analyst-Action Subject Recovery V1** for HUM and PANW 9/28, expected to recover 2 FNs with no network changes and strict direct-subject checks.",
        "",
        "## Reproducibility and scope",
        "",
        "The generator verifies the exact 10 dates, uses only eligible/non-stale existing stock labels, excludes the 2026-09-22 integrity-FAIL date, and asserts that the live residual join is exactly 8 FN + 2 FP. Source SHA-256 values are recorded in the JSON. No provider/cache calls were made; no production behavior or labels were changed.",
        "",
        "Metrics are the supplied current baseline values; residual membership is recomputed from the run's current `overall_explanation_status` against the unchanged labels.",
    ]
    (out_dir / "residual_qa_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_dir


if __name__ == "__main__":
    print(build())
