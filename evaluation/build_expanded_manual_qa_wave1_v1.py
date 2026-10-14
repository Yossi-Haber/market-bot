"""Build the offline Expanded Manual QA Wave 1 packet from existing artifacts.

This is a packaging-only tool: it reads existing QA queues and an offline
multiday replay, and never writes labels or project caches.
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
QA_ROOT = ROOT / "output" / "qa"
SOURCE_DIR = QA_ROOT / "interim_29_day_manual_qa_v1"
V2_DIR = QA_ROOT / "manual_qa_ground_truth_v2"
REPLAY = ROOT / "output/evaluation_extended_v2/30_day_milestone/resume_after_dxy/replays/resume_20261005_160401/evaluation/market_driver_multiday.json"
OUT = QA_ROOT / "expanded_manual_qa_wave1_v1"

STOCK_WAVE = [
    ("2026-08-17", "AMAT"), ("2026-08-20", "WMT"), ("2026-08-21", "TSLA"),
    ("2026-08-27", "CRM"), ("2026-08-28", "PYPL"), ("2026-08-31", "SLB"),
    ("2026-09-02", "PANW"), ("2026-09-03", "TSLA"), ("2026-09-04", "ADBE"),
    ("2026-09-08", "AMGN"), ("2026-09-09", "META"), ("2026-09-10", "ORCL"),
    ("2026-09-11", "CSCO"), ("2026-08-20", "ISRG"), ("2026-09-08", "SYK"),
    ("2026-09-04", "CEG"), ("2026-09-10", "ELV"), ("2026-08-31", "UBER"),
]
MARKET_WAVE = [
    "2026-08-18", "2026-08-27", "2026-08-28", "2026-09-01",
    "2026-09-03", "2026-09-10", "2026-09-11",
]

SECTOR = {
    "AMAT": "Information Technology / semiconductors", "MRVL": "Information Technology / semiconductors",
    "WMT": "Consumer Staples / retail", "TSLA": "Consumer Discretionary / autos",
    "CRM": "Information Technology / software", "PYPL": "Financials / payments",
    "SLB": "Energy / oilfield services", "PANW": "Information Technology / cybersecurity",
    "ADBE": "Information Technology / software", "AMGN": "Health Care / biotechnology",
    "META": "Communication Services / internet platforms", "ORCL": "Information Technology / software",
    "ETN": "Industrials / electrical equipment", "COP": "Energy / exploration & production",
    "HUM": "Health Care / managed care", "BA": "Industrials / aerospace",
    "CSCO": "Information Technology / networking",
    "CEG": "Utilities / power generation",
    "ISRG": "Health Care / medical devices",
    "SYK": "Health Care / medical devices",
    "ELV": "Health Care / managed care",
    "UBER": "Industrials / mobility platform",
    "TMO": "Health Care / life-sciences tools", "QCOM": "Information Technology / semiconductors",
    "NFLX": "Communication Services / streaming",
}
SECTOR_ETF = {
    "Information Technology": "XLK", "Consumer Staples": "XLP", "Consumer Discretionary": "XLY",
    "Financials": "XLF", "Energy": "XLE", "Health Care": "XLV",
    "Communication Services": "XLC", "Industrials": "XLI",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def pct(value: Any) -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return ""


def sector_context(day: dict[str, Any], symbol: str) -> str:
    label = SECTOR.get(symbol, "Sector not independently classified in this packet")
    broad = label.split(" / ", 1)[0]
    etf = SECTOR_ETF.get(broad)
    for driver in day.get("sector_drivers", []):
        evidence = driver.get("movement_evidence") or []
        for item in evidence:
            metric = str(item.get("metric", ""))
            if etf and metric.startswith(etf + "_"):
                return f"{label}; {metric}={pct(item.get('value'))} (cached sector-driver evidence)"
        if symbol in (driver.get("related_symbols") or []):
            vals = ", ".join(f"{x.get('metric')}={pct(x.get('value'))}" for x in evidence)
            return f"{label}; related cached driver: {vals or driver.get('driver_id', '')}"
    return f"{label}; no directly matched sector ETF observation found in replay"


def market_context(day: dict[str, Any]) -> str:
    vals = day.get("market_values") or {}
    indexes = ", ".join(f"{s} {pct(v.get('change_pct'))}" for s, v in vals.items())
    breadth = (day.get("market_driver_analysis") or {}).get("breadth") or {}
    regime = ", ".join((day.get("market_driver_analysis") or {}).get("market_regime") or [])
    return f"Indices: {indexes or 'unavailable'}; breadth {breadth.get('up','?')} up/{breadth.get('down','?')} down; regimes: {regime or 'none recorded'}"


def candidate_projection(c: dict[str, Any], selected_title: str | None) -> dict[str, Any]:
    title = c.get("title") or c.get("canonical_event") or "(untitled cached candidate)"
    return {
        "title": title,
        "timestamp": c.get("published_at"),
        "source": c.get("source_name") or ", ".join(c.get("independent_sources") or []),
        "event_type": c.get("event_type") or c.get("category"),
        "subject_match": {"role": c.get("target_role"), "gate": c.get("subject_gate"), "score": c.get("subject_score")},
        "causal_gate": c.get("causal_gate"),
        "rejection_or_selection_reason": "SELECTED" if title == selected_title else (c.get("causal_reason") or ("NOT_SELECTED" if c.get("causal_gate") == "PASS" else "REJECTED_OR_NOT_SELECTED")),
        "selected": title == selected_title,
    }


def main() -> None:
    market_rows = read_csv(SOURCE_DIR / "market_review_queue.csv")
    stock_rows = read_csv(SOURCE_DIR / "stock_review_queue.csv")
    review_required = read_csv(V2_DIR / "ground_truth_review_required.csv")
    replay = json.loads(REPLAY.read_text(encoding="utf-8"))
    day_map = {x["market_date"]: x for x in replay["days"]}
    record_map = {(x["date"], x["symbol"]): x for x in replay["stock_records"]}
    OUT.mkdir(parents=True, exist_ok=True)

    # Only genuinely new rows are in scope; prior reviewed rows are not repeated.
    new_market = [r for r in market_rows if r.get("review_status") == "NEW_UNLABELED"]
    new_stock = [r for r in stock_rows if r.get("review_status") == "NEW_UNLABELED"]
    market_index = {r["date"]: r for r in new_market}
    stock_index = {(r["date"], r["symbol"]): r for r in new_stock}

    # P1 is the curated wave only. Integrity-blocked rows are held at P3.
    p1_keys = {("STOCK", d, s) for d, s in STOCK_WAVE} | {("MARKET", d, "") for d in MARKET_WAVE}
    priority_rows: list[dict[str, Any]] = []
    for scope, rows in (("MARKET", new_market), ("STOCK", new_stock)):
        for r in rows:
            date, symbol = r["date"], r.get("symbol", "")
            key = (scope, date, symbol)
            valid_integrity = r.get("integrity_status") == "PASS"
            if key in p1_keys:
                priority, reason = "P1", "Curated into Wave 1 for high information gain and behavior/regime coverage."
            elif not valid_integrity:
                priority, reason = "P3", "Integrity blocked; defer human causal adjudication until source artifacts are repaired."
            elif scope == "STOCK" and (abs(float(r.get("move_pct") or 0)) >= 6 or r.get("overall_explanation_status") in {"STOCK_SPECIFIC", "SECTOR_EXPLAINED", "MARKET_EXPLAINED"}):
                priority, reason = "P2", "Useful second-wave case: material move or non-unexplained model attribution, not selected in the diverse first wave."
            elif scope == "MARKET" and (abs(float(r.get("SPY_change_pct") or 0)) >= 0.6 or abs(float(r.get("QQQ_change_pct") or 0)) >= 1.0 or r.get("primary_market_driver")):
                priority, reason = "P2", "Useful second-wave market-regime/driver case, not selected in the first wave."
            else:
                priority, reason = "P3", "Lower incremental information for the first manual pass; retain in the remaining queue."
            priority_rows.append({**r, "scope": scope, "priority_v1": priority, "priority_reason": reason,
                                  "human_driver_state": "", "human_confidence": "", "human_event_summary": "", "human_source_note": ""})

    review_fields = ["date", "scope", "symbol", "move_pct", "model_explanation_state", "selected_event", "event_type", "source", "published_at", "verification_status", "market_context", "sector_context", "system_rationale", "suggested_review_question", "priority", "human_driver_state", "human_confidence", "human_event_summary", "human_source_note"]
    wave: list[dict[str, Any]] = []
    for date, symbol in STOCK_WAVE:
        r = stock_index[(date, symbol)]
        day = day_map[date]
        expl = r.get("overall_explanation_status", "")
        event = r.get("selected_event", "")
        wave.append({"date": date, "scope": "STOCK", "symbol": symbol, "move_pct": r.get("move_pct"),
                     "model_explanation_state": expl, "selected_event": event, "event_type": r.get("event_type"),
                     "source": r.get("source"), "published_at": r.get("selected_published_at"), "verification_status": r.get("verification_status"),
                     "market_context": market_context(day), "sector_context": sector_context(day, symbol),
                     "system_rationale": r.get("why_system_selected_it") or f"{abs(float(r.get('move_pct') or 0)):.2f}% move; {expl}; causal={r.get('causal_gate')} ({r.get('causal_reason')}).",
                     "suggested_review_question": f"Does the cached evidence support {event or 'a public stock-specific catalyst'} as a material explanation for {symbol}'s {pct(r.get('move_pct'))} move?" if event else f"Is there sufficient evidence for a public catalyst for {symbol}'s {pct(r.get('move_pct'))} move, or should this remain unexplained?",
                     "priority": "P1", "human_driver_state": "", "human_confidence": "", "human_event_summary": "", "human_source_note": ""})
    for date in MARKET_WAVE:
        r = market_index[date]
        day = day_map[date]
        wave.append({"date": date, "scope": "MARKET", "symbol": "", "move_pct": r.get("SPY_change_pct"),
                     "model_explanation_state": "MARKET_EXPLAINED" if (r.get("primary_market_driver") or r.get("selected_market_driver")) else "UNEXPLAINED", "selected_event": r.get("primary_market_driver") or r.get("selected_market_driver"),
                     "event_type": r.get("market_regimes"), "source": r.get("selected_market_story_source"), "published_at": r.get("evidence_timestamp"),
                     "verification_status": r.get("market_driver_status"), "market_context": market_context(day),
                     "sector_context": r.get("sector_breadth_summary") or "See cached sector dispersion/regime context in market_context.",
                     "system_rationale": f"SPY {pct(r.get('SPY_change_pct'))}; QQQ {pct(r.get('QQQ_change_pct'))}; {r.get('market_regimes') or 'no explicit regime'}; driver={r.get('primary_market_driver') or 'none selected'}.",
                     "suggested_review_question": "Does the selected market-level driver adequately explain the index/breadth pattern, or should the day be multi-factor/unclear?",
                     "priority": "P1", "human_driver_state": "", "human_confidence": "", "human_event_summary": "", "human_source_note": ""})
    write_csv(OUT / "wave1_manual_review.csv", wave, review_fields)

    # Separate packet: human recommendations are clearly advisory and never copied into label tables.
    adjudication_rows = []
    recs = {
        ("2026-09-15", "TMO"): ("UNCLEAR", "UNCERTAIN", "The conference appearances and incidental references do not establish a concrete same-day event; current evidence does not establish no catalyst either."),
        ("2026-09-15", "QCOM"): ("UNCLEAR", "UNCERTAIN", "The cached AI/data-center and chip-demand headlines are not a sufficiently specific, direction-confirming event in the selected evidence."),
        ("2026-09-18", "NFLX"): ("STOCK_SPECIFIC_CAUSAL", "PLAUSIBLE", "A timely Wells Fargo downgrade is directly about NFLX and directionally consistent, but replay verification remains not confirmed."),
        ("2026-09-28", "PANW"): ("STOCK_SPECIFIC_CAUSAL", "PLAUSIBLE", "A same-day BTIG target increase is direct and directionally supportive; the prior note also mentions same-day analyst catalysts, but exact causality remains unverified."),
    }
    prior_by = {(r["date"], r.get("symbol", "")): r for r in review_required}
    adjudication_csv = []
    for key, (rec_state, rec_conf, rec_basis) in recs.items():
        date, symbol = key
        prior = prior_by[key]
        cur = record_map[key]
        projected = [candidate_projection(c, cur.get("selected_event")) for c in cur.get("candidate_stories", [])]
        day = day_map[date]
        selected = cur.get("selected_event") or ""
        one = {"date": date, "symbol": symbol, "move_pct": cur.get("move_pct"), "previous_manual_note": prior.get("human_source_note"),
               "current_model_explanation": cur.get("overall_explanation_status"), "current_selected_event": selected,
               "event_type": cur.get("event_type"), "publication_timestamp": cur.get("selected_published_at"),
               "source": cur.get("source"), "verification_status": cur.get("verification_status"),
               "causal_gate": cur.get("causal_gate"), "causal_reason": cur.get("causal_reason"),
               "market_context": market_context(day), "sector_context": sector_context(day, symbol),
               "candidate_count": len(projected), "candidate_events": projected,
               "recommendation_only_state": rec_state, "recommendation_only_confidence": rec_conf,
               "recommendation_only_basis": rec_basis}
        adjudication_rows.append(one)
        adjudication_csv.append({**one, "move_pct": pct(one["move_pct"]), "candidate_events_json": json.dumps(projected, ensure_ascii=False, separators=(",", ":"))})
    write_csv(OUT / "four_case_adjudication.csv", adjudication_csv,
              ["date", "symbol", "move_pct", "previous_manual_note", "current_model_explanation", "current_selected_event", "event_type", "publication_timestamp", "source", "verification_status", "causal_gate", "causal_reason", "market_context", "sector_context", "candidate_count", "candidate_events_json", "recommendation_only_state", "recommendation_only_confidence", "recommendation_only_basis"])

    # Markdown packet keeps the entire cached candidate set, but omits URLs and discovery internals.
    md = ["# Four-case adjudication packet", "", "Recommendations below are **RECOMMENDATION_ONLY**. They are not gold labels and have not been written to any label file.", ""]
    for item in adjudication_rows:
        md += [f"## {item['symbol']} — {item['date']} ({pct(item['move_pct'])})", "",
               f"- Previous manual note: {item['previous_manual_note']}",
               f"- Current model: {item['current_model_explanation']}; selected event: {item['current_selected_event'] or 'none'}; event type: {item['event_type']}",
               f"- Publication/source: {item['publication_timestamp'] or 'n/a'} / {item['source'] or 'n/a'}; verification: {item['verification_status']}; causal gate: {item['causal_gate']} ({item['causal_reason']})",
               f"- Market context: {item['market_context']}", f"- Sector context: {item['sector_context']}",
               f"- **RECOMMENDATION_ONLY:** {item['recommendation_only_state']} / {item['recommendation_only_confidence']} — {item['recommendation_only_basis']}", "",
               "Cached candidate events (complete symbol candidate set):", "",
               "| # | Title | Time | Source | Event | Subject match | Causal | Selection/rejection |",
               "|---:|---|---|---|---|---|---|---|"]
        for i, c in enumerate(item["candidate_events"], 1):
            title = str(c["title"]).replace("|", "\\|").replace("\n", " ")
            subject = c.get("subject_match") or {}
            md.append(f"| {i} | {title} | {c.get('timestamp') or 'n/a'} | {c.get('source') or 'n/a'} | {c.get('event_type') or 'n/a'} | {subject.get('role') or 'n/a'} / {subject.get('gate') or 'n/a'} ({subject.get('score') if subject.get('score') is not None else 'n/a'}) | {c.get('causal_gate') or 'n/a'} | {c.get('rejection_or_selection_reason') or 'n/a'} |")
        md.append("")
    (OUT / "four_case_adjudication.md").write_text("\n".join(md), encoding="utf-8")

    # Priority queue is all new rows, sorted P1/P2/P3 then date/scope/symbol.
    priority_rows.sort(key=lambda x: ({"P1": 0, "P2": 1, "P3": 2}[x["priority_v1"]], x["date"], x["scope"], x.get("symbol", "")))
    write_csv(OUT / "remaining_priority_queue.csv", priority_rows,
              list(dict.fromkeys(["priority_v1", "priority_reason", "scope"] + list(market_rows[0].keys()) + ["symbol"] + list(stock_rows[0].keys()) + ["human_driver_state", "human_confidence", "human_event_summary", "human_source_note"])))

    counts = Counter(x["priority_v1"] for x in priority_rows)
    explain_counts = Counter(x["model_explanation_state"] or "UNSPECIFIED" for x in wave)
    explain = {name: explain_counts.get(name, 0) for name in ("STOCK_SPECIFIC", "SECTOR_EXPLAINED", "MARKET_EXPLAINED", "UNEXPLAINED")}
    event_types = Counter((x["event_type"] or "UNSPECIFIED").split(",")[0] for x in wave)
    dates = sorted({x["date"] for x in wave})
    sectors = sorted({(x["sector_context"] or "").split(";")[0] for x in wave if x["scope"] == "STOCK"})
    summary = {
        "source_replay": str(REPLAY.relative_to(ROOT)),
        "new_unlabeled_rows": {"market": len(new_market), "stock": len(new_stock), "total": len(new_market) + len(new_stock)},
        "priority_counts": {k: counts.get(k, 0) for k in ("P1", "P2", "P3")},
        "priority_p1_share": round(counts.get("P1", 0) / max(1, len(priority_rows)), 4),
        "wave1": {"rows": len(wave), "market": sum(x["scope"] == "MARKET" for x in wave), "stock": sum(x["scope"] == "STOCK" for x in wave),
                  "explanation_types": explain, "event_types": dict(event_types), "distinct_dates": len(dates), "dates": dates,
                  "distinct_stock_sectors": len(sectors), "stock_sectors": sectors, "estimated_manual_decisions": len(wave)},
        "selection_constraints": ["The new-unlabeled stock source set contains 66 UNEXPLAINED, 22 STOCK_SPECIFIC, 5 MARKET_EXPLAINED, and 0 SECTOR_EXPLAINED rows; therefore no sector-explained/inherited stock case could be selected without drawing from already-labeled or integrity-blocked rows."],
        "integrity_blocked_new_rows": [{"scope": r["scope"], "date": r["date"], "symbol": r.get("symbol", ""), "reasons": r.get("integrity_reasons", "")} for r in priority_rows if r.get("integrity_status") != "PASS"],
        "existing_schema_v2_calibration_reference": {"migrated_cleanly": 46, "review_required": 4, "stock_precision_like": 0.895, "stock_recall_like": 0.654, "market_precision_like": 0.667, "market_recall_like": 0.333},
        "label_safety": "No new human labels were populated; adjudication states are advisory only.",
    }
    (OUT / "wave1_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    smd = ["# Expanded Manual QA Wave 1 — summary", "", f"New unlabeled rows: **{summary['new_unlabeled_rows']['total']}** ({len(new_market)} market, {len(new_stock)} stock).",
           f"Priority distribution: **P1 {counts.get('P1',0)} / P2 {counts.get('P2',0)} / P3 {counts.get('P3',0)}**; P1 share {summary['priority_p1_share']:.1%}.",
           f"Wave 1: **{len(wave)}** decisions ({summary['wave1']['market']} market, {summary['wave1']['stock']} stock); estimated manual decisions: {len(wave)}.",
           f"Coverage: explanation types {explain}; event types {dict(event_types)}; {len(dates)} dates ({', '.join(dates)}); {len(sectors)} distinct stock sectors.",
           "", "## Selection rationale", "Wave 1 intentionally mixes large unexplained moves, selected stock-specific explanations, analyst/earnings/contract/regulatory cases, market attribution, positive and negative moves, and distinct dates/sectors. It is a curated information-gain sample, not a cut of the old HIGH queue.",
           "", "## Integrity holds", "Integrity-failed source rows are assigned P3 and excluded from Wave 1. Human label columns remain blank. The four adjudication recommendations are separate and explicitly advisory.",
           "The new-unlabeled stock pool contains no SECTOR_EXPLAINED/inherited rows (66 UNEXPLAINED, 22 STOCK_SPECIFIC, 5 MARKET_EXPLAINED, 0 SECTOR_EXPLAINED); Wave 1 does not manufacture that category from already-labeled cases.",
           "", "## Existing calibration reference", "V2: 46 migrated cleanly, 4 review required. Stock precision-like 89.5%, recall-like 65.4%; market precision-like 66.7%, recall-like 33.3%. The four unresolved cases remain unresolved until a human records adjudication.", "",
           "## Outputs", *[f"- `{p.name}`" for p in [OUT / "four_case_adjudication.md", OUT / "four_case_adjudication.csv", OUT / "wave1_manual_review.csv", OUT / "wave1_summary.md", OUT / "wave1_summary.json", OUT / "remaining_priority_queue.csv"]], ""]
    (OUT / "wave1_summary.md").write_text("\n".join(smd), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"OUTPUT_DIR={OUT}")


if __name__ == "__main__":
    main()
