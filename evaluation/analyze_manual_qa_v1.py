"""Build a reproducible QA taxonomy from the V1 manual labels and offline replay."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
INPUT_DIR = ROOT / "output" / "evaluation" / "manual_qa_v1"
OUTPUT_DIR = ROOT / "output" / "qa" / "manual_qa_v1"
ALLOWED_LABELS = {
    "CORRECT_CAUSAL", "PLAUSIBLE_CAUSAL", "FALSE_POSITIVE",
    "FALSE_NEGATIVE", "NO_PUBLIC_CATALYST", "UNCLEAR",
}
TAXONOMY = [
    "MARKET_DISCOVERY_MISS", "MARKET_PROMOTION_MISS",
    "SECTOR_THEME_DISCOVERY_MISS", "SECTOR_PROMOTION_MISS",
    "STOCK_DISCOVERY_MISS", "EVENT_CLASSIFICATION_MISS",
    "ANALYST_ACTION_MISS", "CAUSAL_GATE_FALSE_REJECT",
    "CAUSAL_GATE_FALSE_ACCEPT", "VERIFICATION_MISS",
    "MARKET_INHERITANCE_FALSE_POSITIVE", "MARKET_INHERITANCE_FALSE_NEGATIVE",
    "SECTOR_INHERITANCE_FALSE_POSITIVE", "SECTOR_INHERITANCE_FALSE_NEGATIVE",
    "TIMING_ERROR", "SUBJECT_MATCH_ERROR", "NOISE_SELECTION_ERROR", "OTHER",
]
EVENT_TYPES = [
    "ANALYST_ACTION", "EARNINGS", "REGULATORY", "PRODUCT", "PARTNERSHIP",
    "M&A", "MACRO", "RATES", "OIL/COMMODITY", "AI_THEME", "SECTOR_THEME", "OTHER",
]
STAGES = [
    "DISCOVERY", "CLASSIFICATION", "CAUSAL_GATE", "VERIFICATION",
    "MARKET_DRIVER", "SECTOR_DRIVER", "INHERITANCE", "SELECTION/RANKING",
]

# Primary classifications are analyst judgments grounded in the manual notes and
# the corresponding replay path. The evidence fields below are extracted afresh
# from the dated artifacts on every run.
ROOT_CAUSE_BY_CASE = {
    ("2026-09-15", "MARKET"): ("MARKET_PROMOTION_MISS", "CAUSAL_GATE_FALSE_ACCEPT", "A wrong-scope energy hypothesis was promoted as the market's primary explanation despite broad index weakness."),
    ("2026-09-16", "MARKET"): ("MARKET_DISCOVERY_MISS", "MARKET_PROMOTION_MISS", "Macro/market headlines were present in symbol-news candidates, but the market-level candidate set and primary driver were empty."),
    ("2026-09-17", "MARKET"): ("MARKET_DISCOVERY_MISS", "MARKET_PROMOTION_MISS", "A Nasdaq/AI rally headline existed in symbol-news candidates, but market-level discovery produced no candidate or promoted driver."),
    ("2026-09-21", "MARKET"): ("MARKET_DISCOVERY_MISS", "MARKET_PROMOTION_MISS", "Same-day Nasdaq/AI rally evidence existed in stock-news caches, but no market-level candidate or driver was built."),
    ("2026-09-15", "TMO"): ("MARKET_INHERITANCE_FALSE_POSITIVE", "SUBJECT_MATCH_ERROR", "A sector-scoped energy hypothesis related to XLE was used for TMO without a subject/sector relationship."),
    ("2026-09-15", "QCOM"): ("MARKET_INHERITANCE_FALSE_POSITIVE", "SUBJECT_MATCH_ERROR", "A sector-scoped energy hypothesis related to XLE was used for QCOM without a subject/sector relationship."),
    ("2026-09-16", "OXY"): ("NOISE_SELECTION_ERROR", "CAUSAL_GATE_FALSE_ACCEPT", "A stale Q2 earnings recap was selected for a same-day decline while cached coverage described the oil-producer selloff."),
    ("2026-09-16", "COP"): ("SECTOR_INHERITANCE_FALSE_NEGATIVE", "SECTOR_PROMOTION_MISS", "The replay built a down energy-sector driver and cached oil-producer coverage, but COP remained unexplained."),
    ("2026-09-16", "EOG"): ("SECTOR_INHERITANCE_FALSE_NEGATIVE", "SECTOR_PROMOTION_MISS", "The replay built a down energy-sector driver and cached oil-producer coverage, but EOG remained unexplained."),
    ("2026-09-17", "AMD"): ("SECTOR_INHERITANCE_FALSE_NEGATIVE", "MARKET_INHERITANCE_FALSE_NEGATIVE", "A rising technology-sector driver existed on the broad AI/risk-on day but was not reflected in AMD's explanation."),
    ("2026-09-17", "MU"): ("SECTOR_INHERITANCE_FALSE_NEGATIVE", "MARKET_INHERITANCE_FALSE_NEGATIVE", "A rising technology-sector driver and same-day AI/semiconductor coverage existed but were not reflected in MU's explanation."),
    ("2026-09-17", "ORCL"): ("SECTOR_INHERITANCE_FALSE_NEGATIVE", "MARKET_INHERITANCE_FALSE_NEGATIVE", "A rising technology-sector driver existed but ORCL remained unexplained."),
    ("2026-09-18", "LRCX"): ("SECTOR_PROMOTION_MISS", "SECTOR_THEME_DISCOVERY_MISS", "Same-day semiconductor-equipment group coverage was in symbol candidates, while no matching semiconductor-equipment sector driver was built."),
    ("2026-09-18", "AMAT"): ("SECTOR_PROMOTION_MISS", "SECTOR_THEME_DISCOVERY_MISS", "Same-day semiconductor-equipment group coverage was in symbol candidates, while no matching semiconductor-equipment sector driver was built."),
    ("2026-09-18", "KLAC"): ("SECTOR_PROMOTION_MISS", "SECTOR_THEME_DISCOVERY_MISS", "Same-day chip-equipment coverage and a KLAC-focused candidate were present, while no matching sector driver was built."),
    ("2026-09-21", "QCOM"): ("SECTOR_INHERITANCE_FALSE_NEGATIVE", "MARKET_INHERITANCE_FALSE_NEGATIVE", "A rising technology-sector driver and same-day AI/interconnect coverage existed, but QCOM was left unexplained."),
    ("2026-09-21", "LRCX"): ("SECTOR_INHERITANCE_FALSE_NEGATIVE", "MARKET_INHERITANCE_FALSE_NEGATIVE", "A rising technology-sector driver existed, but LRCX's AI/capex-linked move was left unexplained."),
    ("2026-09-23", "ABNB"): ("MARKET_INHERITANCE_FALSE_POSITIVE", "SECTOR_INHERITANCE_FALSE_POSITIVE", "Generic market-risk explanation displaced explicit cached travel-booking/Muse disintermediation coverage."),
    ("2026-09-23", "BKNG"): ("MARKET_INHERITANCE_FALSE_POSITIVE", "SECTOR_INHERITANCE_FALSE_POSITIVE", "Generic market-risk explanation displaced explicit cached travel-booking/Muse disintermediation coverage."),
    ("2026-09-23", "PANW"): ("STOCK_DISCOVERY_MISS", "EVENT_CLASSIFICATION_MISS", "Related PANW AI-security coverage existed, but the exact Unit 42 Continuous Frontier AI Defense launch was not represented in the candidate set."),
    ("2026-09-24", "META"): ("NOISE_SELECTION_ERROR", "ANALYST_ACTION_MISS", "A generic price-target item was selected over cached contemporaneous Muse/Charm product coverage cited by manual QA."),
    ("2026-09-25", "HUM"): ("ANALYST_ACTION_MISS", "CAUSAL_GATE_FALSE_REJECT", "Barclays upgrade/target coverage was present in the symbol candidates, but the actionable analyst event was not selected."),
    ("2026-09-28", "PANW"): ("EVENT_CLASSIFICATION_MISS", "ANALYST_ACTION_MISS", "AI-security/analyst evidence was present in cached candidates, but the replay selected no event and left PANW unexplained."),
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    return str(value)


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value for key, value in row.items()})


def event_type_for(row: dict[str, str], market: bool) -> str:
    date, symbol = row.get("date", ""), row.get("symbol", "")
    note = " ".join(str(row.get(k, "")) for k in ("review_notes", "selected_event", "selected_market_story", "primary_market_driver", "market_regimes")).lower()
    has_ai = bool(re.search(r"\bai(?:[- ]|\b)", note))
    if market:
        if any(x in note for x in ("oil_rally", "oil shock", "oil selloff", "crude", "oil")):
            return "OIL/COMMODITY"
        if has_ai or "tech-led" in note or "broad_risk_on" in note:
            return "AI_THEME"
        if any(x in note for x in ("yield", "treasury", "rate-sensitive", "rates")):
            return "RATES"
        if "semiconductor" in note:
            return "SECTOR_THEME"
        if "sector" in note or date == "2026-09-18":
            return "SECTOR_THEME"
        return "MACRO"
    if (date, symbol) == ("2026-09-16", "OXY"):
        return "EARNINGS"
    if any(x in note for x in ("upgrade", "price target", "analyst", "barclays", "morgan stanley", "btig")):
        return "ANALYST_ACTION"
    if any(x in note for x in ("faa", "regulatory", "sec edgar")):
        return "REGULATORY"
    if any(x in note for x in ("partnership", "agreement", "contract")):
        return "PARTNERSHIP"
    if any(x in note for x in ("acquisition", "merger", "m&a")):
        return "M&A"
    if any(x in note for x in ("earnings", "q2", "q3")):
        return "EARNINGS"
    if any(x in note for x in ("launch", "product", "muse", "charm", "unit 42")):
        return "PRODUCT"
    if has_ai or any(x in note for x in ("semiconductor", "chip", "capex", "security")):
        return "AI_THEME"
    if any(x in note for x in ("oil", "crude", "energy")):
        return "OIL/COMMODITY"
    if any(x in note for x in ("sector", "travel stocks")):
        return "SECTOR_THEME"
    return "OTHER"


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(row["manual_label"] for row in rows)
    tp = counts["CORRECT_CAUSAL"] + counts["PLAUSIBLE_CAUSAL"]
    fp, fn = counts["FALSE_POSITIVE"], counts["FALSE_NEGATIVE"]
    precision_den, recall_den = tp + fp, tp + fn
    n = len(rows)
    return {
        "labeled_rows": n,
        "label_counts": dict(counts),
        "positive_supported": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "precision_like": tp / precision_den if precision_den else None,
        "recall_like": tp / recall_den if recall_den else None,
        # With no labeled true-negative class, report FP among predicted-positive labels.
        "false_positive_rate": fp / precision_den if precision_den else None,
        "false_negative_rate": fn / recall_den if recall_den else None,
        "no_public_catalyst_rate": counts["NO_PUBLIC_CATALYST"] / n if n else None,
        "unclear_rate": counts["UNCLEAR"] / n if n else None,
    }


def compact_driver(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    keys = ("hypothesis_id", "driver_id", "event_type", "direction", "scope", "related_symbols", "verification_status")
    return {key: value[key] for key in keys if key in value}


def main() -> None:
    run_dirs = sorted((ROOT / "output" / "evaluation_batch" / "runs").glob("2026-09-15_to_2026-09-28_integrity_*"), key=lambda p: p.stat().st_mtime)
    if not run_dirs:
        raise SystemExit("No integrity-scoped ten-day evaluation package found")
    run_dir = run_dirs[-1]
    package = json.loads((run_dir / "historical_batch_summary.json").read_text(encoding="utf-8"))
    market_input = read_csv(INPUT_DIR / "market_day_review_queue_manual_qa_v1.csv")
    stock_input = read_csv(INPUT_DIR / "stock_review_queue_manual_qa_v1.csv")
    days = {day["market_date"]: day for day in package["days"]}
    stock_review = {(r["date"], r["symbol"]): r for r in read_csv(run_dir / "stock_review_queue.csv")}
    market_rows: list[dict[str, Any]] = []
    stock_rows: list[dict[str, Any]] = []
    validation = {"duplicates": [], "invalid_labels": [], "unmatched_labels": [], "excluded_integrity": [], "excluded_not_eligible": []}

    def collect(source: list[dict[str, str]], is_market: bool) -> None:
        seen: set[tuple[str, str]] = set()
        label_field = "market_manual_label" if is_market else "manual_label"
        for row in source:
            date = row.get("date", "")
            symbol = "MARKET" if is_market else row.get("symbol", "")
            key = (date, symbol)
            if key in seen:
                validation["duplicates"].append({"date": date, "symbol": symbol, "source": "market" if is_market else "stock"})
                continue
            seen.add(key)
            day_integrity = (days.get(date, {}).get("integrity") or {}).get("integrity_status", "")
            row_integrity = row.get("integrity_status", "")
            integrity_failed = day_integrity == "FAIL" or row_integrity == "FAIL"
            if integrity_failed:
                validation["excluded_integrity"].append({"date": date, "symbol": symbol, "label": (row.get(label_field) or "").strip().upper(), "day_integrity": day_integrity, "row_integrity": row_integrity})
            label = (row.get(label_field) or "").strip().upper()
            if not label:
                continue
            if label not in ALLOWED_LABELS:
                validation["invalid_labels"].append({"date": date, "symbol": symbol, "label": label})
                continue
            if row.get("manual_label_eligible", "").lower() != "true":
                validation["excluded_not_eligible"].append({"date": date, "symbol": symbol, "label": label})
                continue
            # Exclude the entire date if the day-level integrity gate failed, even if a
            # downstream queue inherited a non-FAIL status.
            if integrity_failed:
                continue
            if date not in days or (not is_market and key not in stock_review):
                validation["unmatched_labels"].append({"date": date, "symbol": symbol, "label": label})
                continue
            enriched = dict(row)
            enriched.update({"date": date, "symbol": symbol, "manual_label": label, "event_type_bucket": event_type_for(row, is_market), "row_kind": "MARKET" if is_market else "STOCK"})
            if label in {"FALSE_POSITIVE", "FALSE_NEGATIVE"}:
                primary, secondary, rationale = ROOT_CAUSE_BY_CASE.get(key, ("OTHER", "", "No specific pipeline-stage evidence could be isolated from the current artifacts."))
                enriched.update({"primary_root_cause": primary, "secondary_root_cause": secondary, "stage": stage_for(primary), "trace_rationale": rationale})
            if is_market:
                market_rows.append(enriched)
            else:
                stock_rows.append(enriched)

    collect(market_input, True)
    collect(stock_input, False)
    if validation["duplicates"] or validation["invalid_labels"] or validation["unmatched_labels"]:
        raise SystemExit(f"Manual QA input validation failed: {json.dumps(validation, ensure_ascii=False)}")
    input_labeled_market = sum(bool((r.get("market_manual_label") or "").strip()) for r in market_input)
    input_labeled_stock = sum(bool((r.get("manual_label") or "").strip()) for r in stock_input)
    if input_labeled_market != len(market_rows) + sum(bool(x.get("label")) for x in validation["excluded_integrity"] if x.get("symbol") == "MARKET") + len(validation["excluded_not_eligible"]):
        raise SystemExit("Market QA label accounting mismatch")
    if input_labeled_stock != len(stock_rows) + sum(bool(x.get("label")) for x in validation["excluded_integrity"] if x.get("symbol") != "MARKET") + len([x for x in validation["excluded_not_eligible"] if x.get("symbol") != "MARKET"]):
        raise SystemExit("Stock QA label accounting mismatch")

    errors: list[dict[str, Any]] = []
    fn_trace: list[dict[str, Any]] = []
    fp_trace: list[dict[str, Any]] = []
    for row in market_rows + stock_rows:
        if row["manual_label"] not in {"FALSE_POSITIVE", "FALSE_NEGATIVE"}:
            continue
        date, symbol = row["date"], row["symbol"]
        day = days[date]
        market_metrics = day.get("market_candidate_metrics") or {}
        rec = next((r for r in day.get("stock_records", []) if r.get("symbol") == symbol), None) if symbol != "MARKET" else None
        if rec is None and symbol != "MARKET":
            raise SystemExit(f"Labeled stock row has no replay record: {date} {symbol}")
        root = row["primary_root_cause"]
        relevant: list[str] = []
        if rec:
            relevant = [str(c.get("title", "")) for c in rec.get("candidate_stories", []) if isinstance(c, dict) and c.get("title")]
        else:
            for stock in day.get("stock_records", []):
                for candidate in stock.get("candidate_stories", []):
                    if isinstance(candidate, dict) and candidate.get("title"):
                        title = str(candidate["title"])
                        if any(token in title.lower() for token in ("market today", "nasdaq 100", "oil slides", "treasury", "stocks fall", "stocks drop", "yields")):
                            relevant.append(title)
        driver_titles = []
        for driver in [day.get("main_market_driver"), *(day.get("sector_drivers") or [])]:
            if isinstance(driver, dict):
                driver_titles.extend(str(c.get("title", "")) for c in driver.get("candidate_stories", []) if isinstance(c, dict) and c.get("title"))
        candidates = rec.get("candidate_stories", []) if rec else []
        gate_counts = Counter(str(c.get("causal_reason", "UNKNOWN")) for c in candidates if isinstance(c, dict))
        if row["manual_label"] == "FALSE_NEGATIVE":
            if root in {"SECTOR_INHERITANCE_FALSE_NEGATIVE", "MARKET_INHERITANCE_FALSE_NEGATIVE"}:
                trace_class = "E_BROADER_DRIVER_BUILT_BUT_NOT_INHERITED"
            elif root == "SECTOR_PROMOTION_MISS":
                trace_class = "B_EVIDENCE_PRESENT_BUT_NOT_SELECTED" if relevant else "C_EVIDENCE_NOT_DISCOVERED"
            elif root == "ANALYST_ACTION_MISS":
                trace_class = "B_EVIDENCE_PRESENT_BUT_NOT_SELECTED"
            elif root == "EVENT_CLASSIFICATION_MISS":
                trace_class = "B_EVIDENCE_PRESENT_BUT_NOT_SELECTED"
            elif root == "STOCK_DISCOVERY_MISS":
                trace_class = "C_EVIDENCE_NOT_DISCOVERED"
            elif root == "MARKET_DISCOVERY_MISS":
                trace_class = "B_EVIDENCE_PRESENT_BUT_NOT_SELECTED" if relevant else "D_BROADER_DRIVER_NOT_BUILT"
            else:
                trace_class = "F_UNKNOWN"
            rec_driver_titles = relevant[:12]
            trace = {
                "date": date, "symbol": symbol, "manual_label": row["manual_label"],
                "move_pct": row.get("move_pct", ""), "primary_root_cause": root,
                "secondary_root_cause": row.get("secondary_root_cause", ""),
                "trace_category": trace_class, "pipeline_stage": row.get("stage"),
                "candidate_count": rec.get("candidate_count", "") if rec else market_metrics.get("raw_candidate_count", 0),
                "market_raw_candidates": market_metrics.get("raw_candidate_count", ""),
                "market_filtered_candidates": market_metrics.get("filtered_candidate_count", ""),
                "market_selected_candidates": market_metrics.get("selected_candidate_count", ""),
                "stock_candidate_gate_reasons": dict(gate_counts),
                "market_driver_built": bool(day.get("main_market_driver")),
                "sector_drivers_built": [x.get("driver_id") or x.get("sector_id") for x in day.get("sector_drivers", []) if isinstance(x, dict)],
                "selected_event": rec.get("selected_event", "") if rec else day.get("selected_market_story", ""),
                "selected_published_at": rec.get("selected_published_at", "") if rec else "",
                "candidate_headlines": rec_driver_titles,
                "evidence_assessment": row.get("trace_rationale", ""),
                "manual_qa_notes": row.get("review_notes", ""),
            }
            fn_trace.append(trace)
        else:
            trace = {
                "date": date, "symbol": symbol, "manual_label": row["manual_label"],
                "move_pct": row.get("move_pct", ""), "primary_root_cause": root,
                "secondary_root_cause": row.get("secondary_root_cause", ""),
                "pipeline_stage": row.get("stage"), "selected_explanation": rec.get("selected_event", "") if rec else day.get("selected_market_story", ""),
                "selected_published_at": rec.get("selected_published_at", "") if rec else "",
                "inherited_market_driver": compact_driver((rec or {}).get("inherited_market_driver", "")) if rec else compact_driver(day.get("main_market_driver")),
                "inherited_sector_driver": compact_driver((rec or {}).get("inherited_sector_driver", "")) if rec else "",
                "candidate_headlines": relevant[:12] if rec else driver_titles[:12],
                "causal_gate_reasons": dict(gate_counts), "evidence_assessment": row.get("trace_rationale", ""),
                "manual_qa_notes": row.get("review_notes", ""),
            }
            fp_trace.append(trace)
        errors.append({"date": date, "symbol": symbol, "row_kind": row["row_kind"], "manual_label": row["manual_label"], "move_pct": row.get("move_pct", ""), "event_type": row["event_type_bucket"], "primary_root_cause": root, "secondary_root_cause": row.get("secondary_root_cause", ""), "stage": row.get("stage"), "trace_category": trace_class if row["manual_label"] == "FALSE_NEGATIVE" else "", "rationale": row.get("trace_rationale", ""), "review_notes": row.get("review_notes", "")})

    all_labeled = market_rows + stock_rows
    by_kind = {kind: metrics([r for r in all_labeled if r["row_kind"] == kind]) for kind in ("MARKET", "STOCK")}
    by_kind["COMBINED"] = metrics(all_labeled)
    event_breakdown = []
    for event in EVENT_TYPES:
        subset = [r for r in all_labeled if r["event_type_bucket"] == event]
        event_breakdown.append({"event_type": event, "sample_size": len(subset), **metrics(subset)})
    stage_breakdown = []
    for stage in STAGES:
        subset = [e for e in errors if e["stage"] == stage]
        stage_breakdown.append({"stage": stage, "total_errors": len(subset), "false_positives": sum(e["manual_label"] == "FALSE_POSITIVE" for e in subset), "false_negatives": sum(e["manual_label"] == "FALSE_NEGATIVE" for e in subset), "examples": [f"{e['date']} {e['symbol']} ({e['primary_root_cause']})" for e in subset[:5]]})
    root_counts = Counter(e["primary_root_cause"] for e in errors)
    top_roots = [
        {"root_cause": "SECTOR_INHERITANCE_FALSE_NEGATIVE", "confirmed_errors": root_counts["SECTOR_INHERITANCE_FALSE_NEGATIVE"], "severity_and_breadth": "Seven stocks across three dates; 9/16 energy and 9/17/21 technology drivers were already built.", "current_behavior": "Directionally aligned sector drivers exist, but stock explanations remain UNEXPLAINED instead of inheriting the scoped driver.", "desired_behavior": "Apply an existing matching sector driver to eligible member stocks while retaining direction/scope safeguards.", "implementation_surface": "drivers/causal.py, evaluate_market_drivers_product.py (inheritance association and evaluator parity only).", "regression_risk": "Moderate: broader inheritance can recreate false positives if driver membership, scope, or direction is permissive.", "expected_improvement": "Recover up to 7 manually labeled false negatives in this sample; verify unchanged abstention for unrelated stocks."},
        {"root_cause": "MARKET_INHERITANCE_FALSE_POSITIVE", "confirmed_errors": root_counts["MARKET_INHERITANCE_FALSE_POSITIVE"], "severity_and_breadth": "Four stocks across two dates: TMO/QCOM on 9/15 and ABNB/BKNG on 9/23.", "current_behavior": "A broad or wrong-scope inherited explanation masks a stock/sector-specific mismatch.", "desired_behavior": "Do not use a market hypothesis outside its scope; prefer an available more-specific theme or abstain.", "implementation_surface": "drivers/causal.py, market/sector inheritance resolver and its evidence trace.", "regression_risk": "Moderate: stricter scope checks may increase unexplained outcomes where broad inheritance was appropriate.", "expected_improvement": "Remove up to 4 false-positive inherited explanations without changing discovery."},
        {"root_cause": "MARKET_DISCOVERY_MISS", "confirmed_errors": root_counts["MARKET_DISCOVERY_MISS"], "severity_and_breadth": "Three market dates (9/16, 9/17, 9/21), each potentially affecting many index constituents.", "current_behavior": "Market-level candidate set is empty although relevant same-day macro/rally headlines occur in symbol-news caches and structured market signals are available.", "desired_behavior": "Surface already-cached market-theme evidence into the market driver builder and report a transparent MISS when unavailable.", "implementation_surface": "news/live.py market discovery-to-driver input assembly; market driver candidate plumbing only.", "regression_risk": "Moderate: symbol-level recaps can be mistaken for broad-market catalysts unless market scope and timing remain required.", "expected_improvement": "Address 3 market false negatives and potentially improve broad attribution for many stocks on those dates."},
    ]
    market_excluded_fail = sum(r.get("integrity_status") == "FAIL" or (days.get(r.get("date", ""), {}).get("integrity") or {}).get("integrity_status") == "FAIL" for r in market_input)
    stock_excluded_fail = sum(r.get("integrity_status") == "FAIL" or (days.get(r.get("date", ""), {}).get("integrity") or {}).get("integrity_status") == "FAIL" for r in stock_input)
    info = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "input_files": [str(INPUT_DIR / "market_day_review_queue_manual_qa_v1.csv"), str(INPUT_DIR / "stock_review_queue_manual_qa_v1.csv")],
        "evaluation_package": str(run_dir), "dates": package.get("dates", []),
        "label_validation": {
            "allowed_taxonomy": sorted(ALLOWED_LABELS), "market_rows_labeled": len(market_rows), "stock_rows_labeled": len(stock_rows),
            "input_nonempty_labels": {"market": input_labeled_market, "stock": input_labeled_stock},
            "accepted_labels": {"market": len(market_rows), "stock": len(stock_rows)},
            "rows_excluded_for_integrity": {"market": market_excluded_fail, "stock": stock_excluded_fail},
            "rows_unlabeled_manual_eligible": {"market": sum(r.get("manual_label_eligible", "").lower() == "true" and not (r.get("market_manual_label") or "").strip() for r in market_input), "stock": sum(r.get("manual_label_eligible", "").lower() == "true" and not (r.get("manual_label") or "").strip() for r in stock_input)},
            "duplicates": validation["duplicates"], "invalid_labels": validation["invalid_labels"], "unmatched_labels": validation["unmatched_labels"],
            "excluded_not_eligible": validation["excluded_not_eligible"], "integrity_fail_rows_excluded": validation["excluded_integrity"],
            "integrity_warn_rows_included": sum(r.get("integrity_status") == "WARN" and (r.get("manual_label") or r.get("market_manual_label") or "").strip() in ALLOWED_LABELS for r in market_input + stock_input),
        },
        "metrics": by_kind, "event_type_metrics": event_breakdown,
        "error_totals": {"false_negatives": len(fn_trace), "false_positives": len(fp_trace)},
        "root_cause_counts": dict(root_counts), "stage_breakdown": stage_breakdown,
        "false_negative_traces": fn_trace, "false_positive_traces": fp_trace,
        "top_three_root_causes": top_roots,
        "limitations": ["Market/stock rows are scored only when manually labeled and eligible.", "NO_PUBLIC_CATALYST and UNCLEAR are excluded from FP/FN denominators.", "False-positive rate is reported as FP/(supported positive + FP), because the label set has no true-negative class.", "WARN integrity labels are retained; the date-level integrity FAIL is excluded.", "Event-type attribution is a single primary analysis bucket, inferred from QA notes and dated artifacts; categories with small samples are descriptive only."],
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "qa_summary.json").write_text(json.dumps(json_safe(info), ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(OUTPUT_DIR / "error_taxonomy.csv", errors, ["date", "symbol", "row_kind", "manual_label", "move_pct", "event_type", "primary_root_cause", "secondary_root_cause", "stage", "trace_category", "rationale", "review_notes"])
    write_csv(OUTPUT_DIR / "false_negative_trace.csv", fn_trace, list(fn_trace[0]) if fn_trace else ["date", "symbol", "manual_label"])
    write_csv(OUTPUT_DIR / "false_positive_trace.csv", fp_trace, list(fp_trace[0]) if fp_trace else ["date", "symbol", "manual_label"])
    write_csv(OUTPUT_DIR / "stage_breakdown.csv", stage_breakdown, ["stage", "total_errors", "false_positives", "false_negatives", "examples"])
    write_csv(OUTPUT_DIR / "event_type_breakdown.csv", event_breakdown, ["event_type", "sample_size", "label_counts", "positive_supported", "false_positives", "false_negatives", "precision_like", "recall_like", "false_positive_rate", "false_negative_rate", "no_public_catalyst_rate", "unclear_rate"])

    def pct(value: Any) -> str:
        return "n/a" if value is None else f"{value:.1%}"
    md = ["# Manual QA V1 — Error Taxonomy", "", f"Evaluation package: `{run_dir}`", "", "## Label validation", "", f"- Labeled market rows: {len(market_rows)}; stock rows: {len(stock_rows)}.", f"- Rows excluded for FAIL integrity: market {market_excluded_fail}, stock {stock_excluded_fail}; the 2026-09-22 market row had no manual label, so no labeled result was removed from the metrics.", f"- Unlabeled eligible rows: market {info['label_validation']['rows_unlabeled_manual_eligible']['market']}, stock {info['label_validation']['rows_unlabeled_manual_eligible']['stock']}.", f"- Duplicates: {len(validation['duplicates'])}; invalid labels: {len(validation['invalid_labels'])}; unmatched labels: {len(validation['unmatched_labels'])}.", "- Integrity-WARN labels are retained; the explicitly failed 2026-09-22 date is excluded.", "- All nonempty valid labels reconcile to accepted plus explicitly excluded labels.", "", "## Product quality", "", "| Scope | Labeled | Supported positive | FP | FN | Precision-like | Recall-like | FP rate* | FN rate | No public catalyst | Unclear |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for scope in ("MARKET", "STOCK", "COMBINED"):
        m = by_kind[scope]
        md.append(f"| {scope} | {m['labeled_rows']} | {m['positive_supported']} | {m['false_positives']} | {m['false_negatives']} | {pct(m['precision_like'])} | {pct(m['recall_like'])} | {pct(m['false_positive_rate'])} | {pct(m['false_negative_rate'])} | {pct(m['no_public_catalyst_rate'])} | {pct(m['unclear_rate'])} |")
    md += ["", "*FP rate is FP/(supported positive + FP), since manual labels contain no true-negative class. NO_PUBLIC_CATALYST and UNCLEAR are not counted as FN.", "", "## Error totals", "", f"- False negatives: {len(fn_trace)}.", f"- False positives: {len(fp_trace)}.", f"- Evidence was already present somewhere in cached evaluation candidates for {sum(x['trace_category'].startswith(('A_', 'B_', 'E_')) for x in fn_trace)}/{len(fn_trace)} false negatives; one case's exact launch was absent, and the remaining evidence was either rejected/not promoted or available only on the broader-driver path.", "", "## Root-cause ranking", ""]
    for i, item in enumerate(top_roots, 1):
        md += [f"{i}. **{item['root_cause']} — {item['confirmed_errors']} confirmed errors.** {item['severity_and_breadth']}", f"   Current: {item['current_behavior']}", f"   Desired: {item['desired_behavior']}", f"   Risk: {item['regression_risk']}", ""]
    md += ["A three-case `SECTOR_PROMOTION_MISS` cluster (LRCX/AMAT/KLAC, 2026-09-18) is the closest runner-up; it is narrower in date breadth than the three market-wide misses.", "", "## Pipeline stages", "", "| Stage | Errors | FP | FN | Examples |", "|---|---:|---:|---:|---|"]
    for item in stage_breakdown:
        md.append(f"| {item['stage']} | {item['total_errors']} | {item['false_positives']} | {item['false_negatives']} | {', '.join(item['examples'])} |")
    md += ["", "## Event / driver type breakdown", "", "See `event_type_breakdown.csv` for all requested categories, including zero-count categories and sample sizes. Treat small-n rates as descriptive only.", "", "## Next engineering task — exactly one", "", "Implement **Sector Driver Inheritance Association V1**: make already-built, directionally aligned sector drivers attach to eligible member stocks through explicit sector membership/scope, with an audit trace of why each inheritance passed or abstained. Do not expand discovery, relax causal gates, or add providers in this task.", "", "Acceptance against this fixed manual-QA sample: recover the seven sector-inheritance false negatives (COP/EOG 9/16; AMD/MU/ORCL 9/17; QCOM/LRCX 9/21) while keeping TMO/QCOM 9/15 and ABNB/BKNG 9/23 free of the reviewed wrong inherited explanations; no change to integrity gates; replay remains offline. This is the highest-impact next change because the qualifying drivers and related context are already present, it spans three dates/seven misses, and it can be measured directly without network/provider changes.", "", "## Artifacts", "", "- `qa_summary.json`", "- `error_taxonomy.csv`", "- `false_negative_trace.csv`", "- `false_positive_trace.csv`", "- `stage_breakdown.csv`", "- `event_type_breakdown.csv`", "- `improvement_plan.md`", ""]
    (OUTPUT_DIR / "qa_summary.md").write_text("\n".join(md), encoding="utf-8")
    plan = ["# Manual QA V1 — Improvement Plan", "", "Analysis only; no production logic was modified.", "", "## Top root cause 1 — Sector inheritance false negatives (7)", "", top_roots[0]["current_behavior"], "", "Minimal proposal: correct the association between existing sector drivers and explicitly classified member symbols; preserve direction, timing, point-in-time, and scope checks. Add an inheritance-decision trace (matched sector, driver direction, stock direction, pass/abstain reason).", "", "Likely modules: `drivers/causal.py`, `evaluate_market_drivers_product.py`, associated tests. Why: the evaluation artifacts already contain aligned energy/technology sector drivers, but the stock explanations do not inherit them.", "", "Expected cases: COP/EOG 9/16, AMD/MU/ORCL 9/17, QCOM/LRCX 9/21. Keep unchanged: TMO/QCOM 9/15 (energy driver scope mismatch), ABNB/BKNG 9/23 (generic risk-off must not conceal Muse/travel-specific evidence), and all integrity gates. Regression tests: assert the seven expected inherited explanations, assert the four protected false-positive cases do not receive the reviewed wrong driver, assert direction mismatches abstain, then replay offline. Network/provider usage: none.", "", "## Top root cause 2 — Wrong-scope market inheritance false positives (4)", "", top_roots[1]["current_behavior"], "", "Minimal proposal: enforce the existing hypothesis scope and related-symbol membership during inheritance; where a more-specific cached theme conflicts with the generic explanation, retain that evidence distinction and abstain from the generic attribution. Likely modules: driver inheritance resolver and replay parity tests. Why: current hypotheses can be inherited beyond their declared scope or obscure a more specific explanation.", "", "Expected cases: prevent the reviewed errors for TMO/QCOM 9/15 and ABNB/BKNG 9/23. Keep unchanged: directionally consistent and correctly scoped inherited controls, including 9/28 broad market explanations. Regression tests: out-of-scope energy driver cannot explain TMO/QCOM; generic risk-off does not supersede cached Muse/travel evidence; existing valid broad-driver inheritance remains stable. Network/provider usage: none.", "", "## Top root cause 3 — Market-level discovery misses (3 dates)", "", top_roots[2]["current_behavior"], "", "Minimal proposal: make eligible, already-cached broad-market headlines from symbol candidate streams visible to the existing market driver builder, without adding a provider or new request. Likely modules: market discovery-to-driver input assembly. Why: market candidate/driver slots were empty while same-date macro/rally evidence was present elsewhere in the cached replay data.", "", "Expected cases: 9/16, 9/17, 9/21 market false negatives. Keep unchanged: 9/15's reviewed wrong oil/energy primary framing and the correct 9/23/9/28 market-driver controls. Regression tests: cached headline is market-scoped, in-window, and directionally consistent before promotion; symbol-only stories do not become broad drivers; offline network counter remains zero. Network/provider usage: none if limited to existing caches; do not add a live discovery request in this task.", "", "## Other confirmed patterns", "", "- `SECTOR_PROMOTION_MISS` (3): LRCX/AMAT/KLAC 9/18 share same-day equipment-group evidence in symbol candidates, but no corresponding sector driver was built.", "- `NOISE_SELECTION_ERROR` (2): OXY 9/16 recap and META 9/24 analyst-price-target item outranked stronger reviewed explanations.", "- `ANALYST_ACTION_MISS` (1): HUM 9/25 Barclays upgrade/target coverage was present but not promoted.", "- `STOCK_DISCOVERY_MISS` (1): PANW 9/23 related AI-security material was cached, but the exact Unit 42 launch was absent from the replay candidate set.", "- `EVENT_CLASSIFICATION_MISS` (1): PANW 9/28 AI-security/analyst candidates were available but no event was selected.", "", "## One recommended next task", "", "Proceed only with **Sector Driver Inheritance Association V1**. Acceptance: improve the seven designated FN cases; preserve the four reviewed inheritance FP cases; keep all integrity protections and offline/no-network behavior unchanged. Do not combine it with discovery or event-selection changes.", ""]
    (OUTPUT_DIR / "improvement_plan.md").write_text("\n".join(plan), encoding="utf-8")
    print(json.dumps({"output_dir": str(OUTPUT_DIR), "market_labeled": len(market_rows), "stock_labeled": len(stock_rows), "false_negatives": len(fn_trace), "false_positives": len(fp_trace), "metrics": by_kind, "root_causes": dict(root_counts), "files": sorted(p.name for p in OUTPUT_DIR.iterdir())}, ensure_ascii=False, indent=2))


def stage_for(root: str) -> str:
    return {
        "MARKET_DISCOVERY_MISS": "DISCOVERY", "STOCK_DISCOVERY_MISS": "DISCOVERY",
        "SECTOR_THEME_DISCOVERY_MISS": "DISCOVERY", "MARKET_PROMOTION_MISS": "MARKET_DRIVER",
        "SECTOR_PROMOTION_MISS": "SECTOR_DRIVER", "EVENT_CLASSIFICATION_MISS": "CLASSIFICATION",
        "ANALYST_ACTION_MISS": "CLASSIFICATION", "CAUSAL_GATE_FALSE_REJECT": "CAUSAL_GATE",
        "CAUSAL_GATE_FALSE_ACCEPT": "CAUSAL_GATE", "VERIFICATION_MISS": "VERIFICATION",
        "MARKET_INHERITANCE_FALSE_POSITIVE": "INHERITANCE", "MARKET_INHERITANCE_FALSE_NEGATIVE": "INHERITANCE",
        "SECTOR_INHERITANCE_FALSE_POSITIVE": "INHERITANCE", "SECTOR_INHERITANCE_FALSE_NEGATIVE": "INHERITANCE",
        "TIMING_ERROR": "CLASSIFICATION", "SUBJECT_MATCH_ERROR": "CLASSIFICATION",
        "NOISE_SELECTION_ERROR": "SELECTION/RANKING",
    }.get(root, "SELECTION/RANKING")


if __name__ == "__main__":
    main()
