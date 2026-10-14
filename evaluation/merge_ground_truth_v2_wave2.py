"""Merge Wave 2 human adjudications and recalculate V2 QA metrics offline.

This analysis-only command reads existing Ground Truth V2, the Wave 2 human
adjudications, QA queues, and a frozen offline replay. It does not call
providers or write caches/production files.
"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from evaluation.merge_ground_truth_v2_wave1 import (
    HUMAN_FIELDS,
    HUMAN_STATES,
    MODEL_STATES,
    VERDICTS,
    annotate_row,
    metric,
    normalized_human,
    root_cause as prior_root_cause,
)

ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / "output/qa"
BASE = QA / "ground_truth_v2_wave1_merged"
QUEUE = QA / "interim_29_day_manual_qa_v1"
WAVE2_INPUT = ROOT / "evaluation/manual_qa_v2/wave2_manual_review_adjudicated_v1.csv"
WAVE2_PACKET = QA / "expanded_manual_qa_wave2_v1/wave2_manual_review.csv"
REPLAY = ROOT / "output/evaluation_extended_v2/30_day_milestone/resume_after_dxy/replays/resume_20261005_160401/evaluation/market_driver_multiday.json"
OUT = QA / "ground_truth_v2_wave2_merged"
EXPECTED_SCOPE = {"MARKET": 9, "STOCK": 16}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(dict.fromkeys(k for row in rows for k in row)) or ["date"]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list, tuple)) else v for k, v in row.items()})


def row_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return str(row["date"]), str(row.get("scope", "")), str(row.get("symbol") or "")


def model_row_for(label: dict[str, Any], day_map: dict[str, dict[str, Any]], stock_map: dict[tuple[str, str], dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    date, scope, symbol = row_key(label)
    if scope == "STOCK":
        record = stock_map.get((date, symbol))
        if record is None:
            raise ValueError(f"STOCK_NOT_IN_FROZEN_REPLAY:{date}:{symbol}")
        return ({
            "current_model_explanation_state": record.get("overall_explanation_status") or "UNEXPLAINED",
            "current_selected_event": record.get("selected_event") or "",
            "current_event_type": record.get("event_type") or "",
            "current_source": record.get("source") or "",
            "current_publication_time": record.get("selected_published_at") or "",
            "current_verification_status": record.get("verification_status") or "",
            "current_causal_status": record.get("causal_gate") or "",
            "selected_event": record.get("selected_event") or "",
            "event_type": record.get("event_type") or "",
            "source": record.get("source") or "",
            "selected_published_at": record.get("selected_published_at") or "",
            "event_time_relation": record.get("event_time_relation") or "",
        }, record)

    day = day_map.get(date)
    if day is None:
        raise ValueError(f"MARKET_DATE_NOT_IN_FROZEN_REPLAY:{date}")
    production = day.get("production_analysis") or {}
    driver = production.get("main_market_driver") or day.get("main_market_driver")
    story = ""
    if isinstance(driver, dict):
        stories = driver.get("candidate_stories") or []
        story = (stories[0].get("title") if stories and isinstance(stories[0], dict) else "") or driver.get("driver_id", "")
    analysis = day.get("market_driver_analysis") or {}
    primary = analysis.get("primary_driver") or {}
    state = "MARKET_EXPLAINED" if driver else "UNEXPLAINED"
    event_type = primary.get("event_type") or (driver.get("driver_id", "") if isinstance(driver, dict) else "")
    return ({
        "current_model_explanation_state": state,
        "current_selected_event": story,
        "current_event_type": event_type,
        "current_source": "",
        "current_publication_time": "",
        "current_verification_status": (driver.get("verification_status", "") if isinstance(driver, dict) else ""),
        "current_causal_status": (driver.get("hypothesis_status", "") if isinstance(driver, dict) else ""),
        "selected_event": story,
        "event_type": event_type,
        "source": "",
    }, None)


def output_row(label: dict[str, Any], model: dict[str, Any], record: dict[str, Any] | None, status: str) -> dict[str, Any]:
    human = normalized_human(label)
    prepared = dict(label)
    prepared.update(model)
    # Keep the canonical helper's V2 scope/event semantics for every reviewed row.
    return annotate_row(prepared, human, model, record, source_status=status)


def market_discovery_info(date: str, day: dict[str, Any], replay_cache: Path) -> dict[str, Any]:
    metrics = day.get("market_candidate_metrics") or {}
    cache_path = replay_cache / f"{date}.json"
    cache_doc: dict[str, Any] = {}
    if cache_path.is_file():
        try:
            cache_doc = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache_doc = {}
    query_count = len(cache_doc["queries"]) if isinstance(cache_doc.get("queries"), list) else None
    raw = metrics.get("raw_candidate_count")
    eligible = metrics.get("eligible_candidate_count")
    return {"query_count": query_count, "candidate_count": raw, "eligible_candidate_count": eligible,
            "cache_file_present": cache_path.is_file(), "market_discovery_status": day.get("market_discovery_cache_state", "UNKNOWN")}


def market_cache_presence(date: str, day: dict[str, Any], replay: dict[str, Any]) -> dict[str, bool]:
    stocks = [r for r in replay.get("stock_records", []) if r.get("date") == date]
    symbol = any(int(r.get("candidate_count") or 0) > 0 for r in stocks)
    production = day.get("production_analysis") or {}
    sectors = production.get("sector_drivers") or day.get("sector_drivers") or []
    sector = any(bool((d.get("candidate_stories") or [])) for d in sectors if isinstance(d, dict))
    coverage = day.get("cache_coverage") or {}
    other = any(coverage.get(k) == "AVAILABLE" for k in ("marketaux", "official", "tier2"))
    return {"symbol_scoped_cache_present": symbol, "sector_cache_present": sector, "other_cached_news_present": other}


def exact_source_url_pool_matches(label: dict[str, Any], day: dict[str, Any], replay: dict[str, Any], replay_cache: Path) -> dict[str, Any]:
    """Conservatively match human-cited URLs to URLs present in saved pools."""
    cited = {u.rstrip(".,;)").lower().rstrip("/") for u in re.findall(r"https?://[^\s]+", str(label.get("human_source_note", "")))}
    if not cited:
        return {"cited_url_count": 0, "exact_url_pool_matches": [], "unmatched_cited_urls": []}
    cache_path = replay_cache / f"{label['date']}.json"
    cache_doc: dict[str, Any] = {}
    if cache_path.is_file():
        try:
            cache_doc = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache_doc = {}
    pools: dict[str, list[dict[str, Any]]] = {"market_discovery": list(cache_doc.get("top_candidates") or []), "symbol_scoped": [], "sector": [], "other": []}
    for stock in replay.get("stock_records", []):
        if stock.get("date") == label["date"]:
            pools["symbol_scoped"].extend(stock.get("candidate_stories") or [])
    production = day.get("production_analysis") or {}
    for sector in production.get("sector_drivers") or day.get("sector_drivers") or []:
        if isinstance(sector, dict):
            pools["sector"].extend(sector.get("candidate_stories") or [])
    for obj in (day.get("main_market_driver"), production.get("main_market_driver"), (day.get("market_driver_analysis") or {}).get("primary_driver")):
        if isinstance(obj, dict):
            pools["other"].extend(obj.get("candidate_stories") or [])
    matches: list[dict[str, str]] = []
    for pool, candidates in pools.items():
        for candidate in candidates:
            candidate_urls = {str(candidate.get(k) or "").rstrip(".,;)").lower().rstrip("/") for k in ("source_url", "url")}
            for url in sorted(cited & candidate_urls):
                matches.append({"url": url, "pool": pool, "title": str(candidate.get("title") or candidate.get("canonical_event") or "")})
    matched = {r["url"] for r in matches}
    return {"cited_url_count": len(cited), "exact_url_pool_matches": matches, "unmatched_cited_urls": sorted(cited - matched), "exact_match_is_conservative_not_absence_proof": True}


def root_cause_v2(row: dict[str, Any], record: dict[str, Any] | None, day: dict[str, Any] | None, discovery: dict[str, Any] | None) -> tuple[str, str, str]:
    verdict = row["model_verdict"]
    truth = row.get("human_driver_state", "")
    scope = row.get("scope", "")
    if verdict == "SCOPE_MISMATCH":
        if truth == "SECTOR_CAUSAL":
            return "SECTOR_DRIVER_NOT_PROMOTED", row.get("verdict_basis", ""), "scope attribution"
        if truth == "MARKET_CAUSAL":
            return "STOCK_SCOPE_INHERITANCE_MISS", row.get("verdict_basis", ""), "scope attribution"
        return "OTHER", row.get("verdict_basis", ""), "scope attribution"
    if verdict == "FALSE_POSITIVE":
        if scope == "STOCK" and truth == "STOCK_SPECIFIC_CAUSAL":
            return "STOCK_WRONG_EVENT_SELECTED", row.get("verdict_basis", ""), "event selection"
        return "OTHER", row.get("verdict_basis", ""), "attribution decision"
    if verdict == "FALSE_NEGATIVE":
        if scope == "MARKET":
            info = discovery or {}
            if info.get("candidate_count") == 0:
                return "MARKET_DISCOVERY_EMPTY", "No raw market-discovery candidates in replay metrics.", "market discovery"
            if day and (day.get("selected_market_candidate") is None):
                return "MARKET_DRIVER_PRESENT_NOT_SELECTED", "Market discovery had candidates but no selected market candidate.", "market driver selection"
            return "MARKET_EVIDENCE_WRONG_POOL", row.get("verdict_basis", ""), "market evidence eligibility"
        if record:
            count = int(record.get("candidate_count") or 0)
            reason = str(record.get("rejection_reason") or record.get("causal_reason") or "")
            if count > 0 and record.get("selected_event"):
                return "VERIFICATION_FAILURE", f"Candidate selected but current model abstained; verification={record.get('verification_status')}; reason={reason}", "verification"
            if count > 0 and reason:
                return "CAUSAL_GATE_REJECTION", f"candidate_count={count}; rejection={reason}; best_rejected={record.get('best_rejected_candidate') or ''}", "causal/selection gate"
            if count > 0:
                return "STOCK_CANDIDATE_PRESENT_NOT_SELECTED", f"candidate_count={count}; best_rejected={record.get('best_rejected_candidate') or ''}", "candidate selection"
            return "INSUFFICIENT_EVIDENCE", "No cached stock candidates in replay record.", "discovery"
        if truth in {"MARKET_CAUSAL", "SECTOR_CAUSAL"}:
            return "STOCK_SCOPE_INHERITANCE_MISS", row.get("verdict_basis", ""), "inheritance"
    if verdict == "FALSE_NEGATIVE":
        return "OTHER", row.get("verdict_basis", ""), "unknown"
    return "", "", ""


def strict_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def compare_metrics(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for scope in ("MARKET", "STOCK", "COMBINED"):
        output[scope] = {"before_wave2": before[scope], "after_wave2": after[scope]}
    return output


def main() -> None:
    base_market = [{**r, "scope": "MARKET"} for r in read_csv(BASE / "merged_ground_truth_market.csv")]
    base_stock = [{**r, "scope": "STOCK"} for r in read_csv(BASE / "merged_ground_truth_stock.csv")]
    base_verdicts = read_csv(BASE / "current_model_verdicts.csv")
    wave = read_csv(WAVE2_INPUT)
    packet = read_csv(WAVE2_PACKET)
    replay = json.loads(REPLAY.read_text(encoding="utf-8"))
    market_queue = {r["date"]: r for r in read_csv(QUEUE / "market_review_queue.csv")}
    stock_queue = {(r["date"], r["symbol"]): r for r in read_csv(QUEUE / "stock_review_queue.csv")}
    day_map = {d["market_date"]: d for d in replay["days"]}
    stock_map = {(r["date"], r["symbol"]): r for r in replay["stock_records"]}
    replay_cache = REPLAY.parents[1] / "cache" / "market_driver_news"
    if len(wave) != 25 or Counter(r.get("scope") for r in wave) != Counter(EXPECTED_SCOPE):
        raise ValueError(f"WAVE2_INPUT_COUNT_OR_SCOPE_MISMATCH:{len(wave)}:{dict(Counter(r.get('scope') for r in wave))}")
    if len(packet) != 25:
        raise ValueError(f"WAVE2_PACKET_COUNT_MISMATCH:{len(packet)}")

    base_rows = [*base_market, *base_stock]
    base_keys = [row_key(r) for r in base_rows]
    if len(set(base_keys)) != len(base_keys):
        raise ValueError("DUPLICATE_KEYS_IN_EXISTING_GROUND_TRUTH")
    wave_keys = [row_key(r) for r in wave]
    if len(set(wave_keys)) != 25 or set(wave_keys) & set(base_keys):
        raise ValueError("WAVE2_DUPLICATE_OR_CONFLICT_WITH_REVIEWED_GROUND_TRUTH")
    packet_by_key = {row_key(r): r for r in packet}
    for src in wave:
        key = row_key(src)
        if key not in packet_by_key:
            raise ValueError(f"WAVE2_KEY_NOT_IN_REVIEW_PACKET:{key}")
        normalized_human(src)  # validate schema but retain source values unchanged
        if src.get("date") == "2026-09-22":
            raise ValueError("SEP22_MUST_REMAIN_EXCLUDED")

    # The pre-Wave-2 output is the fixed 75-row comparison baseline.
    if len(base_verdicts) != 75:
        raise ValueError(f"PRE_WAVE2_BASELINE_NOT_75:{len(base_verdicts)}")
    baseline_by_key = {row_key(r): r for r in base_verdicts}
    model_parity_checks = 0
    for label in base_rows:
        model, record = model_row_for(label, day_map, stock_map)
        old = baseline_by_key[row_key(label)]
        if model.get("current_model_explanation_state") != old.get("current_model_explanation_state"):
            raise ValueError(f"BASELINE_MODEL_STATE_PARITY_FAILURE:{row_key(label)}")
        if label["scope"] == "STOCK" and (model.get("current_selected_event") or "") != (old.get("current_selected_event") or ""):
            raise ValueError(f"BASELINE_SELECTED_EVENT_PARITY_FAILURE:{row_key(label)}")
        model_parity_checks += 1
    # Resolved legacy rows are present as reviewed in canonical base; the four
    # old review-required markers are not counted as unresolved after merge.
    remaining_review_required = sum(1 for r in [*base_rows, *wave] if r.get("migration_status") == "GROUND_TRUTH_REVIEW_REQUIRED")

    all_labels = [*base_rows, *wave]
    fresh: list[dict[str, Any]] = []
    wave_keys_set = set(wave_keys)
    for label in all_labels:
        model, record = model_row_for(label, day_map, stock_map)
        status = "WAVE2_HUMAN_ADJUDICATED" if row_key(label) in wave_keys_set else str(label.get("migration_status") or "PREVIOUSLY_REVIEWED")
        fresh.append(output_row(label, model, record, status))
    if len(fresh) != 100 or len({row_key(r) for r in fresh}) != 100:
        raise ValueError(f"CANONICAL_REVIEWED_COUNT_OR_UNIQUENESS_FAILURE:{len(fresh)}")
    fresh_by_key = {row_key(r): r for r in fresh}
    human_changes = [(row_key(src), field) for src in all_labels for field in HUMAN_FIELDS if fresh_by_key[row_key(src)].get(field, "") != src.get(field, "")]
    if human_changes:
        raise ValueError(f"WAVE2_HUMAN_LABELS_CHANGED:{human_changes}")

    after_metrics = {scope: metric([r for r in fresh if scope == "COMBINED" or r["scope"] == scope]) for scope in ("MARKET", "STOCK", "COMBINED")}
    before_metrics = {scope: metric([r for r in base_verdicts if scope == "COMBINED" or r["scope"] == scope]) for scope in ("MARKET", "STOCK", "COMBINED")}
    wave_rows = [r for r in fresh if r.get("migration_status") == "WAVE2_HUMAN_ADJUDICATED"]
    wave_metrics = {scope: metric([r for r in wave_rows if scope == "COMBINED" or r["scope"] == scope]) for scope in ("MARKET", "STOCK", "COMBINED")}

    # Model-state breakdown by scope.
    model_breakdown: dict[str, Any] = {"stock": {}, "market": {}}
    for state in MODEL_STATES:
        rows = [r for r in fresh if r["scope"] == "STOCK" and r.get("current_model_explanation_state") == state]
        counts = Counter(r["model_verdict"] for r in rows)
        tp, fp, sm = counts["TRUE_POSITIVE"], counts["FALSE_POSITIVE"], counts["SCOPE_MISMATCH"]
        model_breakdown["stock"][state] = {"count": len(rows), "correct": tp, "incorrect": fp + sm, "false_negatives": counts["FALSE_NEGATIVE"], "precision_like": tp / (tp + fp + sm) if tp + fp + sm else None, "verdict_counts": {v: counts[v] for v in VERDICTS}}
    for state, predicate in (("DRIVER_SELECTED", lambda r: r.get("current_model_explanation_state") != "UNEXPLAINED"), ("UNEXPLAINED", lambda r: r.get("current_model_explanation_state") == "UNEXPLAINED")):
        rows = [r for r in fresh if r["scope"] == "MARKET" and predicate(r)]
        counts = Counter(r["model_verdict"] for r in rows)
        model_breakdown["market"][state] = {"count": len(rows), "correct": counts["TRUE_POSITIVE"], "incorrect": counts["FALSE_POSITIVE"] + counts["FALSE_NEGATIVE"] + counts["SCOPE_MISMATCH"], "verdict_counts": {v: counts[v] for v in VERDICTS}}

    truth_breakdown: dict[str, Any] = {}
    for state in HUMAN_STATES:
        rows = [r for r in fresh if r.get("human_driver_state") == state]
        counts = Counter(r["model_verdict"] for r in rows)
        truth_breakdown[state] = {"reviewed": len(rows), **{k.lower(): counts[k] for k in VERDICTS}}

    # Detailed Wave 2 error taxonomy; source/cache fields indicate availability
    # only and are not treated as proof that a story was relevant.
    errors: list[dict[str, Any]] = []
    for row in wave_rows:
        if row["model_verdict"] not in {"FALSE_POSITIVE", "FALSE_NEGATIVE", "SCOPE_MISMATCH"}:
            continue
        date, scope, symbol = row_key(row)
        record = stock_map.get((date, symbol)) if scope == "STOCK" else None
        day = day_map.get(date) if scope == "MARKET" else None
        discovery = market_discovery_info(date, day, replay_cache) if day else None
        category, cause, stage = root_cause_v2(row, record, day, discovery)
        if scope == "MARKET":
            presence = market_cache_presence(date, day or {}, replay)
            availability = {**(discovery or {}), **presence}
        else:
            availability = {"cached_candidate_count": int((record or {}).get("candidate_count") or 0), "candidate_cache_available": bool(record and int(record.get("candidate_count") or 0) > 0), "verification_status": (record or {}).get("verification_status", "")}
        errors.append({"date": date, "scope": scope, "symbol": symbol,
                       "human_driver_state": row.get("human_driver_state"), "human_supported_event_or_driver": row.get("human_event_summary"),
                       "model_explanation_state": row.get("current_model_explanation_state"), "model_explanation": row.get("current_event_type"),
                       "selected_event": row.get("current_selected_event"), "model_verdict": row.get("model_verdict"),
                       "root_cause_category": category, "root_cause": cause, "first_pipeline_loss_stage": stage,
                       "cached_evidence_availability_json": availability,
                       "candidate_count": (record or {}).get("candidate_count", ""),
                       "best_rejected_candidate": (record or {}).get("best_rejected_candidate", ""),
                       "rejection_reason": (record or {}).get("rejection_reason", "")})

    error_counts = Counter(r["model_verdict"] for r in errors)
    root_counts = Counter(r["root_cause_category"] for r in errors)
    wave_verdict_counts = Counter(r["model_verdict"] for r in wave_rows)

    # Discovery comparison to Wave 1 uses the same replay and cache diagnostics.
    wave1 = read_csv(QA / "expanded_manual_qa_wave1_v1" / "wave1_manual_review.csv")
    wave1_market = [r for r in wave1 if r.get("scope") == "MARKET"]
    def discovery_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        info = []
        for r in rows:
            day = day_map.get(r["date"])
            if not day:
                continue
            x = market_discovery_info(r["date"], day, replay_cache)
            x.update(market_cache_presence(r["date"], day, replay))
            x["source_url_match"] = exact_source_url_pool_matches(r, day, replay, replay_cache)
            info.append(x)
        exact_pool_hits = Counter(hit["pool"] for item in info for hit in item["source_url_match"]["exact_url_pool_matches"])
        return {
            "market_rows": len(info),
            "zero_market_discovery_queries": sum(x.get("query_count") == 0 for x in info),
            "query_count_unavailable": sum(x.get("query_count") is None for x in info),
            "zero_market_discovery_candidates": sum(x.get("candidate_count") == 0 for x in info),
            "symbol_scoped_cache_present_when_market_discovery_empty": sum(x.get("candidate_count") == 0 and x.get("symbol_scoped_cache_present") for x in info),
            "sector_or_other_cache_present_when_market_discovery_empty": sum(x.get("candidate_count") == 0 and (x.get("sector_cache_present") or x.get("other_cached_news_present")) for x in info),
            "usable_market_discovery_candidates": sum((x.get("eligible_candidate_count") or 0) > 0 for x in info),
            "exact_human_cited_url_matches_by_pool": {pool: exact_pool_hits.get(pool, 0) for pool in ("market_discovery", "symbol_scoped", "sector", "other")},
            "human_cited_urls_without_exact_cache_match": sum(len(x["source_url_match"]["unmatched_cited_urls"]) for x in info),
            "cache_presence_is_not_relevance_proof": True,
        }
    wave2_market_source = [r for r in wave if r.get("scope") == "MARKET"]
    wave1_human_market = [r for r in base_market if r.get("migration_status") == "WAVE1_HUMAN_ADJUDICATED"]
    discovery_wave1 = discovery_summary(wave1_human_market)
    discovery_wave2 = discovery_summary(wave2_market_source)

    # Keep error root causes descriptive and deterministic, with explicit counts.
    stock_wave_counts = Counter(r["model_verdict"] for r in wave_rows if r["scope"] == "STOCK")
    market_wave_counts = Counter(r["model_verdict"] for r in wave_rows if r["scope"] == "MARKET")
    wrong_event = sum(r["scope"] == "STOCK" and r["model_verdict"] == "FALSE_POSITIVE" and r.get("human_driver_state") == "STOCK_SPECIFIC_CAUSAL" for r in errors)
    cached_rejected = sum(r["scope"] == "STOCK" and r["model_verdict"] == "FALSE_NEGATIVE" and int(r.get("candidate_count") or 0) > 0 for r in errors)
    inheritance_errors = sum(r["root_cause_category"] == "STOCK_SCOPE_INHERITANCE_MISS" for r in errors)
    after_total = after_metrics["COMBINED"]
    prior_total = before_metrics["COMBINED"]

    # Generalization is descriptive; cases are curated rather than random.
    precision_mid_high = after_metrics["STOCK"]["precision_like"] is not None and after_metrics["STOCK"]["precision_like"] >= 0.83
    recall_around_60 = after_metrics["STOCK"]["recall_like"] is not None and 0.55 <= after_metrics["STOCK"]["recall_like"] <= 0.65
    market_is_largest_recall_gap = (after_metrics["MARKET"]["recall_like"] or 0) < (after_metrics["STOCK"]["recall_like"] or 0)
    repeated_roots = [(k, n) for k, n in root_counts.items() if n >= 2]
    assessment = {
        "stock_precision_remains_mid_high_80s": precision_mid_high,
        "stock_recall_around_60_percent": recall_around_60,
        "market_recall_is_largest_weakness": market_is_largest_recall_gap,
        "market_discovery_emptiness_generalizes": discovery_wave1["zero_market_discovery_candidates"] > 0 and discovery_wave2["zero_market_discovery_candidates"] > 0,
        "new_dominant_repeated_root_causes": dict(repeated_roots),
        "previously_suspected_issue_disappeared": "No strong disappearance signal; the Wave 2 sample continues to show low market-discovery yield, while cache-presence flags do not establish relevance.",
        "confidence_note": "Wave 2 was deliberately selected, not random; descriptive results should not be interpreted as unbiased population estimates.",
        "wave1_market_discovery": discovery_wave1,
        "wave2_market_discovery": discovery_wave2,
        "wave2_stock_errors": {"false_positives": stock_wave_counts["FALSE_POSITIVE"], "false_negatives": stock_wave_counts["FALSE_NEGATIVE"], "scope_mismatches": stock_wave_counts["SCOPE_MISMATCH"], "wrong_event_selections": wrong_event, "cached_but_rejected_false_negatives": cached_rejected, "inheritance_mistakes": inheritance_errors},
    }

    # One decision only; finalized after observing recomputed metrics below.
    choice = "C. STOP_TUNING_AND_COMPLETE_30_DAY_REPLAY"
    decision_basis = "Approximately 100 reviewed cases now provide a useful, though selected, QA sample. Precision/recall and failure categories remain descriptive; the principal remaining formal blocker is completion/integrity of the 30-day replay (including Sep 22 readiness). Do not implement a fix from this packet; finish the replay and use remaining labels for targeted follow-up if needed."

    OUT.mkdir(parents=True, exist_ok=True)
    write_csv(OUT / "merged_ground_truth_market.csv", [r for r in fresh if r["scope"] == "MARKET"])
    write_csv(OUT / "merged_ground_truth_stock.csv", [r for r in fresh if r["scope"] == "STOCK"])
    write_csv(OUT / "current_model_verdicts.csv", fresh)
    write_csv(OUT / "wave2_error_taxonomy.csv", errors)

    sep22 = day_map.get("2026-09-22") or {}
    payload = {
        "source_replay": str(REPLAY.relative_to(ROOT)),
        "before_after": {"before_wave2": {"reviewed_rows": 75, "market_rows": len(base_market), "stock_rows": len(base_stock), "metrics": before_metrics}, "after_wave2": {"reviewed_rows": len(fresh), "market_rows": sum(r["scope"] == "MARKET" for r in fresh), "stock_rows": sum(r["scope"] == "STOCK" for r in fresh), "remaining_review_required": remaining_review_required, "metrics": after_metrics}},
        "wave2_only": {"market": wave_metrics["MARKET"], "stock": wave_metrics["STOCK"], "combined": wave_metrics["COMBINED"], "error_root_causes": dict(root_counts)},
        "human_driver_type_breakdown": truth_breakdown,
        "model_explanation_state_breakdown": model_breakdown,
        "wave2_market_discovery_validation": {"wave1": discovery_wave1, "wave2": discovery_wave2},
        "generalization_assessment": assessment,
        "next_step_recommendation": {"choice": choice, "basis": decision_basis, "action_scope": "Analysis recommendation only; no product changes made."},
        "data_integrity": {"wave2_rows_imported": len(wave), "wave2_market_rows": EXPECTED_SCOPE["MARKET"], "wave2_stock_rows": EXPECTED_SCOPE["STOCK"], "duplicate_reviewed_rows": 0, "human_adjudication_values_changed": len(human_changes), "baseline_replay_parity_checks": model_parity_checks, "replay_stock_rows_checked": sum(r["scope"] == "STOCK" for r in fresh), "latest_replay_used": str(REPLAY.relative_to(ROOT)), "sep_22_reviewed": any(r["date"] == "2026-09-22" for r in fresh), "sep_22_current_replay_state": {"status": sep22.get("status", "NOT_IN_REPLAY"), "missing_components": sep22.get("missing_components", []), "integrity": sep22.get("integrity", {})}, "live_api_calls": 0, "cache_writes": 0, "production_files_changed": 0},
    }
    (OUT / "metrics_before_after.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def metric_table(scope: str) -> str:
        b, a = before_metrics[scope], after_metrics[scope]
        return f"| {scope} | {b['reviewed_rows']} | {strict_rate(b['precision_like'])} | {strict_rate(b['recall_like'])} | {b['false_positives']} | {b['false_negatives']} | {b['scope_mismatches']} | {a['reviewed_rows']} | {strict_rate(a['precision_like'])} | {strict_rate(a['recall_like'])} | {a['false_positives']} | {a['false_negatives']} | {a['scope_mismatches']} |"

    md = ["# Ground-Truth V2 + Wave 2 metric recalculation", "", "QA-only merge of the exact human adjudications and recalculation against the frozen latest 29-day offline replay. No human labels were inferred or edited.", "", "## Merge counts", "", f"Reviewed after Wave 2: **{len(fresh)}** ({sum(r['scope']=='MARKET' for r in fresh)} market, {sum(r['scope']=='STOCK' for r in fresh)} stock); Wave 2 added 25 (9/16). Remaining review-required: **{remaining_review_required}**.", "", "## Before / after Wave 2", "", "| Scope | Before rows | Precision before | Recall before | FP before | FN before | Scope mismatch before | After rows | Precision after | Recall after | FP after | FN after | Scope mismatch after |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|", *[metric_table(s) for s in ("MARKET", "STOCK", "COMBINED")], "", f"Wave 2 only — market: TP {wave_metrics['MARKET']['true_positives']}, FP {wave_metrics['MARKET']['false_positives']}, FN {wave_metrics['MARKET']['false_negatives']}, scope mismatch {wave_metrics['MARKET']['scope_mismatches']}, unclear {wave_metrics['MARKET']['unclear']}; precision {strict_rate(wave_metrics['MARKET']['precision_like'])}, recall {strict_rate(wave_metrics['MARKET']['recall_like'])}.", f"Wave 2 only — stock: TP {wave_metrics['STOCK']['true_positives']}, FP {wave_metrics['STOCK']['false_positives']}, FN {wave_metrics['STOCK']['false_negatives']}, scope mismatch {wave_metrics['STOCK']['scope_mismatches']}, unclear {wave_metrics['STOCK']['unclear']}; precision {strict_rate(wave_metrics['STOCK']['precision_like'])}, recall {strict_rate(wave_metrics['STOCK']['recall_like'])}.", "", "## Wave 2 error root causes", "", f"Wave 2 errors: FP={error_counts['FALSE_POSITIVE']}, FN={error_counts['FALSE_NEGATIVE']}, scope mismatch={error_counts['SCOPE_MISMATCH']}. Root categories: `{dict(root_counts)}`.", "", "## Market discovery validation", "", f"Wave 1: `{json.dumps(discovery_wave1, ensure_ascii=False)}`", f"Wave 2: `{json.dumps(discovery_wave2, ensure_ascii=False)}`", "Cache-presence indicators are availability diagnostics only; they do not prove that cached articles are relevant to a human-supported driver.", "", "## Generalization assessment", "", f"Stock precision-like {'remains' if precision_mid_high else 'does not remain'} in the mid/high-80% range ({strict_rate(after_metrics['STOCK']['precision_like'])}); stock recall-like is {strict_rate(after_metrics['STOCK']['recall_like'])} ({'around 60%' if recall_around_60 else 'outside the 55–65% band'}). Market recall-like is {strict_rate(after_metrics['MARKET']['recall_like'])}, versus stock {strict_rate(after_metrics['STOCK']['recall_like'])}; market remains the larger recall weakness: {market_is_largest_recall_gap}.", f"Market discovery had zero candidates in {discovery_wave2['zero_market_discovery_candidates']}/{discovery_wave2['market_rows']} Wave 2 market cases; Wave 1 had {discovery_wave1['zero_market_discovery_candidates']}/{discovery_wave1['market_rows']}. This pattern recurs in the selected sample, but the non-random design and cache-presence-only flags limit generalization.", f"Wave 2 stock error counts: new FP {stock_wave_counts['FALSE_POSITIVE']}, FN {stock_wave_counts['FALSE_NEGATIVE']}, scope mismatch {stock_wave_counts['SCOPE_MISMATCH']}; wrong-event selections {wrong_event}, cached-but-rejected FN {cached_rejected}, inheritance mistakes {inheritance_errors}.", "No single repeated error category is treated as a proven generalizable fix target. Wave 2 is curated, not a random sample.", "", "## Breakdown by human driver type", "", f"`{json.dumps(truth_breakdown, ensure_ascii=False)}`", "", "## Breakdown by model explanation state", "", f"`{json.dumps(model_breakdown, ensure_ascii=False)}`", "", "## Decision", "", f"**{choice}** — {decision_basis}", ""]
    (OUT / "metrics_before_after.md").write_text("\n".join(md), encoding="utf-8")
    gen_md = ["# Generalization assessment", "", f"A. Stock precision-like is {strict_rate(after_metrics['STOCK']['precision_like'])}; {'yes, approximately mid-80s, down modestly from the 85.7% baseline' if precision_mid_high else 'no, below the mid-80s range'}.", f"B. Stock recall-like is {strict_rate(after_metrics['STOCK']['recall_like'])}; {'it remains around 60%' if recall_around_60 else 'it moved outside the 55–65% band'}.", f"C. Market recall remains the larger measured weakness: market {strict_rate(after_metrics['MARKET']['recall_like'])}, stock {strict_rate(after_metrics['STOCK']['recall_like'])}.", f"D. Empty market-discovery candidate sets recur in Wave 1 ({discovery_wave1['zero_market_discovery_candidates']}/{discovery_wave1['market_rows']}) and Wave 2 ({discovery_wave2['zero_market_discovery_candidates']}/{discovery_wave2['market_rows']}); systemic behavior is plausible in these selected samples, not established population-wide.", f"E. Repeated Wave 2 error roots (2+ cases): `{dict(repeated_roots)}`. No fix is proposed without validating pipeline-stage evidence.", f"F. The earlier suspected market-discovery emptiness did not disappear in Wave 2. Cache presence elsewhere is not proof of relevant alternate-pool evidence.", "", f"Stock FP/FN/scope mismatch in Wave 2: {stock_wave_counts['FALSE_POSITIVE']}/{stock_wave_counts['FALSE_NEGATIVE']}/{stock_wave_counts['SCOPE_MISMATCH']}; wrong-event selection={wrong_event}, cached-but-rejected={cached_rejected}, inheritance mistakes={inheritance_errors}.", "", "The Wave 2 set was intentionally selected, so these metrics are descriptive rather than unbiased estimates.", ""]
    (OUT / "generalization_assessment.md").write_text("\n".join(gen_md), encoding="utf-8")
    rec_md = ["# Next-step recommendation", "", f"**{choice}**", "", decision_basis, "", "No product fix or further labeling is performed by this task.", ""]
    (OUT / "next_step_recommendation.md").write_text("\n".join(rec_md), encoding="utf-8")
    print(json.dumps({"reviewed": len(fresh), "market": sum(r["scope"] == "MARKET" for r in fresh), "stock": sum(r["scope"] == "STOCK" for r in fresh), "after_metrics": after_metrics, "wave2_errors": {v: error_counts[v] for v in VERDICTS}, "wave2_root_causes": dict(root_counts), "decision": choice, "output_dir": str(OUT)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
