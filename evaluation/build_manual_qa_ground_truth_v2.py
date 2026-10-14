"""Build a two-layer manual-ground-truth calibration package from existing QA.

This is offline QA tooling. It reads the interim 29-day review queues and the
unchanged manual-label source, but migrates only the existing 9 market + 41
stock reviewed rows. It never assigns labels to new rows or touches caches.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "output/qa/interim_29_day_manual_qa_v1"
OUT = ROOT / "output/qa/manual_qa_ground_truth_v2"

# These row-scoped annotations are grounded only in the corresponding existing
# human note. When the note does not identify a defensible driver/event, the
# entry deliberately requests review instead of filling in a plausible guess.
ANNOTATIONS: dict[tuple[str, str, str], dict[str, Any]] = {}


def _annotation(scope: str, date: str, symbol: str, state: str, confidence: str,
                event_type: str, summary: str, *, review: str = "",
                compatible_models: tuple[str, ...] = ()) -> None:
    ANNOTATIONS[(scope, date, symbol)] = {
        "human_driver_state": state,
        "human_confidence": confidence,
        "human_event_type": event_type,
        "human_event_summary": summary,
        "ground_truth_review_reason": review,
        "compatible_model_states": compatible_models,
    }


# Market labels: preserve human causal scope; old verdict labels are not used as
# ground truth. Multi-factor claims retain the actual rationale in summary.
_annotation("MARKET", "2026-09-15", "", "MULTI_FACTOR", "PLAUSIBLE", "MACRO_MULTI_FACTOR",
            "Oil spike and Treasury-yield pressure weighed on broad equities; selected oil-rally/energy framing was not the primary broad-market explanation.")
_annotation("MARKET", "2026-09-16", "", "MULTI_FACTOR", "PLAUSIBLE", "MACRO_MULTI_FACTOR",
            "Fed hike, weak breadth, and oil selloff materially shaped the session.", compatible_models=("MARKET_EXPLAINED",))
_annotation("MARKET", "2026-09-17", "", "MULTI_FACTOR", "PLAUSIBLE", "MARKET_SECTOR_THEME",
            "Broad tech-led risk-on rally alongside easing oil and Treasury yields.", compatible_models=("MARKET_EXPLAINED",))
_annotation("MARKET", "2026-09-18", "", "NO_PUBLIC_CATALYST", "UNCERTAIN", "",
            "No single broad-market catalyst was identified; indexes were mixed and strength was concentrated in semiconductor equipment.")
_annotation("MARKET", "2026-09-21", "", "MULTI_FACTOR", "PLAUSIBLE", "MARKET_SECTOR_THEME",
            "Broad AI-led risk-on move with Nasdaq/technology leadership.", compatible_models=("MARKET_EXPLAINED",))
_annotation("MARKET", "2026-09-23", "", "MARKET_CAUSAL", "CONFIRMED", "MACRO",
            "Rate-sensitive risk-off session associated with rising yields, weak breadth, and broad index declines.", compatible_models=("MARKET_EXPLAINED",))
_annotation("MARKET", "2026-09-24", "", "NO_PUBLIC_CATALYST", "UNCERTAIN", "",
            "Broad market was essentially flat; no single market-wide catalyst was identified.")
_annotation("MARKET", "2026-09-25", "", "NO_PUBLIC_CATALYST", "UNCERTAIN", "",
            "Moderately positive session without a clear single market-wide event; stock/sector dispersion dominated.")
_annotation("MARKET", "2026-09-28", "", "MARKET_CAUSAL", "CONFIRMED", "MACRO",
            "Higher oil prices and Treasury yields weighed on broad equities.", compatible_models=("MARKET_EXPLAINED",))

# Stock labels. These are human statements distilled from the exact existing
# notes. Any older FP/FN label remains only in legacy_model_relative_label.
_annotation("STOCK", "2026-09-15", "TMO", "", "UNCERTAIN", "", "",
            review="The note rejects the old broad-market explanation but does not establish what did explain TMO's rise or that no public catalyst existed.")
_annotation("STOCK", "2026-09-15", "QCOM", "", "UNCERTAIN", "", "",
            review="The note rejects the old broad-market explanation but does not establish an alternative human-supported cause or absence of public catalyst.")
_annotation("STOCK", "2026-09-16", "OXY", "SECTOR_CAUSAL", "PLAUSIBLE", "COMMODITY_SECTOR",
            "Energy-sector selloff and crude retreat were stronger explanations than the selected Q2 earnings recap.", compatible_models=("SECTOR_EXPLAINED",))
for _sym in ("COP", "EOG"):
    _annotation("STOCK", "2026-09-16", _sym, "SECTOR_CAUSAL", "PLAUSIBLE", "COMMODITY_SECTOR",
                "Oil producers fell together as crude retreated.", compatible_models=("SECTOR_EXPLAINED",))
_annotation("STOCK", "2026-09-16", "IBM", "UNCLEAR", "UNCERTAIN", "", "")
_annotation("STOCK", "2026-09-17", "AMD", "MULTI_FACTOR", "PLAUSIBLE", "MARKET_SECTOR_THEME",
            "AMD participated in the broad technology/AI risk-on rally.", compatible_models=("SECTOR_EXPLAINED", "MARKET_EXPLAINED"))
_annotation("STOCK", "2026-09-17", "MU", "MULTI_FACTOR", "PLAUSIBLE", "MARKET_SECTOR_THEME",
            "Semiconductor strength participated in a broad technology-led rally.", compatible_models=("SECTOR_EXPLAINED", "MARKET_EXPLAINED"))
_annotation("STOCK", "2026-09-17", "ORCL", "MULTI_FACTOR", "PLAUSIBLE", "MARKET_SECTOR_THEME",
            "Oracle's move occurred on a broad technology-led risk-on day.", compatible_models=("SECTOR_EXPLAINED", "MARKET_EXPLAINED"))
_annotation("STOCK", "2026-09-17", "MRVL", "STOCK_SPECIFIC_CAUSAL", "PLAUSIBLE", "CONTRACT",
            "GlobalFoundries and Marvell announced a multi-year capacity agreement for silicon-germanium optical-connectivity technology.", compatible_models=("STOCK_SPECIFIC",))
for _sym in ("LRCX", "AMAT", "KLAC"):
    _summary = "Semiconductor-equipment group rallied after a bullish JPMorgan wafer-fab-equipment/AI-capex note."
    if _sym == "KLAC":
        _summary += " KLA was highlighted as a preferred name."
    _annotation("STOCK", "2026-09-18", _sym, "SECTOR_CAUSAL", "PLAUSIBLE", "ANALYST_ACTION_SECTOR_THEME",
                _summary, compatible_models=("SECTOR_EXPLAINED",))
for _sym, _summary in (
    ("QCOM", "No sufficiently strong same-day causal event was established; the move followed prior strength."),
    ("GM", "No sufficiently strong same-day causal event was established."),
    ("ACN", "Contemporaneous coverage confirmed the move but did not establish a concrete same-day catalyst."),
    ("NFLX", "The original review was uncertain about a sufficiently specific same-day public catalyst."),
):
    _annotation("STOCK", "2026-09-18", _sym, "UNCLEAR", "UNCERTAIN", "", _summary,
                review=("The latest current replay now selects a direction-compatible Wells Fargo downgrade, while the prior manual note was UNCLEAR; a human must adjudicate this changed evidence." if _sym == "NFLX" else ""))
_annotation("STOCK", "2026-09-21", "META", "MULTI_FACTOR", "CONFIRMED", "ANALYST_ACTION_PRODUCT",
            "Wells Fargo price-target increase plus contemporaneous Muse momentum were directly cited with the jump.", compatible_models=("STOCK_SPECIFIC", "MARKET_EXPLAINED", "SECTOR_EXPLAINED"))
_annotation("STOCK", "2026-09-21", "AMD", "MULTI_FACTOR", "PLAUSIBLE", "PRODUCT_SECTOR_THEME",
            "AI-chip demand was directly relevant, alongside the broader AI/semiconductor rally.", compatible_models=("STOCK_SPECIFIC", "SECTOR_EXPLAINED", "MARKET_EXPLAINED"))
_annotation("STOCK", "2026-09-21", "QCOM", "MULTI_FACTOR", "PLAUSIBLE", "MARKET_SECTOR_THEME",
            "Large semiconductor/AI-linked move occurred on a broad AI-rally day.", compatible_models=("SECTOR_EXPLAINED", "MARKET_EXPLAINED"))
_annotation("STOCK", "2026-09-21", "MRVL", "STOCK_SPECIFIC_CAUSAL", "PLAUSIBLE", "ANALYST_ACTION",
            "Morgan Stanley maintained Equal-Weight and raised its Marvell price target to $268.", compatible_models=("STOCK_SPECIFIC",))
_annotation("STOCK", "2026-09-21", "LRCX", "MULTI_FACTOR", "PLAUSIBLE", "MARKET_SECTOR_THEME",
            "Semiconductor equipment remained part of the AI/capex theme during a large move.", compatible_models=("SECTOR_EXPLAINED", "MARKET_EXPLAINED"))
_annotation("STOCK", "2026-09-21", "UPS", "UNCLEAR", "UNCERTAIN", "",
            "The decline was confirmed, but no sufficiently specific public catalyst was established.")
for _sym, _summary in (
    ("ABNB", "Travel stocks were affected by Meta Muse disruption fears."),
    ("BKNG", "Travel stocks were affected by Meta Muse/Expedia disintermediation fears."),
):
    _annotation("STOCK", "2026-09-23", _sym, "SECTOR_CAUSAL", "PLAUSIBLE", "PRODUCT_SECTOR_THEME",
                _summary, compatible_models=("SECTOR_EXPLAINED",))
_annotation("STOCK", "2026-09-23", "PANW", "STOCK_SPECIFIC_CAUSAL", "CONFIRMED", "PRODUCT",
            "Palo Alto Networks launched Unit 42 Continuous Frontier AI Defense.", compatible_models=("STOCK_SPECIFIC",))
_annotation("STOCK", "2026-09-23", "MCD", "STOCK_SPECIFIC_CAUSAL", "PLAUSIBLE", "INVESTOR_DAY",
            "McDonald's Investor Day/NEXT strategy was the relevant event; the review attributes the negative reaction to investment and near-term economics.", compatible_models=("STOCK_SPECIFIC",))
_annotation("STOCK", "2026-09-24", "META", "STOCK_SPECIFIC_CAUSAL", "PLAUSIBLE", "PRODUCT",
            "Contemporaneous Meta Charm/Muse product catalyst that external reporting tied to the move.", compatible_models=("STOCK_SPECIFIC",))
for _sym in ("CDNS", "FDX"):
    _annotation("STOCK", "2026-09-24", _sym, "NO_PUBLIC_CATALYST", "UNCERTAIN", "",
                "No stronger same-day public catalyst was established.")
_annotation("STOCK", "2026-09-25", "MCHP", "UNCLEAR", "UNCERTAIN", "PRODUCT",
            "A prior-day power-monitor launch may have contributed, but evidence was not strong enough to call it the clear driver.")
_annotation("STOCK", "2026-09-25", "CVS", "NO_PUBLIC_CATALYST", "UNCERTAIN", "",
            "No convincing same-day public catalyst was established.")
_annotation("STOCK", "2026-09-25", "PYPL", "STOCK_SPECIFIC_CAUSAL", "PLAUSIBLE", "M_AND_A_RUMOR",
            "Renewed PayPal takeover rumors were a plausible contemporaneous contributor, but remained unverified.", compatible_models=("STOCK_SPECIFIC",))
_annotation("STOCK", "2026-09-25", "HUM", "STOCK_SPECIFIC_CAUSAL", "PLAUSIBLE", "ANALYST_ACTION",
            "Barclays upgraded Humana to Overweight and raised its target to $515; contemporaneous coverage tied the jump to the upgrade.", compatible_models=("STOCK_SPECIFIC",))
_annotation("STOCK", "2026-09-25", "BKNG", "NO_PUBLIC_CATALYST", "UNCERTAIN", "",
            "A rebound followed the prior Muse-related decline, but no distinct same-day public catalyst was established.")
for _sym in ("DASH", "QCOM", "META", "REGN"):
    _summary = {
        "DASH": "Broad risk-off and AI-disruption concerns were plausible contributors; attribution was not definitive.",
        "QCOM": "Broader technology/rate pressure plausibly contributed; no stronger stock-specific catalyst was established.",
        "META": "Broader technology/rate pressure plausibly contributed; no clear stock-specific catalyst was established.",
        "REGN": "Broad risk-off/yield pressure plausibly contributed, without a strongly stock-specific explanation.",
    }[_sym]
    _annotation("STOCK", "2026-09-28", _sym, "MARKET_CAUSAL", "PLAUSIBLE", "MACRO",
                _summary, compatible_models=("MARKET_EXPLAINED",))
_annotation("STOCK", "2026-09-28", "BA", "STOCK_SPECIFIC_CAUSAL", "CONFIRMED", "REGULATORY",
            "FAA/737 MAX 10 certification-delay story was concrete, contemporaneous, and directly consistent with the decline.", compatible_models=("STOCK_SPECIFIC",))
_annotation("STOCK", "2026-09-28", "PANW", "STOCK_SPECIFIC_CAUSAL", "PLAUSIBLE", "ANALYST_ACTION_PRODUCT",
            "Same-day analyst/AI-security catalysts existed, but the prior note does not identify which event was the supported cause.",
            review="The current replay selects a BTIG target-raise story, but the human note does not identify BTIG or establish that this exact event was the supported catalyst.", compatible_models=("STOCK_SPECIFIC",))

REVIEW_STATUSES = {"GROUND_TRUTH_REVIEW_REQUIRED"}
LEGACY_LABELS = {"CORRECT_CAUSAL", "PLAUSIBLE_CAUSAL", "FALSE_POSITIVE", "FALSE_NEGATIVE", "NO_PUBLIC_CATALYST", "UNCLEAR"}
STOPWORDS = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "is", "it", "of", "on", "or", "the", "to", "was", "were", "with", "this", "that", "same", "day", "move", "stock", "shares", "company", "event", "catalyst", "cause", "causal", "current", "selected", "story", "review", "note", "notes", "after", "during", "rise", "fell", "large", "strong", "broad", "specific", "public", "reported", "directly", "plausibly", "plausible", "contemporaneous", "concrete", "price", "target", "raised", "raise", "increase", "increased"}
COMPANY_ALIASES = {
    "TMO": ("thermo fisher",), "QCOM": ("qualcomm",), "OXY": ("occidental",),
    "COP": ("conocophillips",), "EOG": ("eog resources",), "AMD": ("advanced micro devices",),
    "MU": ("micron",), "ORCL": ("oracle",), "MRVL": ("marvell",), "LRCX": ("lam research",),
    "AMAT": ("applied materials",), "KLAC": ("kla", "kla corporation"), "GM": ("general motors",),
    "ACN": ("accenture",), "NFLX": ("netflix",), "META": ("meta",), "UPS": ("united parcel",),
    "ABNB": ("airbnb",), "BKNG": ("booking holdings", "booking.com"), "PANW": ("palo alto networks",),
    "MCD": ("mcdonald",), "CDNS": ("cadence",), "FDX": ("fedex",), "MCHP": ("microchip",),
    "CVS": ("cvs",), "PYPL": ("paypal",), "HUM": ("humana",), "BA": ("boeing",),
    "REGN": ("regeneron",), "DASH": ("doordash",), "IBM": ("ibm",),
}
EVENT_PHRASES = ("investor day", "price target", "takeover rumors", "takeover rumor", "unit 42", "max 10", "capacity agreement", "wfe", "ai capex", "muse", "charm", "downgrade", "upgrade", "launch", "disintermediation", "crude retreat", "oil selloff")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row)) or ["date"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict, tuple)) else value for key, value in row.items()})


def _tokens(value: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[A-Za-z][A-Za-z0-9'-]*", value) if token.lower() not in STOPWORDS and len(token) > 2}


def _model_state(row: dict[str, str], scope: str) -> str:
    raw = row.get("explanation_source") or row.get("current_final_explanation") or row.get("explanation_status") or ""
    if scope == "MARKET":
        return "MARKET_EXPLAINED" if raw == "EXPLAINED" or row.get("primary_market_driver") else "UNEXPLAINED"
    if raw in {"STOCK_SPECIFIC", "SECTOR_EXPLAINED", "MARKET_EXPLAINED", "UNEXPLAINED"}:
        return raw
    return "UNEXPLAINED"


def _event_match(row: dict[str, str], annotation: dict[str, Any], scope: str, model_state: str) -> dict[str, str]:
    if scope != "STOCK" or annotation["human_driver_state"] != "STOCK_SPECIFIC_CAUSAL":
        return {"status": "NOT_REQUIRED", "basis": "No single stock-specific event comparison required by this ground-truth scope.", "subject_match": "NOT_REQUIRED", "event_type_match": "NOT_REQUIRED", "timing_match": "NOT_REQUIRED", "keyword_overlap": "", "source_match": "NOT_COMPARABLE"}
    if model_state != "STOCK_SPECIFIC":
        return {"status": "NOT_APPLICABLE_SCOPE", "basis": "Current model did not select a stock-specific event.", "subject_match": "NOT_APPLICABLE", "event_type_match": "NOT_APPLICABLE", "timing_match": "NOT_APPLICABLE", "keyword_overlap": "", "source_match": "NOT_COMPARABLE"}
    selected = row.get("selected_event", "").strip()
    if not selected:
        return {"status": "NO_SELECTED_EVENT", "basis": "Current model state has no selected event title.", "subject_match": "NOT_AVAILABLE", "event_type_match": "NOT_AVAILABLE", "timing_match": "NOT_AVAILABLE", "keyword_overlap": "", "source_match": "NOT_COMPARABLE"}
    human_words = _tokens(annotation["human_event_summary"])
    selected_words = _tokens(selected)
    overlap = sorted(human_words & selected_words)
    symbol = row.get("symbol", "").upper()
    subject_match = bool(re.search(rf"(?<![A-Z0-9]){re.escape(symbol)}(?![A-Z0-9])", selected.upper())) if symbol else False
    if not subject_match:
        subject_match = any(alias.lower() in selected.lower() for alias in COMPANY_ALIASES.get(symbol, ()))
    phrase_overlap = [phrase for phrase in EVENT_PHRASES if phrase in annotation["human_event_summary"].lower() and phrase in selected.lower()]
    keyword_match = len(overlap) >= 2 or bool(phrase_overlap)
    relation = str(row.get("event_time_relation", "")).upper()
    publication_date = str(row.get("publication_time") or row.get("selected_published_at") or "")[:10]
    if "AFTER" in relation or "FUTURE" in relation or "POST_SESSION" in relation or (publication_date and publication_date > row.get("date", "")):
        timing = "FAIL_FUTURE_OR_AFTER_SESSION"
    elif "BEFORE" in relation or "DURING" in relation or (publication_date and publication_date <= row.get("date", "")):
        timing = "PASS"
    else:
        timing = "UNKNOWN"
    human_types = set(re.findall(r"ANALYST_ACTION|PRODUCT|REGULATORY|CONTRACT|EARNINGS|GUIDANCE|INVESTOR_DAY|M_AND_A|MACRO|SECTOR_THEME", annotation["human_event_type"].upper()))
    current_type = str(row.get("event_type", "")).upper()
    current_types = set(re.findall(r"ANALYST_ACTION|PRODUCT|REGULATORY|CONTRACT|EARNINGS|GUIDANCE|INVESTOR_DAY|M_AND_A|MACRO|SECTOR_THEME", current_type))
    type_match = "NOT_AVAILABLE" if not human_types or not current_types else ("MATCH" if human_types & current_types else "MISMATCH")
    matched = subject_match and keyword_match and timing != "FAIL_FUTURE_OR_AFTER_SESSION"
    status = "MATCH" if matched else "MISMATCH"
    basis = f"subject={'MATCH' if subject_match else 'MISMATCH'}; keyword_overlap={','.join(overlap) or 'none'}; phrase_overlap={','.join(phrase_overlap) or 'none'}; timing={timing}; event_type={type_match}."
    return {"status": status, "basis": basis, "subject_match": "MATCH" if subject_match else "MISMATCH", "event_type_match": type_match, "timing_match": timing, "keyword_overlap": ";".join(overlap), "source_match": "NOT_COMPARABLE_MANUAL_NOTE_DOES_NOT_NAME_SOURCE"}


def _verdict(row: dict[str, str], scope: str, annotation: dict[str, Any]) -> tuple[str, str, str]:
    model = _model_state(row, scope)
    truth = annotation["human_driver_state"]
    if annotation["ground_truth_review_reason"]:
        return model, "UNCLEAR", annotation["ground_truth_review_reason"]
    if truth == "UNCLEAR":
        return model, "UNCLEAR", "Human ground truth is explicitly uncertain; not scored."
    if truth == "NO_PUBLIC_CATALYST":
        verdict = "ACCEPTABLE_ABSTENTION" if model == "UNEXPLAINED" else "FALSE_POSITIVE"
        return model, verdict, "No-public-catalyst truth compared with whether the current model abstained."
    if model == "UNEXPLAINED":
        return model, "FALSE_NEGATIVE", "Human-supported causal scope exists, but the current model is unexplained."
    if truth == "STOCK_SPECIFIC_CAUSAL":
        event = _event_match(row, annotation, scope, model)
        if model != "STOCK_SPECIFIC":
            return model, "SCOPE_MISMATCH", "Human truth is stock-specific but current model used a broader scope."
        if event["status"] == "MATCH":
            return model, "TRUE_POSITIVE", event["basis"]
        return model, "FALSE_POSITIVE", "Current selected stock event does not match the human-supported event. " + event["basis"]
    compatible = annotation.get("compatible_model_states", ())
    if model in compatible:
        return model, "TRUE_POSITIVE", "Current explanation scope is compatible with the human-supported driver scope."
    if model != "UNEXPLAINED":
        return model, "SCOPE_MISMATCH", f"Human truth={truth}; current model scope={model}; no compatible scope/event match."
    return model, "FALSE_NEGATIVE", "Human-supported causal scope exists, but the current model is unexplained."


def _migrate(row: dict[str, str], scope: str) -> dict[str, Any]:
    symbol = row.get("symbol", "") if scope == "STOCK" else ""
    key = (scope, row["date"], symbol)
    annotation = ANNOTATIONS.get(key)
    if annotation is None:
        raise ValueError(f"NO_GROUND_TRUTH_MIGRATION_RULE:{key}")
    old_label = row.get("market_manual_label", row.get("manual_label", ""))
    if old_label not in LEGACY_LABELS:
        raise ValueError(f"INVALID_LEGACY_LABEL:{key}:{old_label}")
    model_state, verdict, basis = _verdict(row, scope, annotation)
    output = dict(row)
    # Keep the old judgment only under an explicit historical/model-relative
    # name; do not leave a misleading timeless `manual_label` field behind.
    output.pop("manual_label", None)
    output.pop("market_manual_label", None)
    output.update({
        "legacy_model_relative_label": old_label,
        "human_driver_state": annotation["human_driver_state"],
        "human_confidence": annotation["human_confidence"],
        "human_event_type": annotation["human_event_type"],
        "human_event_summary": annotation["human_event_summary"],
        "human_source_note": row.get("manual_note", row.get("review_notes", "")),
        "migration_status": "GROUND_TRUTH_REVIEW_REQUIRED" if annotation["ground_truth_review_reason"] else "MIGRATED",
        "ground_truth_review_reason": annotation["ground_truth_review_reason"],
        "current_model_explanation_state": model_state,
        "current_selected_event": row.get("selected_event") or row.get("selected_evidence") or row.get("selected_market_story", ""),
        "current_event_type": row.get("event_type", ""),
        "current_source": row.get("source") or row.get("selected_market_story_source", ""),
        "current_publication_time": row.get("publication_time") or row.get("evidence_timestamp", ""),
        "current_verification_status": row.get("verification_status", ""),
        "current_causal_status": row.get("causal_gate") or row.get("causal_status", ""),
        "model_verdict": verdict,
        "verdict_basis": basis,
    })
    if scope == "STOCK" and annotation["human_driver_state"] == "STOCK_SPECIFIC_CAUSAL":
        event = _event_match(row, annotation, scope, model_state)
    else:
        event = {"status": "NOT_REQUIRED", "basis": "", "subject_match": "NOT_REQUIRED", "event_type_match": "NOT_REQUIRED", "timing_match": "NOT_REQUIRED", "keyword_overlap": "", "source_match": "NOT_COMPARABLE"}
    output["event_match"] = event["status"]
    output["event_match_basis"] = event["basis"]
    output["event_subject_match"] = event["subject_match"]
    output["event_type_match"] = event["event_type_match"]
    output["event_timing_match"] = event["timing_match"]
    output["event_keyword_overlap"] = event["keyword_overlap"]
    output["event_source_match"] = event["source_match"]
    output["compatible_model_states"] = list(annotation.get("compatible_model_states", ()))
    return output


def _metric(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(row["model_verdict"] for row in rows)
    tp, fp, fn, mismatch = (counts[key] for key in ("TRUE_POSITIVE", "FALSE_POSITIVE", "FALSE_NEGATIVE", "SCOPE_MISMATCH"))
    return {
        "scored_rows": tp + fp + fn + mismatch,
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "scope_mismatches": mismatch,
        "acceptable_abstentions": counts["ACCEPTABLE_ABSTENTION"],
        "unclear_or_review_required": sum(
            row["model_verdict"] == "UNCLEAR" or row.get("migration_status") == "GROUND_TRUTH_REVIEW_REQUIRED"
            for row in rows
        ),
        "precision_like_strict": tp / (tp + fp + mismatch) if tp + fp + mismatch else None,
        "recall_like_strict": tp / (tp + fn + mismatch) if tp + fn + mismatch else None,
        "formula_note": "Strict rates count SCOPE_MISMATCH as an error in both precision-like and recall-like denominators; ACCEPTABLE_ABSTENTION and UNCLEAR/review-required rows are excluded.",
    }


def build() -> dict[str, Any]:
    market_rows = [row for row in _read_csv(SOURCE / "market_review_queue.csv") if row.get("market_manual_label")]
    stock_rows = [row for row in _read_csv(SOURCE / "stock_review_queue.csv") if row.get("manual_label")]
    if len(market_rows) != 9 or len(stock_rows) != 41:
        raise RuntimeError(f"CALIBRATION_SET_MISMATCH market={len(market_rows)} stock={len(stock_rows)}")
    migrated_market = [_migrate(row, "MARKET") for row in market_rows]
    migrated_stock = [_migrate(row, "STOCK") for row in stock_rows]
    verdict_rows = []
    for scope, rows in (("MARKET", migrated_market), ("STOCK", migrated_stock)):
        for row in rows:
            verdict_rows.append({
                "date": row["date"], "scope": scope, "symbol": row.get("symbol", ""),
                "legacy_model_relative_label": row["legacy_model_relative_label"],
                "human_driver_state": row["human_driver_state"], "human_confidence": row["human_confidence"],
                "human_event_type": row["human_event_type"], "human_event_summary": row["human_event_summary"],
                "current_model_explanation_state": row["current_model_explanation_state"],
                "current_selected_event": row["current_selected_event"], "current_event_type": row["current_event_type"],
                "current_source": row["current_source"], "current_publication_time": row["current_publication_time"],
                "current_verification_status": row["current_verification_status"], "current_causal_status": row["current_causal_status"],
                "event_match": row["event_match"], "event_match_basis": row["event_match_basis"],
                "event_subject_match": row["event_subject_match"], "event_type_match": row["event_type_match"],
                "event_timing_match": row["event_timing_match"], "event_keyword_overlap": row["event_keyword_overlap"],
                "event_source_match": row["event_source_match"],
                "model_verdict": row["model_verdict"], "verdict_basis": row["verdict_basis"],
                "migration_status": row["migration_status"], "human_source_note": row["human_source_note"],
            })
    required = [row for row in verdict_rows if row["migration_status"] == "GROUND_TRUTH_REVIEW_REQUIRED"]
    metrics = {
        "MARKET": _metric([row for row in verdict_rows if row["scope"] == "MARKET"]),
        "STOCK": _metric([row for row in verdict_rows if row["scope"] == "STOCK"]),
        "COMBINED": _metric(verdict_rows),
    }
    prior_qa_path = ROOT / "output/qa/manual_qa_v1/qa_summary.json"
    validation_path = ROOT / "output/qa/post_change_validation_v3/validation_summary.json"
    prior_qa = json.loads(prior_qa_path.read_text(encoding="utf-8"))
    prior_validation = json.loads(validation_path.read_text(encoding="utf-8"))
    old_stock_metrics = prior_qa.get("metrics", {}).get("STOCK", {})
    if not old_stock_metrics:
        old_stock_metrics = prior_qa.get("metrics", {}).get("stock", {})
    metric_counts = Counter(row["legacy_model_relative_label"] for row in migrated_stock)
    label_counts = Counter(row["legacy_model_relative_label"] for row in migrated_market + migrated_stock)
    prior_p = float(prior_validation.get("current_metrics", {}).get("precision_like") or 0)
    prior_r = float(prior_validation.get("current_metrics", {}).get("recall_like") or 0)
    prior_fp = int(prior_validation.get("current_metrics", {}).get("false_positives", 0) or 0)
    prior_fn = int(prior_validation.get("current_metrics", {}).get("false_negatives", 0) or 0)
    prior_tp_p = round(prior_p * prior_fp / (1 - prior_p)) if 0 < prior_p < 1 else 0
    prior_tp_r = round(prior_r * prior_fn / (1 - prior_r)) if 0 < prior_r < 1 else 0
    review_counts = Counter(row["model_verdict"] for row in verdict_rows)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema": "Manual QA Ground-Truth Schema V2",
        "scope": {"market_rows": len(migrated_market), "stock_rows": len(migrated_stock), "total_rows": len(verdict_rows), "new_29_day_rows_adjudicated": 0, "source_replay": str(SOURCE)},
        "migration": {"migrated_cleanly": len(verdict_rows) - len(required), "ground_truth_review_required": len(required), "review_required_rows": [{"date": row["date"], "scope": row["scope"], "symbol": row["symbol"], "reason": row["verdict_basis"]} for row in required], "legacy_label_counts": dict(label_counts), "old_model_relative_labels_used_as_ground_truth": False},
        "current_model_metrics": metrics,
        "current_verdict_counts": {scope: dict(Counter(row["model_verdict"] for row in verdict_rows if row["scope"] == scope)) for scope in ("MARKET", "STOCK")},
        "reconciliation": {
            "interim_stock_metrics_from_literal_legacy_labels": {"legacy_positive_supported": metric_counts["CORRECT_CAUSAL"] + metric_counts["PLAUSIBLE_CAUSAL"], "legacy_false_positives": metric_counts["FALSE_POSITIVE"], "legacy_false_negatives": metric_counts["FALSE_NEGATIVE"], "precision_like": old_stock_metrics.get("precision_like"), "recall_like": old_stock_metrics.get("recall_like")},
            "prior_post_change_10_date_metrics": prior_validation.get("current_metrics", {}),
            "prior_post_change_counts_reconstructed": {"true_positives": prior_tp_p if prior_tp_p == prior_tp_r else None, "false_positives": prior_fp, "false_negatives": prior_fn, "reconstruction_note": "TP inferred independently from reported precision/FP and recall/FN; null if the two reconstructions disagree."},
            "current_v2_stock_counts": {key: metrics["STOCK"][key] for key in ("true_positives", "false_positives", "false_negatives", "scope_mismatches")},
            "reason_for_interim_regression": "The interim package copied the static manual FP/FN labels into its metrics (stock: 6 FP, 13 FN, 11 positive-supported); those labels described an earlier model state, not human ground truth. The post-change 90/75 figures came from a separate 10-date Oct 3 replay. V2 instead preserves the human-supported driver claims, checks event match, and scores the current 29-day replay; it surfaces two former-FP travel rows as current false negatives, an OXY scope mismatch, and a PANW-2026-09-28 exact-event review requirement. Relative to the prior 10-date report, V2 has one fewer TP because PANW Sep 28 is held for human event-level review, two additional FNs because ABNB/BKNG have supported travel-theme truth but are currently unexplained, and OXY is a scope mismatch rather than a same-scope FP. The datasets/methods are therefore not identical and the V2 rates are not forced to match 90/75.",
            "prior_90_75_source": str(validation_path),
            "current_replay_source": str(SOURCE),
        },
        "provider_activity": {"live_provider_calls": 0, "offline_network_calls": 0},
    }
    OUT.mkdir(parents=True, exist_ok=True)
    _write_csv(OUT / "migrated_market_labels.csv", migrated_market)
    _write_csv(OUT / "migrated_stock_labels.csv", migrated_stock)
    _write_csv(OUT / "ground_truth_review_required.csv", required)
    _write_csv(OUT / "current_model_verdicts.csv", verdict_rows)
    (OUT / "metric_reconciliation.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_schema_doc(metrics, summary)
    _write_reconciliation_doc(summary)
    return summary


def _write_schema_doc(metrics: dict[str, Any], summary: dict[str, Any]) -> None:
    text = f'''# Manual QA Ground-Truth Schema V2

This package migrates only the existing reviewed calibration set: 9 market rows and 41 stock rows. It does not adjudicate any new 29-day rows.

## Two layers

**Human ground truth** describes the best human-supported explanation, independent of model success: `human_driver_state` (`STOCK_SPECIFIC_CAUSAL`, `SECTOR_CAUSAL`, `MARKET_CAUSAL`, `MULTI_FACTOR`, `NO_PUBLIC_CATALYST`, `UNCLEAR`), `human_confidence` (`CONFIRMED`, `PLAUSIBLE`, `UNCERTAIN`), plus human event type, summary, and the original source note.

**Model evaluation** is computed for this replay only: current explanation state, selected event, event match, and `model_verdict` (`TRUE_POSITIVE`, `FALSE_POSITIVE`, `FALSE_NEGATIVE`, `ACCEPTABLE_ABSTENTION`, `SCOPE_MISMATCH`, or `UNCLEAR`). A separate `migration_status=GROUND_TRUTH_REVIEW_REQUIRED` flag excludes uncertain truth/event mappings from scoring. `legacy_model_relative_label` is retained for traceability only and is never used to calculate V2 metrics.

## Migration and review policy

Existing notes are copied verbatim into `human_source_note`. Clear notes are mapped to a supported driver scope and confidence; old `FALSE_POSITIVE`/`FALSE_NEGATIVE` labels are not carried over as truth. If the note only rejects an old output, or cannot identify the supported event, the row is flagged for human review. Existing `UNCLEAR` remains uncertain; where the latest replay introduces new contradictory evidence, the row is surfaced for review rather than silently relabeled.

Event matching for stock-specific claims checks the subject (ticker/company alias), timing (explicit future/after-session evidence fails), and normalized event-specific keyword/phrase overlap between the explicit human-supported event summary and the current selected title. Two shared non-generic tokens or a listed exact event phrase are required. Event-type agreement, analyst/action terms, publication time, and source are retained as diagnostics; source is marked non-comparable when the human note does not name it. Scope-only matching is used for sector, market, and explicitly multi-factor truth. A mismatch is not treated as a true positive just because the model selected the same broad explanation type.

## Metric rule

Strict precision-like = TP / (TP + FP + SCOPE_MISMATCH). Strict recall-like = TP / (TP + FN + SCOPE_MISMATCH). A scope mismatch is an incorrect causal assertion for precision and a missed correct-scope explanation for recall. Acceptable abstentions, unclear cases, and review-required rows are excluded. `NO_PUBLIC_CATALYST` is not treated as a false negative; it yields `ACCEPTABLE_ABSTENTION` only when the model abstains.

## Calibration result

Clean migrations: {summary['migration']['migrated_cleanly']}/50. Ground-truth review required: {summary['migration']['ground_truth_review_required']}/50. New rows adjudicated: 0. Provider calls: 0.

| Scope | Strict precision-like | Strict recall-like | TP | FP | FN | Scope mismatch | Acceptable abstention | Unclear/review |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
{_metric_md_row('MARKET', metrics['MARKET'])}
{_metric_md_row('STOCK', metrics['STOCK'])}
{_metric_md_row('COMBINED', metrics['COMBINED'])}
'''
    (OUT / "ground_truth_schema.md").write_text(text, encoding="utf-8")


def _metric_md_row(scope: str, item: dict[str, Any]) -> str:
    def percent(value: Any) -> str:
        return "n/a" if value is None else f"{value:.1%}"
    return f"| {scope} | {percent(item['precision_like_strict'])} | {percent(item['recall_like_strict'])} | {item['true_positives']} | {item['false_positives']} | {item['false_negatives']} | {item['scope_mismatches']} | {item['acceptable_abstentions']} | {item['unclear_or_review_required']} |"


def _write_reconciliation_doc(summary: dict[str, Any]) -> None:
    rec = summary["reconciliation"]
    metrics = summary["current_model_metrics"]
    stock = metrics["STOCK"]
    market = metrics["MARKET"]
    combined = metrics["COMBINED"]
    lines = [
        "# Metric Reconciliation — Manual QA Ground-Truth V2", "",
        "This is a calibration-only, offline recomputation over the 50 already reviewed rows (9 market, 41 stock). No new 29-day rows were adjudicated.", "",
        "## Why the interim stock rate appeared to regress", "",
        "The interim package literally reused the old model-relative labels: 11 positive-supported, 6 FALSE_POSITIVE, and 13 FALSE_NEGATIVE stock labels. That arithmetic produces 11/(11+6)=64.7% precision-like and 11/(11+13)=45.8% recall-like. Those are historical labels, not current model verdicts or durable human truth.", "",
        "The previously reported 90.0% / 75.0% figures came from the separate post-change 10-date replay, with 2 FP and 6 FN. V2 compares the current 29-day replay to separately represented human claims and applies event matching. It does not force parity with that model-relative report.", "",
        "Compared with that reported 10-date result (TP=18 reconstructed, FP=2, FN=6), this V2 stock set has TP=17, FP=1, FN=8, and one scope mismatch. The differences are explicit: one formerly counted TP (PANW Sep 28) is held for event-level human review because the note does not identify BTIG; ABNB and BKNG move from old model-relative FP labels to human-supported sector truth and become current FNs because this replay is unexplained; OXY moves from FP to scope mismatch because the current selected explanation is stock-specific while human truth is sector/commodity. This changes the strict rates to 89.5% / 65.4%; scope mismatch is included in both denominators.", "",
        f"V2 stock strict rates are {stock['precision_like_strict']:.1%} precision-like and {stock['recall_like_strict']:.1%} recall-like (TP={stock['true_positives']}, FP={stock['false_positives']}, FN={stock['false_negatives']}, scope mismatch={stock['scope_mismatches']}). Two travel-theme rows whose old label was FALSE_POSITIVE now have human-supported sector truth but are unexplained in the current replay, so they are current false negatives. OXY is a scope mismatch: current stock-specific earnings selection conflicts with the human-supported sector/commodity explanation. The current PANW Sep 28 event cannot be matched to the vague human note and remains review-required.", "",
        f"Market strict rates are {market['precision_like_strict']:.1%} precision-like and {market['recall_like_strict']:.1%} recall-like (TP={market['true_positives']}, FP={market['false_positives']}, FN={market['false_negatives']}, scope mismatch={market['scope_mismatches']}). The old Sep 15 FP label is not used as truth; its note supports a multi-factor market rationale, while the current selected oil/energy framing is marked a scope mismatch.", "",
        f"Combined strict rates are {combined['precision_like_strict']:.1%} precision-like and {combined['recall_like_strict']:.1%} recall-like (TP={combined['true_positives']}, FP={combined['false_positives']}, FN={combined['false_negatives']}, scope mismatch={combined['scope_mismatches']}). Scope mismatch is included in both strict denominators, as documented in the schema.", "",
        "## Migration/review status", "",
        f"Cleanly migrated: {summary['migration']['migrated_cleanly']}/50. Ground-truth review required: {summary['migration']['ground_truth_review_required']}/50. The review file contains only calibration rows and exact reasons. Existing human notes are copied verbatim; old model-relative labels are retained solely in `legacy_model_relative_label`.", "",
        "## Prior 90/75 report provenance", "",
        f"Source: `{rec['prior_90_75_source']}`. It describes the post-change 10-date run, whereas V2 uses the 29-day interim replay output and event-level truth mapping. The sets are related but not methodologically identical; V2 differences are expected and are not evidence that either figure should be altered to match the other.", "",
        "Provider calls: 0. New 29-day rows adjudicated: 0.",
    ]
    (OUT / "metric_reconciliation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    summary = build()
    print(json.dumps({"migration": summary["migration"], "metrics": summary["current_model_metrics"], "output_dir": str(OUT)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
