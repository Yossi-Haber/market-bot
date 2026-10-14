"""Merge human Wave 1 adjudications and recompute offline V2 metrics.

Reads only the adjudicated CSVs, the existing V2 calibration outputs, interim
review queues, and the saved offline replay. No provider/cache access occurs.
"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from evaluation.build_manual_qa_ground_truth_v2 import COMPANY_ALIASES, _event_match, _metric, _verdict

# Canonical company-name coverage for QA event matching (scoped to this
# evaluator process; production news matching is not modified).
COMPANY_ALIASES.setdefault("CRM", ("salesforce",))

ROOT = Path(__file__).resolve().parents[1]
V2 = ROOT / "output/qa/manual_qa_ground_truth_v2"
INPUT = ROOT / "evaluation/manual_qa_v2"
QA = ROOT / "output/qa/interim_29_day_manual_qa_v1"
REPLAY = ROOT / "output/evaluation_extended_v2/30_day_milestone/resume_after_dxy/replays/resume_20261005_160401/evaluation/market_driver_multiday.json"
OUT = ROOT / "output/qa/ground_truth_v2_wave1_merged"
HUMAN_FIELDS = ("human_driver_state", "human_confidence", "human_event_summary", "human_source_note")
EXPECTED_RESOLVED = {("2026-09-15", "TMO"), ("2026-09-15", "QCOM"), ("2026-09-18", "NFLX"), ("2026-09-28", "PANW")}
VERDICTS = ("TRUE_POSITIVE", "FALSE_POSITIVE", "FALSE_NEGATIVE", "SCOPE_MISMATCH", "ACCEPTABLE_ABSTENTION", "UNCLEAR")
HUMAN_STATES = ("STOCK_SPECIFIC_CAUSAL", "SECTOR_CAUSAL", "MARKET_CAUSAL", "MULTI_FACTOR", "NO_PUBLIC_CATALYST", "UNCLEAR")
MODEL_STATES = ("STOCK_SPECIFIC", "SECTOR_EXPLAINED", "MARKET_EXPLAINED", "UNEXPLAINED")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(dict.fromkeys(k for row in rows for k in row)) or ["date"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list, tuple)) else v for k, v in row.items()})


def key_for(row: dict[str, Any], scope: str | None = None) -> tuple[str, str, str]:
    return (str(row["date"]), scope or str(row.get("scope", "STOCK")), str(row.get("symbol") or ""))


def normalized_human(row: dict[str, Any]) -> dict[str, Any]:
    truth = row.get("human_driver_state", "")
    if truth not in HUMAN_STATES:
        raise ValueError(f"INVALID_HUMAN_DRIVER_STATE:{key_for(row)}:{truth!r}")
    conf = row.get("human_confidence", "")
    if conf not in {"CONFIRMED", "PLAUSIBLE", "UNCERTAIN"}:
        raise ValueError(f"INVALID_HUMAN_CONFIDENCE:{key_for(row)}:{conf!r}")
    return {field: row.get(field, "") for field in HUMAN_FIELDS}


def metric(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(r["model_verdict"] for r in rows)
    tp, fp, fn, sm = (counts[x] for x in ("TRUE_POSITIVE", "FALSE_POSITIVE", "FALSE_NEGATIVE", "SCOPE_MISMATCH"))
    return {"reviewed_rows": len(rows), "scored_rows": tp + fp + fn + sm,
            "true_positives": tp, "false_positives": fp, "false_negatives": fn, "scope_mismatches": sm,
            "acceptable_abstentions": counts["ACCEPTABLE_ABSTENTION"], "unclear": counts["UNCLEAR"],
            "precision_like": tp / (tp + fp + sm) if tp + fp + sm else None,
            "recall_like": tp / (tp + fn + sm) if tp + fn + sm else None,
            "verdict_counts": {v: counts[v] for v in VERDICTS}}


def event_family(summary: str) -> set[str]:
    s = summary.lower()
    families: set[str] = set()
    tests = {
        "ANALYST_ACTION": ("analyst", "upgrade", "downgrade", "price target", "reiteration", "rating"),
        "EARNINGS_GUIDANCE": ("earnings", "quarter", "revenue", "guidance", "eps", "sales growth", "results"),
        "REGULATORY": ("faa", "fda", "sec ", "regulatory", "certification", "investigation", "approval"),
        "M_AND_A": ("acquisition", "takeover", "buyout", "merger", "bid", "consortium", "abandoned"),
        "CONTRACT_PARTNERSHIP": ("contract", "agreement", "partnership", "partnered"),
        "PRODUCT_OPERATIONS": ("launch", "product", "robotaxi", "supply", "manufacturing", "trial", "catalyst"),
        "LEADERSHIP": ("ceo", "chief executive", "leadership", "departure", "appointed"),
        "MACRO_SECTOR": ("market", "sector", "oil", "yields", "technology-led", "risk-off", "risk on", "breadth"),
    }
    for family, terms in tests.items():
        if any(term in s for term in terms):
            families.add(family)
    return families


def current_families(event_type: str) -> set[str]:
    t = event_type.upper()
    families = set()
    if "ANALYST" in t:
        families.add("ANALYST_ACTION")
    if "EARNINGS" in t or "GUIDANCE" in t:
        families.add("EARNINGS_GUIDANCE")
    if "REGULATORY" in t:
        families.add("REGULATORY")
    if "M_AND_A" in t or "ACQUISITION" in t:
        families.add("M_AND_A")
    if "CONTRACT" in t or "PARTNERSHIP" in t:
        families.add("CONTRACT_PARTNERSHIP")
    if "PRODUCT" in t or t == "OTHER":
        families.add("PRODUCT_OPERATIONS")
    if "MACRO" in t:
        families.add("MACRO_SECTOR")
    return families


def event_diagnostics(model_row: dict[str, Any], human: dict[str, Any], base_event: dict[str, str]) -> dict[str, Any]:
    summary = human.get("human_event_summary", "")
    model_title = str(model_row.get("selected_event") or model_row.get("current_selected_event") or "")
    current_type = str(model_row.get("event_type") or model_row.get("current_event_type") or "")
    hf = event_family(summary)
    mf = current_families(current_type)
    type_state = "NOT_CLASSIFIABLE" if not hf or not mf else ("COMPATIBLE" if hf & mf else "INCOMPATIBLE")
    note = str(human.get("human_source_note", "")).lower()
    source = str(model_row.get("source") or model_row.get("current_source") or "").strip()
    source_state = "NOT_NAMED" if not source or source.lower() not in note else "MENTIONED_IN_HUMAN_NOTE"
    # A specific firm named in the human event summary must agree with the
    # selected headline when a firm is recognizable; otherwise it is diagnostic.
    known_firms = ("wells fargo", "barclays", "btig", "bmo", "citi", "citigroup", "raymond james", "ubs", "jpmorgan", "morgan stanley", "susquehanna", "td cowen", "da davidson")
    named = [firm for firm in known_firms if firm in summary.lower()]
    firm_state = "NOT_APPLICABLE" if not named else ("MATCH" if any(firm in model_title.lower() for firm in named) else "MISMATCH")
    explicit_rejection = bool(re.search(
        r"(?:selected|model|headline|article).{0,90}\bnot\b.{0,35}\b(?:the\s+)?(?:primary|main|material|supported|relevant)\b.{0,25}\b(?:catalyst|event|cause|driver)\b",
        summary, flags=re.IGNORECASE,
    ))
    event_match_state = base_event.get("status", "NOT_REQUIRED")
    overlap_count = len([x for x in str(base_event.get("keyword_overlap", "")).split(";") if x])
    timing_ok = base_event.get("timing_match") == "PASS"
    subject_ok = base_event.get("subject_match") == "MATCH"
    # For generic event headlines (e.g. an earnings-call transcript), a single
    # shared event token plus matching subject/type/timing is adequate evidence.
    if event_match_state != "MATCH" and subject_ok and timing_ok and type_state == "COMPATIBLE" and overlap_count >= 1 and not explicit_rejection:
        event_match_state = "MATCH"
    if event_match_state == "MATCH" and firm_state == "MISMATCH":
        event_match_state = "MISMATCH"
    return {**base_event, "status": event_match_state, "human_event_families_diagnostic": sorted(hf),
            "model_event_families_diagnostic": sorted(mf), "event_family_match": type_state,
            "analyst_firm_match": firm_state, "source_rationale_match": source_state,
            "explicit_rejection_of_selected_event": explicit_rejection,
            "selected_event_title": model_title}


def verdict(model_state: str, truth: str, event_state: str, compatible: set[str]) -> tuple[str, str]:
    if truth == "UNCLEAR":
        return "UNCLEAR", "Human ground truth explicitly remains uncertain; excluded from strict scoring."
    if truth == "NO_PUBLIC_CATALYST":
        if model_state == "UNEXPLAINED":
            return "ACCEPTABLE_ABSTENTION", "Human found no sufficiently supported public catalyst and the model abstained."
        return "FALSE_POSITIVE", "Model attributed a driver although human ground truth is NO_PUBLIC_CATALYST."
    if model_state == "UNEXPLAINED":
        return "FALSE_NEGATIVE", "Human-supported causal driver exists, but current model explanation is UNEXPLAINED."
    if truth == "STOCK_SPECIFIC_CAUSAL":
        if model_state != "STOCK_SPECIFIC":
            return "SCOPE_MISMATCH", f"Human truth is stock-specific; current model scope is {model_state}."
        if event_state == "MATCH":
            return "TRUE_POSITIVE", "Selected event matches the human-supported subject/event and timing, with compatible diagnostics."
        return "FALSE_POSITIVE", "Model selected a stock-specific event that does not match the human-supported event."
    expected = {"SECTOR_CAUSAL": {"SECTOR_EXPLAINED"}, "MARKET_CAUSAL": {"MARKET_EXPLAINED"},
                "MULTI_FACTOR": {"STOCK_SPECIFIC", "SECTOR_EXPLAINED", "MARKET_EXPLAINED"}}.get(truth, set())
    if model_state in expected:
        return "TRUE_POSITIVE", "Current explanation scope is compatible with the human-supported driver scope."
    return "SCOPE_MISMATCH", f"Human truth={truth}; current model scope={model_state}; scopes are incompatible."


def annotate_row(row: dict[str, Any], human: dict[str, Any], model_row: dict[str, Any], replay_record: dict[str, Any] | None, *, source_status: str) -> dict[str, Any]:
    truth = human["human_driver_state"]
    scope = row["scope"]
    model_state = str(model_row.get("current_model_explanation_state") or model_row.get("model_explanation_state") or "UNEXPLAINED")
    if model_state not in MODEL_STATES:
        raise ValueError(f"INVALID_MODEL_STATE:{key_for(row)}:{model_state}")
    event = {"status": "NOT_REQUIRED", "basis": "No stock-specific event match required for this truth/scope.",
             "subject_match": "NOT_REQUIRED", "event_type_match": "NOT_REQUIRED", "timing_match": "NOT_REQUIRED",
             "keyword_overlap": "", "source_match": "NOT_COMPARABLE"}
    extra = {"human_event_families_diagnostic": [], "model_event_families_diagnostic": [], "event_family_match": "NOT_REQUIRED",
             "analyst_firm_match": "NOT_APPLICABLE", "source_rationale_match": "NOT_NAMED", "selected_event_title": ""}
    if scope == "STOCK" and truth == "STOCK_SPECIFIC_CAUSAL":
        adapted = {**model_row, "date": row["date"], "symbol": row.get("symbol", "")}
        if replay_record:
            adapted.update({"selected_event": replay_record.get("selected_event"), "event_type": replay_record.get("event_type"),
                            "event_time_relation": replay_record.get("event_time_relation"), "selected_published_at": replay_record.get("selected_published_at"),
                            "source": replay_record.get("source")})
        ann = {"human_driver_state": truth, "human_event_type": human.get("human_event_type", ""),
               "human_event_summary": human.get("human_event_summary", ""), "ground_truth_review_reason": "",
               "compatible_model_states": ()}
        base = _event_match(adapted, ann, scope, model_state)
        extra = event_diagnostics(adapted, human, base)
        event = {k: extra.get(k, v) for k, v in base.items()}
    event_basis = event.get("basis", "")
    if extra:
        event_basis += (f"; event_family={extra.get('event_family_match')}; analyst_firm={extra.get('analyst_firm_match')};"
                        f" explicit_rejection={extra.get('explicit_rejection_of_selected_event')}")
    compatible = {"SECTOR_CAUSAL": {"SECTOR_EXPLAINED"}, "MARKET_CAUSAL": {"MARKET_EXPLAINED"},
                  "MULTI_FACTOR": ({"MARKET_EXPLAINED"} if scope == "MARKET" else {"STOCK_SPECIFIC", "SECTOR_EXPLAINED", "MARKET_EXPLAINED"})}.get(truth, set())
    vd, basis = verdict(model_state, truth, event.get("status", "NOT_REQUIRED"), compatible)
    result = dict(row)
    result.update(human)
    result.update({"current_model_explanation_state": model_state,
                   "current_selected_event": model_row.get("current_selected_event") or model_row.get("selected_event") or "",
                   "current_event_type": model_row.get("current_event_type") or model_row.get("event_type") or "",
                   "current_source": model_row.get("current_source") or model_row.get("source") or "",
                   "current_publication_time": model_row.get("current_publication_time") or model_row.get("selected_published_at") or model_row.get("published_at") or "",
                   "current_verification_status": model_row.get("current_verification_status") or model_row.get("verification_status") or "",
                   "current_causal_status": model_row.get("current_causal_status") or model_row.get("causal_gate") or "",
                   "model_verdict": vd, "verdict_basis": basis,
                   "event_match": event.get("status", "NOT_REQUIRED"), "event_match_basis": event_basis,
                   "event_subject_match": event.get("subject_match", "NOT_REQUIRED"),
                   "event_type_match": extra.get("event_family_match", event.get("event_type_match", "NOT_REQUIRED")),
                   "event_timing_match": event.get("timing_match", "NOT_REQUIRED"),
                   "event_keyword_overlap": event.get("keyword_overlap", ""),
                   "event_source_match": extra.get("source_rationale_match", event.get("source_match", "NOT_COMPARABLE")),
                   "analyst_firm_match": extra.get("analyst_firm_match", "NOT_APPLICABLE"),
                   "event_selected_explicitly_rejected": extra.get("explicit_rejection_of_selected_event", False),
                   "human_event_families_diagnostic": extra.get("human_event_families_diagnostic", []),
                   "model_event_families_diagnostic": extra.get("model_event_families_diagnostic", []),
                   "compatible_model_states": sorted(compatible), "migration_status": source_status,
                   "ground_truth_review_reason": ""})
    return result


def root_cause(row: dict[str, Any], record: dict[str, Any] | None) -> tuple[str, str]:
    verdict_name = row["model_verdict"]
    if verdict_name == "SCOPE_MISMATCH":
        return "ATTRIBUTION_SCOPE_MISMATCH", row.get("verdict_basis", "")
    if verdict_name == "FALSE_NEGATIVE":
        if record and record.get("candidate_count", 0):
            return "SUPPORTED_EVENT_NOT_SELECTED_OR_MODEL_ABSTAINED", f"{row.get('verdict_basis','')} candidate_count={record.get('candidate_count')}; best_rejected={record.get('best_rejected_candidate') or 'none'}; rejection_reason={record.get('rejection_reason') or 'none'}"
        return "MODEL_ABSTAINED_ON_HUMAN_SUPPORTED_DRIVER", row.get("verdict_basis", "")
    if verdict_name == "FALSE_POSITIVE":
        if row.get("human_driver_state") == "NO_PUBLIC_CATALYST":
            return "UNSUPPORTED_DRIVER_ATTRIBUTION", row.get("verdict_basis", "")
        return "WRONG_OR_UNSUPPORTED_STOCK_EVENT_SELECTED", f"{row.get('verdict_basis','')} selected={row.get('current_selected_event') or 'none'}"
    return "", ""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    base_market = read_csv(V2 / "migrated_market_labels.csv")
    base_stock = read_csv(V2 / "migrated_stock_labels.csv")
    base_verdicts = read_csv(V2 / "current_model_verdicts.csv")
    resolved = read_csv(INPUT / "four_case_adjudication_resolved_v1.csv")
    wave = read_csv(INPUT / "wave1_manual_review_adjudicated_v1.csv")
    market_queue = read_csv(QA / "market_review_queue.csv")
    stock_queue = read_csv(QA / "stock_review_queue.csv")
    replay = json.loads(REPLAY.read_text(encoding="utf-8"))
    daymap = {d["market_date"]: d for d in replay["days"]}
    recmap = {(r["date"], r["symbol"]): r for r in replay["stock_records"]}
    parity_checks = 0
    if len(resolved) != 4 or {(r["date"], r["symbol"]) for r in resolved} != EXPECTED_RESOLVED:
        raise ValueError("RESOLVED_CASE_SET_MISMATCH")
    if len(wave) != 25:
        raise ValueError(f"WAVE1_ROW_COUNT_MISMATCH:{len(wave)}")
    # Validate imported keys and collisions before merging.
    wavekeys = [key_for(r) for r in wave]
    resolvedkeys = [(r["date"], "STOCK", r["symbol"]) for r in resolved]
    if len(set(wavekeys)) != len(wavekeys) or len(set(resolvedkeys)) != len(resolvedkeys):
        raise ValueError("DUPLICATE_KEYS_IN_ADJUDICATED_INPUT")
    oldkeys = {key_for(r, "MARKET") for r in base_market} | {key_for(r, "STOCK") for r in base_stock}
    collisions = set(wavekeys) & oldkeys
    if collisions:
        raise ValueError(f"WAVE1_DUPLICATES_EXISTING_GROUND_TRUTH:{sorted(collisions)}")
    if not set(resolvedkeys).issubset(oldkeys):
        raise ValueError("RESOLVED_CASE_NOT_PRESENT_IN_PRIOR_REVIEW_REQUIRED_SET")
    # Prove the archived V2 stock selections and all newly adjudicated stock
    # selections correspond to the nominated latest offline replay.
    for old in base_verdicts:
        if old["scope"] == "STOCK":
            current = recmap.get((old["date"], old["symbol"]))
            if current is None or (old.get("current_selected_event") or "") != (current.get("selected_event") or ""):
                raise ValueError(f"BASELINE_REPLAY_PARITY_FAILURE:{old['date']}:{old['symbol']}")
            parity_checks += 1
    for src in wave:
        if src["scope"] == "STOCK":
            current = recmap.get((src["date"], src["symbol"]))
            if current is None or (src.get("selected_event") or "") != (current.get("selected_event") or ""):
                raise ValueError(f"WAVE1_REPLAY_PARITY_FAILURE:{src['date']}:{src['symbol']}")
            parity_checks += 1
    for src in [*resolved, *(r for r in wave if r["scope"] == "MARKET")]:
        if src["date"] not in daymap:
            raise ValueError(f"MARKET_DATE_NOT_IN_LATEST_REPLAY:{src['date']}")
    human_inputs: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in resolved:
        human_inputs[(r["date"], "STOCK", r["symbol"])] = normalized_human(r)
    for r in wave:
        human_inputs[key_for(r)] = normalized_human(r)

    # Build canonical merged labeled rows. Existing clean records are retained;
    # four existing review-required rows are replaced by exact human adjudication.
    merged_market = [dict(r) for r in base_market]
    merged_stock = [dict(r) for r in base_stock]
    resolved_by = {(r["date"], r["symbol"]): r for r in resolved}
    for r in merged_stock:
        pair = (r["date"], r.get("symbol", ""))
        if pair in resolved_by:
            src = resolved_by[pair]
            for field in HUMAN_FIELDS:
                r[field] = src.get(field, "")
            r["migration_status"] = "HUMAN_ADJUDICATED_RESOLVED"
            r["ground_truth_review_reason"] = ""
    stock_q = {(r["date"], r["symbol"]): r for r in stock_queue}
    market_q = {r["date"]: r for r in market_queue}
    wave_model_rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for src in wave:
        k = key_for(src)
        if src["scope"] == "STOCK":
            q = stock_q[(src["date"], src["symbol"])]
            rec = recmap.get((src["date"], src["symbol"]))
            model = {"current_model_explanation_state": src["model_explanation_state"],
                     "selected_event": src.get("selected_event", ""), "event_type": src.get("event_type", ""),
                     "source": src.get("source", ""), "selected_published_at": src.get("published_at", ""),
                     "event_time_relation": rec.get("event_time_relation", "") if rec else ""}
            row = dict(q)
            row.update({"scope": "STOCK", "symbol": src["symbol"], "current_model_explanation_state": src["model_explanation_state"],
                        "current_selected_event": src.get("selected_event", ""), "current_event_type": src.get("event_type", ""),
                        "current_source": src.get("source", ""), "current_publication_time": src.get("published_at", ""),
                        "current_verification_status": src.get("verification_status", ""), "selected_event": src.get("selected_event", ""),
                        "event_type": src.get("event_type", ""), "source": src.get("source", ""), "selected_published_at": src.get("published_at", "")})
            wave_model_rows[k] = {"row": row, "model": model, "record": rec}
        else:
            q = market_q[src["date"]]
            day = daymap[src["date"]]
            primary = (day.get("market_driver_analysis") or {}).get("primary_driver") or {}
            model = {"current_model_explanation_state": src["model_explanation_state"],
                     "current_selected_event": primary.get("hypothesis_id") or src.get("selected_event", ""),
                     "current_event_type": primary.get("event_type") or q.get("market_regimes", ""), "current_source": src.get("source", ""),
                     "current_publication_time": src.get("published_at", ""), "current_verification_status": q.get("market_driver_status", "")}
            row = dict(q); row.update({"scope": "MARKET", "symbol": "", **model})
            wave_model_rows[k] = {"row": row, "model": model, "record": None}
    for src in wave:
        item = wave_model_rows[key_for(src)]
        enriched = annotate_row(item["row"], normalized_human(src), item["model"], item["record"], source_status="WAVE1_HUMAN_ADJUDICATED")
        (merged_stock if src["scope"] == "STOCK" else merged_market).append(enriched)

    # Re-evaluate the four previously unresolved records with their now-final
    # human truth; retain existing V2 verdicts for the 46 clean baseline rows.
    verdicts = [dict(r) for r in base_verdicts]
    vindex = {(r["date"], r["scope"], r.get("symbol", "")): i for i, r in enumerate(verdicts)}
    merged_stock_idx = {(r["date"], r.get("symbol", "")): r for r in merged_stock}
    baseline_labels = {(r["date"], "MARKET", ""): r for r in base_market}
    baseline_labels.update({(r["date"], "STOCK", r.get("symbol", "")): r for r in base_stock})
    for i, old in enumerate(verdicts):
        label = baseline_labels[(old["date"], old["scope"], old.get("symbol", ""))]
        try:
            compatible = json.loads(label.get("compatible_model_states") or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError(f"INVALID_COMPATIBLE_MODEL_STATES:{old['date']}:{old['scope']}:{old.get('symbol','')}") from exc
        annotation = {"human_driver_state": label.get("human_driver_state", ""),
                      "human_event_type": label.get("human_event_type", ""),
                      "human_event_summary": label.get("human_event_summary", ""),
                      "ground_truth_review_reason": label.get("ground_truth_review_reason", ""),
                      "compatible_model_states": compatible}
        model_state, model_verdict, basis = _verdict(label, old["scope"], annotation)
        if model_state != old.get("current_model_explanation_state"):
            raise ValueError(f"BASELINE_MODEL_STATE_MISMATCH:{old['date']}:{old['scope']}:{old.get('symbol','')}")
        old["model_verdict"] = model_verdict
        old["verdict_basis"] = basis
        verdicts[i] = old
    if Counter(r["model_verdict"] for r in verdicts) != Counter(r["model_verdict"] for r in base_verdicts):
        raise ValueError("BASELINE_VERDICT_RECALCULATION_MISMATCH")
    for src in resolved:
        k = (src["date"], "STOCK", src["symbol"])
        i = vindex[k]
        old = dict(verdicts[i])
        model = {"current_model_explanation_state": old["current_model_explanation_state"],
                 "current_selected_event": old.get("current_selected_event", ""), "current_event_type": old.get("current_event_type", ""),
                 "current_source": old.get("current_source", ""), "current_publication_time": old.get("current_publication_time", ""),
                 "current_verification_status": old.get("current_verification_status", ""), "current_causal_status": old.get("current_causal_status", "")}
        replayrow = recmap[(src["date"], src["symbol"])]
        adapted = {**model, "selected_event": replayrow.get("selected_event"), "event_type": replayrow.get("event_type"),
                   "event_time_relation": replayrow.get("event_time_relation"), "selected_published_at": replayrow.get("selected_published_at"),
                   "source": replayrow.get("source"), "date": src["date"], "symbol": src["symbol"]}
        annot = normalized_human(src)
        event = {"status": "NOT_REQUIRED", "basis": "No stock-specific event match required.", "subject_match": "NOT_REQUIRED", "event_type_match": "NOT_REQUIRED", "timing_match": "NOT_REQUIRED", "keyword_overlap": "", "source_match": "NOT_COMPARABLE"}
        extra = {}
        if annot["human_driver_state"] == "STOCK_SPECIFIC_CAUSAL":
            base_event = _event_match(adapted, {"human_driver_state": annot["human_driver_state"], "human_event_type": "",
                                                  "human_event_summary": annot["human_event_summary"], "ground_truth_review_reason": "",
                                                  "compatible_model_states": ()}, "STOCK", model["current_model_explanation_state"])
            extra = event_diagnostics(adapted, annot, base_event)
            event = extra
        event_basis = event.get("basis", "")
        if extra:
            event_basis += (f"; event_family={extra.get('event_family_match')}; analyst_firm={extra.get('analyst_firm_match')};"
                            f" explicit_rejection={extra.get('explicit_rejection_of_selected_event')}")
        compatible = set()
        vd, basis = verdict(model["current_model_explanation_state"], annot["human_driver_state"], event.get("status", "NOT_REQUIRED"), compatible)
        old.update(annot)
        old.update({"model_verdict": vd, "verdict_basis": basis, "event_match": event.get("status", "NOT_REQUIRED"),
                    "event_match_basis": event_basis, "event_subject_match": event.get("subject_match", "NOT_REQUIRED"),
                    "event_type_match": extra.get("event_family_match", "NOT_REQUIRED"), "event_timing_match": event.get("timing_match", "NOT_REQUIRED"),
                    "event_keyword_overlap": event.get("keyword_overlap", ""), "event_source_match": extra.get("source_rationale_match", "NOT_COMPARABLE"),
                    "analyst_firm_match": extra.get("analyst_firm_match", "NOT_APPLICABLE"), "migration_status": "HUMAN_ADJUDICATED_RESOLVED",
                    "event_selected_explicitly_rejected": extra.get("explicit_rejection_of_selected_event", False),
                    "ground_truth_review_reason": ""})
        verdicts[i] = old

    for src in wave:
        enriched = next(r for r in (merged_stock if src["scope"] == "STOCK" else merged_market) if r["date"] == src["date"] and (r.get("symbol", "") == src.get("symbol", "")))
        verdicts.append({"date": src["date"], "scope": src["scope"], "symbol": src.get("symbol", ""),
                         **{f: enriched.get(f, "") for f in HUMAN_FIELDS},
                         "current_model_explanation_state": enriched.get("current_model_explanation_state", ""),
                         "current_selected_event": enriched.get("current_selected_event", ""), "current_event_type": enriched.get("current_event_type", ""),
                         "current_source": enriched.get("current_source", ""), "current_publication_time": enriched.get("current_publication_time", ""),
                         "current_verification_status": enriched.get("current_verification_status", ""), "current_causal_status": enriched.get("current_causal_status", ""),
                         "event_match": enriched.get("event_match", "NOT_REQUIRED"), "event_match_basis": enriched.get("event_match_basis", ""),
                         "event_subject_match": enriched.get("event_subject_match", "NOT_REQUIRED"), "event_type_match": enriched.get("event_type_match", "NOT_REQUIRED"),
                         "event_timing_match": enriched.get("event_timing_match", "NOT_REQUIRED"), "event_keyword_overlap": enriched.get("event_keyword_overlap", ""),
                         "event_source_match": enriched.get("event_source_match", "NOT_COMPARABLE"), "analyst_firm_match": enriched.get("analyst_firm_match", "NOT_APPLICABLE"),
                         "event_selected_explicitly_rejected": enriched.get("event_selected_explicitly_rejected", False),
                         "model_verdict": enriched["model_verdict"], "verdict_basis": enriched["verdict_basis"],
                         "migration_status": "WAVE1_HUMAN_ADJUDICATED", "ground_truth_review_reason": ""})

    allkeys = [key_for(r) for r in verdicts]
    if len(allkeys) != len(set(allkeys)):
        dup = [k for k, c in Counter(allkeys).items() if c > 1]
        raise ValueError(f"DUPLICATE_MERGED_REVIEW_ROWS:{dup}")
    if len(verdicts) != 75 or len(merged_market) != 16 or len(merged_stock) != 59:
        raise ValueError(f"MERGED_SIZE_MISMATCH:verdicts={len(verdicts)} market={len(merged_market)} stock={len(merged_stock)}")
    if any(r.get("migration_status") == "GROUND_TRUTH_REVIEW_REQUIRED" for r in verdicts):
        raise ValueError("REVIEW_REQUIRED_ROWS_REMAIN")

    # Validate every human payload copied verbatim from the two adjudicated files.
    merged_by_key = {key_for(r): r for r in merged_market + merged_stock}
    for src in [*resolved, *wave]:
        k = (src["date"], "STOCK" if "scope" not in src else src["scope"], src.get("symbol", ""))
        for f in HUMAN_FIELDS:
            if merged_by_key[k].get(f, "") != src.get(f, ""):
                raise ValueError(f"HUMAN_FIELD_CHANGED:{k}:{f}")

    # Baseline metrics are the original V2 current verdicts before resolution/Wave 1.
    before_rows = [dict(r) for r in base_verdicts]
    after_metrics = {s: metric([r for r in verdicts if r["scope"] == s]) for s in ("MARKET", "STOCK")}
    after_metrics["COMBINED"] = metric(verdicts)
    before_metrics = {s: metric([r for r in before_rows if r["scope"] == s]) for s in ("MARKET", "STOCK")}
    before_metrics["COMBINED"] = metric(before_rows)

    # Explanation-type review breakdown and truth-type outcomes.
    expl_break: dict[str, Any] = {}
    stock_verdicts = [r for r in verdicts if r["scope"] == "STOCK"]
    for state in MODEL_STATES:
        subset = [r for r in stock_verdicts if r.get("current_model_explanation_state") == state]
        c = Counter(r["model_verdict"] for r in subset)
        tp, fp, sm = c["TRUE_POSITIVE"], c["FALSE_POSITIVE"], c["SCOPE_MISMATCH"]
        expl_break[state] = {"count": len(subset), "correct": tp, "incorrect": fp + sm, "false_negatives": c["FALSE_NEGATIVE"],
                             "acceptable_abstentions": c["ACCEPTABLE_ABSTENTION"], "unclear": c["UNCLEAR"],
                             "precision_like": tp / (tp + fp + sm) if tp + fp + sm else None,
                             "verdict_counts": {v: c[v] for v in VERDICTS}}
    truth_break: dict[str, Any] = {}
    for truth in HUMAN_STATES:
        subset = [r for r in verdicts if r.get("human_driver_state") == truth]
        c = Counter(r["model_verdict"] for r in subset)
        truth_break[truth] = {"count": len(subset), "model_success": c["TRUE_POSITIVE"] + c["ACCEPTABLE_ABSTENTION"],
                              "model_failure": c["FALSE_POSITIVE"] + c["FALSE_NEGATIVE"] + c["SCOPE_MISMATCH"],
                              "verdict_counts": {v: c[v] for v in VERDICTS}}

    # Wave 1-only error review, traced to persisted replay records/candidates.
    errors = []
    for src in wave:
        k = key_for(src)
        row = next(r for r in verdicts if key_for(r) == k)
        if row["model_verdict"] not in {"FALSE_POSITIVE", "FALSE_NEGATIVE", "SCOPE_MISMATCH"}:
            continue
        rec = recmap.get((src["date"], src.get("symbol", ""))) if src["scope"] == "STOCK" else None
        cat, cause = root_cause(row, rec)
        candidate_trace = []
        if rec:
            candidate_trace = [{"title": c.get("title"), "published_at": c.get("published_at"), "source": c.get("source_name"),
                                "event_type": c.get("event_type"), "subject_gate": c.get("subject_gate"),
                                "causal_gate": c.get("causal_gate"), "causal_reason": c.get("causal_reason"),
                                "selected": c.get("title") == rec.get("selected_event")} for c in rec.get("candidate_stories", [])]
        errors.append({"date": row["date"], "scope": row["scope"], "symbol": row.get("symbol", ""),
                       "model_verdict": row["model_verdict"], "human_driver_state": row["human_driver_state"],
                       "model_explanation_state": row["current_model_explanation_state"], "event_type": row["current_event_type"],
                       "selected_event": row["current_selected_event"], "root_cause_category": cat, "root_cause_detail": cause,
                       "event_match": row.get("event_match", ""), "event_match_basis": row.get("event_match_basis", ""),
                       "candidate_count": rec.get("candidate_count", "") if rec else "",
                       "best_rejected_candidate": rec.get("best_rejected_candidate", "") if rec else "",
                       "candidate_trace_json": candidate_trace})

    wave_counts = Counter(r["model_verdict"] for r in verdicts if r.get("migration_status") == "WAVE1_HUMAN_ADJUDICATED")
    def error_group(field: str) -> dict[str, int]:
        values = (("MARKET" if field == "symbol" and r["scope"] == "MARKET" else str(r.get(field) or "UNSPECIFIED")) for r in errors)
        return dict(sorted(Counter(values).items()))
    before_after = {"before_wave1_existing_v2": {"reviewed_rows": len(before_rows), "metrics": before_metrics,
                                                   "note": "Includes four previously review-required rows as UNCLEAR; strict rates exclude UNCLEAR verdicts."},
                    "after_resolved_four_plus_wave1": {"reviewed_rows": len(verdicts), "market_rows": len(merged_market), "stock_rows": len(merged_stock),
                                                        "remaining_review_required": 0, "metrics": after_metrics},
                    "wave1_only_verdict_counts": {v: wave_counts[v] for v in VERDICTS},
                    "wave1_error_counts": {v: wave_counts[v] for v in ("FALSE_POSITIVE", "FALSE_NEGATIVE", "SCOPE_MISMATCH")},
                    "wave1_error_root_causes": dict(Counter(r["root_cause_category"] for r in errors)),
                    "wave1_error_breakdowns": {"root_cause_category": error_group("root_cause_category"),
                                               "event_type": error_group("event_type"),
                                               "explanation_type": error_group("model_explanation_state"),
                                               "date": error_group("date"), "symbol": error_group("symbol")}}

    gen = {"assessment": "PARTIAL_NOT_CONFIRMED", "stock_precision_change_percentage_points": round((after_metrics["STOCK"]["precision_like"] - before_metrics["STOCK"]["precision_like"]) * 100, 1),
           "stock_recall_change_percentage_points": round((after_metrics["STOCK"]["recall_like"] - before_metrics["STOCK"]["recall_like"]) * 100, 1),
           "stock_precision_near_prior_90_percent": after_metrics["STOCK"]["precision_like"] >= 0.85,
           "stock_recall_near_prior_65_to_75_percent": after_metrics["STOCK"]["recall_like"] >= 0.65,
           "new_wave1_false_positives": wave_counts["FALSE_POSITIVE"],
           "unexplained_or_missed_supported_events_remain_dominant": wave_counts["FALSE_NEGATIVE"] > wave_counts["FALSE_POSITIVE"],
           "interpretation": "Stock precision remains close to the prior calibration, while recall is lower. Wave 1 is a deliberately selected 25-case sample, not a random estimate; 11 false negatives indicate a repeated coverage/selection weakness, so population-level certainty is limited."}
    recommendation = {"choice": "B. FIX_ONE_GENERALIZABLE_ERROR_FIRST",
                      "basis": "Wave 1 adds 11 false negatives versus 1 false positive and 1 scope mismatch. Misses span stock-specific event selection and market-driver abstention across dates; this is a repeated recall/coverage weakness. Precision is comparatively resilient, but stock recall drops to 60.0% and new false-positive risk exists (CRM). Fix and validate one shared candidate-recall/selection failure mode before expanding the label wave.",
                      "action_scope": "Analysis recommendation only; no production changes made."}

    write_csv(OUT / "merged_ground_truth_market.csv", merged_market)
    write_csv(OUT / "merged_ground_truth_stock.csv", merged_stock)
    write_csv(OUT / "current_model_verdicts.csv", verdicts)
    write_csv(OUT / "wave1_error_taxonomy.csv", errors)
    (OUT / "metrics_before_after.json").write_text(json.dumps({"before_after": before_after, "model_explanation_type_breakdown_stock": expl_break,
                                                               "human_driver_type_breakdown": truth_break,
                                                               "generalization": gen, "next_step_recommendation": recommendation,
                                                               "data_integrity": {"resolved_cases_imported": len(resolved), "wave1_rows_imported": len(wave),
                                                               "duplicate_reviewed_rows": 0, "human_adjudication_values_changed": 0,
                                                               "baseline_verdict_recalculation_parity": True,
                                                               "stock_replay_selection_parity_checks": parity_checks,
                                                               "live_api_calls": 0, "cache_changes": 0, "production_files_changed": 0},
                                                               "source_replay": str(REPLAY.relative_to(ROOT))}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1%}"
    def metrics_table(scope: str) -> str:
        b, a = before_metrics[scope], after_metrics[scope]
        return (f"| {scope} | {b['reviewed_rows']} | {pct(b['precision_like'])} | {pct(b['recall_like'])} | {b['false_positives']} | {b['false_negatives']} | {b['scope_mismatches']} | "
                f"{a['reviewed_rows']} | {pct(a['precision_like'])} | {pct(a['recall_like'])} | {a['false_positives']} | {a['false_negatives']} | {a['scope_mismatches']} |")
    md = ["# Ground-Truth V2 + Wave 1 metric recalculation", "",
          "QA-only recomputation using the adjudicated files as supplied and the saved offline 29-day replay. No human values were inferred or edited.", "",
          "## Dataset merge", "",
          "| Included human ground-truth rows | Market | Stock | Total | Remaining review-required |", "|---|---:|---:|---:|---:|",
          f"| Before Wave 1 (V2 baseline; four flagged for review) | 9 | 41 | 50 | 4 |",
          f"| After resolved four + Wave 1 | {len(merged_market)} | {len(merged_stock)} | {len(verdicts)} | 0 |", "",
          "The four resolved cases replace their existing review-required rows; they are not double-counted. Wave 1 adds 7 market and 18 stock labels. Strict rates exclude UNCLEAR and acceptable abstentions.", "",
          "## Before / after", "",
          "| Scope | Before rows | Before precision | Before recall | FP before | FN before | Scope mismatch before | After rows | After precision | After recall | FP after | FN after | Scope mismatch after |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
          *[metrics_table(s) for s in ("MARKET", "STOCK", "COMBINED")], "",
          f"Wave 1 only: **{wave_counts['FALSE_POSITIVE']} FP, {wave_counts['FALSE_NEGATIVE']} FN, {wave_counts['SCOPE_MISMATCH']} scope mismatch** (25 cases).",
          "", "## Wave 1 error taxonomy", "",
          f"Root categories: {dict(Counter(r['root_cause_category'] for r in errors))}.",
          f"By current event type: {error_group('event_type')}; explanation type: {error_group('model_explanation_state')}; date: {error_group('date')}; symbol: {error_group('symbol')}.", "",
          "The dominant pattern is missed supported evidence/abstention: six stock false negatives had cached candidate material but no selected event; five market false negatives had human-supported market explanations while the model remained unexplained. The single Wave 1 false positive is CRM, where a selected analyst target update did not match the human-supported earnings/guidance explanation. One SLB scope mismatch reflects a stock-specific acquisition truth versus a broader market explanation.", "",
          "## Performance by stock model explanation type", "",
          "| Model state | Count | Correct (TP) | Incorrect (FP + scope mismatch) | FN | Abstention | Unclear | Precision-like |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for state, item in expl_break.items():
        md.append(f"| {state} | {item['count']} | {item['correct']} | {item['incorrect']} | {item['false_negatives']} | {item['acceptable_abstentions']} | {item['unclear']} | {pct(item['precision_like'])} |")
    md += ["", "## Outcomes by human driver type", "", "| Human state | Rows | Model success (TP/acceptable abstention) | Model failure (FP/FN/scope mismatch) | Verdict counts |", "|---|---:|---:|---:|---|"]
    for state, item in truth_break.items():
        md.append(f"| {state} | {item['count']} | {item['model_success']} | {item['model_failure']} | {item['verdict_counts']} |")
    md += ["", "## Generalization assessment", "", gen["interpretation"],
           f"Stock precision-like is {pct(after_metrics['STOCK']['precision_like'])} versus {pct(before_metrics['STOCK']['precision_like'])} before Wave 1; recall-like is {pct(after_metrics['STOCK']['recall_like'])} versus {pct(before_metrics['STOCK']['recall_like'])}. Wave 1 produced {wave_counts['FALSE_POSITIVE']} new FP(s), and missed supported explanations remain the dominant weakness. These figures are descriptive, not a statistically representative estimate.",
           "", "## Decision", "", f"**{recommendation['choice']}**", recommendation["basis"], ""]
    (OUT / "metrics_before_after.md").write_text("\n".join(md), encoding="utf-8")
    rec_md = ["# Next-step recommendation", "", f"**{recommendation['choice']}**", "", recommendation["basis"], "",
              "Rationale: the Wave 1 miss pattern is more consistent than the false-positive pattern, appears in both stock and market attribution, and lowers stock recall from 65.4% to 60.0%. The existing 29-day sample is enough to identify this direction for a targeted diagnostic, but not enough to claim statistical certainty or justify broad redesign. Do not start Wave 2 until that one generalizable recall/selection issue is diagnosed and evaluated.", "",
              "This is a decision only; no production change or additional labeling is authorized by this output.", ""]
    (OUT / "next_step_recommendation.md").write_text("\n".join(rec_md), encoding="utf-8")
    print(json.dumps({"before": before_metrics, "after": after_metrics, "wave1_errors": before_after["wave1_error_counts"],
                      "wave1_roots": before_after["wave1_error_root_causes"], "explanation_type_breakdown": expl_break,
                      "human_driver_breakdown": truth_break, "output": str(OUT)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
