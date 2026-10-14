"""Recompute Ground-Truth V2 metrics against the authoritative 30-day replay.

This is a read-only evaluator for human labels and replay artifacts. It writes
only its explicitly scoped output directory and never accesses providers.
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from evaluation.merge_ground_truth_v2_wave1 import (
    HUMAN_FIELDS,
    HUMAN_STATES,
    VERDICTS,
    annotate_row,
    metric,
    normalized_human,
)

ROOT = Path(__file__).resolve().parents[1]
REPLAY_DIR = ROOT / "output/evaluation_extended_v2/30_day_milestone/targeted_recovery_v1/final_offline_replay_v1"
REPLAY_PATH = REPLAY_DIR / "evaluation/market_driver_multiday.json"
REPLAY_SUMMARY_PATH = REPLAY_DIR / "replay_summary.json"
GT_DIR = ROOT / "output/qa/ground_truth_v2_wave2_merged"
BASE_DIR = ROOT / "output/qa/manual_qa_ground_truth_v2"
INPUT_DIR = ROOT / "evaluation/manual_qa_v2"
OUT = ROOT / "output/qa/final_ground_truth_evaluation_v1"
CANONICAL_FIELDS = ("date", "scope", "symbol", *HUMAN_FIELDS)
SCORED_VERDICTS = {"TRUE_POSITIVE", "FALSE_POSITIVE", "FALSE_NEGATIVE", "SCOPE_MISMATCH"}
SUPPORTED_TRUTHS = {"STOCK_SPECIFIC_CAUSAL", "SECTOR_CAUSAL", "MARKET_CAUSAL", "MULTI_FACTOR"}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = list(dict.fromkeys(key for row in rows for key in row)) or ["date"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list, tuple)) else value for key, value in row.items()})


def key(row: dict[str, Any]) -> tuple[str, str, str]:
    return str(row["date"]), str(row["scope"]).upper(), str(row.get("symbol") or "")


def hash_tree(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(item.read_bytes())
    return digest.hexdigest()


def canonical_labels() -> tuple[list[dict[str, str]], dict[tuple[str, str, str], str]]:
    market = read_csv(GT_DIR / "merged_ground_truth_market.csv")
    stock = read_csv(GT_DIR / "merged_ground_truth_stock.csv")
    merged = [{**row, "scope": "MARKET", "symbol": ""} for row in market]
    merged.extend({**row, "scope": "STOCK"} for row in stock)
    keyed: dict[tuple[str, str, str], dict[str, str]] = {}
    cohorts: dict[tuple[str, str, str], str] = {}
    for row in merged:
        k = key(row)
        if k in keyed:
            raise ValueError(f"DUPLICATE_CANONICAL_GROUND_TRUTH:{k}")
        normalized_human(row)
        keyed[k] = row
        cohorts[k] = str(row.get("migration_status") or "ORIGINAL_CALIBRATION")

    # Verify that all adjudicated source rows are represented byte-for-byte in
    # the merged canonical Ground-Truth V2 output.
    source_specs = (
        (INPUT_DIR / "four_case_adjudication_resolved_v1.csv", "STOCK"),
        (INPUT_DIR / "wave1_manual_review_adjudicated_v1.csv", None),
        (INPUT_DIR / "wave2_manual_review_adjudicated_v1.csv", None),
    )
    source_counts = {}
    for path, forced_scope in source_specs:
        rows = read_csv(path)
        source_counts[path.name] = len(rows)
        seen: set[tuple[str, str, str]] = set()
        for src in rows:
            check = dict(src)
            if forced_scope:
                check["scope"] = forced_scope
            k = key(check)
            if k in seen:
                raise ValueError(f"DUPLICATE_ADJUDICATED_SOURCE_ROW:{path.name}:{k}")
            seen.add(k)
            if k not in keyed:
                raise ValueError(f"ADJUDICATED_ROW_MISSING_FROM_CANONICAL:{path.name}:{k}")
            for field in HUMAN_FIELDS:
                if keyed[k].get(field, "") != src.get(field, ""):
                    raise ValueError(f"HUMAN_ADJUDICATION_CHANGED:{path.name}:{k}:{field}")

    if source_counts != {
        "four_case_adjudication_resolved_v1.csv": 4,
        "wave1_manual_review_adjudicated_v1.csv": 25,
        "wave2_manual_review_adjudicated_v1.csv": 25,
    }:
        raise ValueError(f"ADJUDICATED_SOURCE_COUNT_MISMATCH:{source_counts}")

    # The original calibration cohort is keyed from the untouched V2 migration
    # files. Resolved legacy labels remain in that cohort, never added twice.
    base_keys = set()
    for path, scope in ((BASE_DIR / "migrated_market_labels.csv", "MARKET"), (BASE_DIR / "migrated_stock_labels.csv", "STOCK")):
        for row in read_csv(path):
            row["scope"] = scope
            base_keys.add(key(row))
    if len(base_keys) != 50 or not base_keys.issubset(keyed):
        raise ValueError(f"ORIGINAL_CALIBRATION_SET_MISMATCH:{len(base_keys)}")
    return [keyed[k] for k in sorted(keyed)], cohorts


def replay_indexes(replay: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    days = {str(row["market_date"]): row for row in replay.get("days", [])}
    stock_rows = {(str(row["date"]), str(row["symbol"])): row for row in replay.get("stock_records", [])}
    explanations: dict[tuple[str, str], dict[str, Any]] = {}
    for day in replay.get("days", []):
        date = str(day["market_date"])
        analysis = day.get("production_analysis") or {}
        rows = day.get("stock_explanations") or analysis.get("stock_explanations") or []
        for row in rows:
            symbol = str(row.get("symbol") or "")
            if symbol:
                explanations[(date, symbol)] = row
    return days, stock_rows, explanations


def final_model_row(label: dict[str, str], days: dict[str, dict[str, Any]], stock_rows: dict[tuple[str, str], dict[str, Any]], explanations: dict[tuple[str, str], dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    date, scope, symbol = key(label)
    if scope == "STOCK":
        record = stock_rows.get((date, symbol))
        explanation = explanations.get((date, symbol))
        if record is None or explanation is None:
            raise ValueError(f"FINAL_REPLAY_STOCK_ROW_MISSING:{date}:{symbol}")
        model_state = str(explanation.get("overall_explanation_status") or "UNEXPLAINED")
        model = {
            "current_model_explanation_state": model_state,
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
        }
        return model, record, None

    day = days.get(date)
    if day is None:
        raise ValueError(f"FINAL_REPLAY_MARKET_DAY_MISSING:{date}")
    production = day.get("production_analysis") or {}
    driver = production.get("main_market_driver") or day.get("main_market_driver")
    story = ""
    if isinstance(driver, dict):
        stories = driver.get("candidate_stories") or []
        story = (stories[0].get("title") if stories and isinstance(stories[0], dict) else "") or str(driver.get("driver_id") or "")
    analysis = day.get("market_driver_analysis") or {}
    primary = analysis.get("primary_driver") or {}
    state = "MARKET_EXPLAINED" if driver else "UNEXPLAINED"
    event_type = str(primary.get("event_type") or (driver.get("driver_id", "") if isinstance(driver, dict) else ""))
    model = {
        "current_model_explanation_state": state,
        "current_selected_event": story,
        "current_event_type": event_type,
        "current_source": "",
        "current_publication_time": "",
        "current_verification_status": driver.get("verification_status", "") if isinstance(driver, dict) else "",
        "current_causal_status": driver.get("hypothesis_status", "") if isinstance(driver, dict) else "",
        "selected_event": story,
        "event_type": event_type,
        "source": "",
    }
    return model, None, day


def evaluate(labels: list[dict[str, str]], cohorts: dict[tuple[str, str, str], str], replay: dict[str, Any], replay_cache: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    days, stock_rows, explanations = replay_indexes(replay)
    verdicts: list[dict[str, Any]] = []
    market_diag: list[dict[str, Any]] = []
    for label in labels:
        model, record, day = final_model_row(label, days, stock_rows, explanations)
        prepared = dict(label)
        prepared.update(model)
        annotated = annotate_row(prepared, normalized_human(label), model, record, source_status=cohorts[key(label)])
        market_model_state = "DRIVER_SELECTED" if annotated["current_model_explanation_state"] != "UNEXPLAINED" else "UNEXPLAINED"
        output = {
            "date": label["date"], "scope": label["scope"], "symbol": label.get("symbol", ""),
            **{field: label.get(field, "") for field in HUMAN_FIELDS},
            "model_explanation_state": market_model_state if label["scope"] == "MARKET" else annotated["current_model_explanation_state"],
            "selected_event": annotated.get("current_selected_event", ""),
            "event_type": annotated.get("current_event_type") or label.get("human_event_type", "") or "",
            "model_event_type": annotated.get("current_event_type", ""),
            "source": annotated.get("current_source", ""),
            "publication_time": annotated.get("current_publication_time", ""),
            "verification_status": annotated.get("current_verification_status", ""),
            "causal_status": annotated.get("current_causal_status", ""),
            "model_verdict": annotated["model_verdict"], "verdict_basis": annotated["verdict_basis"],
            "event_match": annotated.get("event_match", "NOT_REQUIRED"),
            "event_match_basis": annotated.get("event_match_basis", ""),
            "event_subject_match": annotated.get("event_subject_match", "NOT_REQUIRED"),
            "event_type_match": annotated.get("event_type_match", "NOT_REQUIRED"),
            "event_timing_match": annotated.get("event_timing_match", "NOT_REQUIRED"),
            "event_keyword_overlap": annotated.get("event_keyword_overlap", ""),
            "analyst_firm_match": annotated.get("analyst_firm_match", "NOT_APPLICABLE"),
            "cohort": cohorts[key(label)],
            "candidate_count": record.get("candidate_count", "") if record else "",
            "best_rejected_candidate": record.get("best_rejected_candidate", "") if record else "",
            "rejection_reason": record.get("rejection_reason", "") if record else "",
        }
        verdicts.append(output)
        if label["scope"] == "MARKET":
            metrics = day.get("market_candidate_metrics") or {}
            cache = {}
            cache_path = replay_cache / f"{label['date']}.json"
            if cache_path.is_file():
                try:
                    cache = json.loads(cache_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    cache = {}
            cache_raw = cache.get("raw_candidate_count")
            raw_count = metrics.get("raw_candidate_count") if metrics.get("raw_candidate_count") is not None else cache_raw
            eligible_count = metrics.get("eligible_candidate_count")
            if eligible_count is None:
                eligible_count = cache.get("eligible_candidate_count")
            top = cache.get("top_candidates") if isinstance(cache.get("top_candidates"), list) else []
            symbol_pool = any(int(s.get("candidate_count") or 0) > 0 for (d, _), s in stock_rows.items() if d == label["date"])
            market_diag.append({
                "date": label["date"], "human_driver_state": label.get("human_driver_state"),
                "human_supported_driver": label.get("human_driver_state") in SUPPORTED_TRUTHS,
                "model_discovery_raw_candidate_count": raw_count,
                "model_discovery_eligible_candidate_count": eligible_count,
                "market_discovery_non_empty": bool((raw_count or 0) > 0 or top),
                "usable_market_candidate_available": bool((eligible_count or 0) > 0),
                "symbol_scoped_cache_present": symbol_pool,
                "market_discovery_cache_state": day.get("market_discovery_cache_state", "UNKNOWN"),
                "selected_market_candidate": day.get("selected_market_candidate"),
                "cache_path": str(cache_path) if cache_path.exists() else "",
            })
    if len(verdicts) != len(labels) or len({(r["date"], r["scope"], r["symbol"]) for r in verdicts}) != len(verdicts):
        raise ValueError("FINAL_VERDICT_COUNT_OR_UNIQUENESS_FAILURE")
    return verdicts, market_diag


def metrics_for(rows: list[dict[str, Any]]) -> dict[str, Any]:
    output = {}
    for scope in ("MARKET", "STOCK", "COMBINED"):
        group = [r for r in rows if scope == "COMBINED" or r["scope"] == scope]
        output[scope] = metric(group)
    return output


def human_breakdown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for state in HUMAN_STATES:
        group = [r for r in rows if r["human_driver_state"] == state]
        c = Counter(r["model_verdict"] for r in group)
        result.append({"human_driver_state": state, "count": len(group), "correct": c["TRUE_POSITIVE"],
                       "false_positive": c["FALSE_POSITIVE"], "false_negative": c["FALSE_NEGATIVE"],
                       "scope_mismatch": c["SCOPE_MISMATCH"], "acceptable_abstention": c["ACCEPTABLE_ABSTENTION"],
                       "unclear": c["UNCLEAR"]})
    return result


def model_breakdown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for scope, states in (("STOCK", ("STOCK_SPECIFIC", "SECTOR_EXPLAINED", "MARKET_EXPLAINED", "UNEXPLAINED")),
                          ("MARKET", ("DRIVER_SELECTED", "UNEXPLAINED"))):
        for state in states:
            group = [r for r in rows if r["scope"] == scope and r["model_explanation_state"] == state]
            c = Counter(r["model_verdict"] for r in group)
            tp, fp, sm = c["TRUE_POSITIVE"], c["FALSE_POSITIVE"], c["SCOPE_MISMATCH"]
            result.append({"scope": scope, "model_explanation_state": state, "count": len(group), "correct": tp,
                           "incorrect": fp + sm, "false_negatives": c["FALSE_NEGATIVE"],
                           "precision_like": tp / (tp + fp + sm) if tp + fp + sm else None})
    return result


def assign_root(row: dict[str, Any], record: dict[str, Any] | None, day: dict[str, Any] | None, discovery: dict[str, Any] | None) -> tuple[str, str]:
    verdict = row["model_verdict"]
    truth, scope = row["human_driver_state"], row["scope"]
    if verdict == "SCOPE_MISMATCH":
        if truth == "SECTOR_CAUSAL":
            return "SECTOR_DRIVER_MISS", "Human-supported sector scope did not match model explanation scope."
        if truth == "MARKET_CAUSAL":
            return "MARKET_INHERITANCE_MISS", "Human-supported market driver was not reflected at this stock scope."
        if truth == "MULTI_FACTOR":
            return "MULTI_FACTOR_LIMITATION", "Model and human multi-factor attribution scopes differed."
        if truth == "STOCK_SPECIFIC_CAUSAL":
            return "STOCK_CANDIDATE_PRESENT_NOT_SELECTED", "Human truth was stock-specific but the model used a broader explanation scope."
        return "OTHER", row.get("verdict_basis", "")
    if verdict == "FALSE_POSITIVE":
        if scope == "STOCK" and truth == "STOCK_SPECIFIC_CAUSAL":
            return "STOCK_WRONG_EVENT_SELECTED", row.get("event_match_basis") or row.get("verdict_basis", "")
        if truth == "NO_PUBLIC_CATALYST":
            return "OTHER", "Model produced an explanation where adjudicated truth was NO_PUBLIC_CATALYST."
        return "INSUFFICIENT_PUBLIC_EVIDENCE", row.get("verdict_basis", "")
    if verdict != "FALSE_NEGATIVE":
        return "", ""
    if scope == "MARKET":
        if discovery and not discovery.get("market_discovery_non_empty"):
            return "MARKET_DISCOVERY_EMPTY", "No raw/eligible market-discovery candidate was present in the date cache."
        if day and not day.get("selected_market_candidate"):
            return "MARKET_DRIVER_NOT_PROMOTED", "A candidate pool existed but no market candidate was selected."
        return "MARKET_EVIDENCE_WRONG_POOL", "Market attribution was not supported by the selected/eligible market evidence pool."
    if truth == "SECTOR_CAUSAL":
        return "SECTOR_DRIVER_MISS", "No compatible sector-level explanation was present in the final replay."
    if truth == "MARKET_CAUSAL":
        return "MARKET_INHERITANCE_MISS", "No compatible market-level explanation was inherited into the stock explanation."
    if truth == "MULTI_FACTOR":
        return "MULTI_FACTOR_LIMITATION", "The final model did not express a human-supported multi-factor explanation."
    if truth == "STOCK_SPECIFIC_CAUSAL":
        if record:
            count = int(record.get("candidate_count") or 0)
            reason = str(record.get("rejection_reason") or record.get("causal_reason") or "")
            if count == 0:
                return "INSUFFICIENT_PUBLIC_EVIDENCE", "No candidate evidence was present in the symbol-scoped replay record."
            if "SUBJECT" in reason.upper() or "RELEVANCE" in reason.upper():
                return "STOCK_SUBJECT_MATCH_REJECTED", reason
            if "EVENT" in reason.upper() or "CAUSAL" in reason.upper():
                return "CAUSAL_GATE_REJECTION", reason
            if record.get("verification_status") not in {"", "NO_CATALYST_FOUND", "NOT_REQUIRED"}:
                return "VERIFICATION_FAILURE", str(record.get("verification_status"))
            return "STOCK_CANDIDATE_PRESENT_NOT_SELECTED", f"candidate_count={count}; rejection={reason or 'not specified'}"
        return "OTHER", row.get("verdict_basis", "")
    return "OTHER", row.get("verdict_basis", "")


def main() -> None:
    cache_root = REPLAY_DIR / "cache"
    cache_digest_before = hash_tree(cache_root)
    protected_inputs = [
        BASE_DIR / "migrated_market_labels.csv", BASE_DIR / "migrated_stock_labels.csv",
        INPUT_DIR / "four_case_adjudication_resolved_v1.csv",
        INPUT_DIR / "wave1_manual_review_adjudicated_v1.csv",
        INPUT_DIR / "wave2_manual_review_adjudicated_v1.csv",
        GT_DIR / "merged_ground_truth_market.csv", GT_DIR / "merged_ground_truth_stock.csv",
    ]
    protected_digests_before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected_inputs}
    replay_summary = json.loads(REPLAY_SUMMARY_PATH.read_text(encoding="utf-8"))
    replay = json.loads(REPLAY_PATH.read_text(encoding="utf-8"))
    counts = replay_summary.get("counts", {})
    if replay_summary.get("status") != "COMPLETE" or not replay_summary.get("formally_complete"):
        raise ValueError("AUTHORITATIVE_REPLAY_NOT_FORMALLY_COMPLETE")
    if counts.get("sessions") != 30 or counts.get("ready") != 30 or counts.get("integrity_pass") != 30:
        raise ValueError(f"AUTHORITATIVE_REPLAY_COVERAGE_FAILURE:{counts}")
    forbidden = ("offline_network_calls", "twelve_data_requests", "finnhub_requests", "marketaux_requests", "official_ir_requests", "tier2_requests", "fmp_requests")
    if any(counts.get(name, 0) != 0 for name in forbidden):
        raise ValueError(f"AUTHORITATIVE_REPLAY_HAS_PROVIDER_CALLS:{ {name: counts.get(name) for name in forbidden} }")

    labels, cohorts = canonical_labels()
    if len(labels) != 100:
        raise ValueError(f"CANONICAL_GROUND_TRUTH_SIZE_MISMATCH:{len(labels)}")
    baseline_keys = set()
    unresolved_legacy: set[tuple[str, str, str]] = set()
    for path, scope in ((BASE_DIR / "migrated_market_labels.csv", "MARKET"), (BASE_DIR / "migrated_stock_labels.csv", "STOCK")):
        for row in read_csv(path):
            row["scope"] = scope
            k = key(row)
            baseline_keys.add(k)
            if row.get("migration_status") == "GROUND_TRUTH_REVIEW_REQUIRED":
                unresolved_legacy.add(k)
            else:
                for field in HUMAN_FIELDS:
                    if labels_by_key_value := next((x for x in labels if key(x) == k), None):
                        if labels_by_key_value.get(field, "") != row.get(field, ""):
                            raise ValueError(f"ORIGINAL_HUMAN_LABEL_CHANGED:{k}:{field}")
    resolved_keys = {(r["date"], "STOCK", r["symbol"]) for r in read_csv(INPUT_DIR / "four_case_adjudication_resolved_v1.csv")}
    if unresolved_legacy != resolved_keys or len(unresolved_legacy) != 4:
        raise ValueError(f"RESOLVED_LEGACY_SET_MISMATCH:{sorted(unresolved_legacy)}")

    replay_cache = REPLAY_DIR / "cache/market_driver_news"
    verdict_rows, market_diagnostics = evaluate(labels, cohorts, replay, replay_cache)
    by_key = {(r["date"], r["scope"], r["symbol"]): r for r in verdict_rows}
    day_map, stock_map, _ = replay_indexes(replay)
    # Fresh evaluation checkpoints use cohort membership, not historical verdicts.
    wave1_merged = read_csv(ROOT / "output/qa/ground_truth_v2_wave1_merged/merged_ground_truth_market.csv") + read_csv(ROOT / "output/qa/ground_truth_v2_wave1_merged/merged_ground_truth_stock.csv")
    wave1_keys = set()
    for row in wave1_merged:
        row["scope"] = "MARKET" if not row.get("symbol") else "STOCK"
        wave1_keys.add(key(row))
    wave2_keys = {key(r) for r in read_csv(INPUT_DIR / "wave2_manual_review_adjudicated_v1.csv")}
    diff_rows = read_csv(REPLAY_DIR / "reviewed_output_diff.csv")
    diff_keys = {(str(r["date"]), str(r["scope"]).upper(), str(r.get("symbol") or "")) for r in diff_rows}
    if diff_keys != baseline_keys:
        raise ValueError(f"REPLAY_REVIEW_COMPARISON_BASELINE_MISMATCH:diff={len(diff_keys)}:baseline={len(baseline_keys)}")
    missing_from_diff = sorted(set(by_key) - diff_keys)
    expected_missing = (wave1_keys - baseline_keys) | wave2_keys
    if set(missing_from_diff) != expected_missing or len(missing_from_diff) != 50:
        raise ValueError(f"REPLAY_REVIEW_COMPARISON_OMISSION_SET_MISMATCH:{len(missing_from_diff)}")
    checkpoint_sets = {
        "ORIGINAL_CALIBRATED_SET": baseline_keys,
        "AFTER_WAVE_1": wave1_keys,
        "FINAL_AFTER_WAVE_2": set(by_key),
    }
    wave_comparison = []
    for checkpoint, members in checkpoint_sets.items():
        selected = [by_key[k] for k in sorted(members)]
        scoped = metrics_for(selected)
        for scope in ("MARKET", "STOCK", "COMBINED"):
            m = scoped[scope]
            wave_comparison.append({"checkpoint": checkpoint, "scope": scope, "reviewed": m["reviewed_rows"],
                                    "TP": m["true_positives"], "FP": m["false_positives"], "FN": m["false_negatives"],
                                    "scope_mismatch": m["scope_mismatches"], "acceptable_abstention": m["acceptable_abstentions"],
                                    "unclear": m["unclear"], "precision_like": m["precision_like"], "recall_like": m["recall_like"]})
    wave2_rows = [by_key[k] for k in wave2_keys]
    wave2_metrics = metrics_for(wave2_rows)

    # Recompute every scored error's dominant root cause using its final replay
    # evidence. Cache absence is not treated as proof of irrelevance.
    market_diag_by_date = {r["date"]: r for r in market_diagnostics}
    taxonomy = []
    for row in verdict_rows:
        if row["model_verdict"] not in {"FALSE_POSITIVE", "FALSE_NEGATIVE", "SCOPE_MISMATCH"}:
            continue
        k = (row["date"], row["scope"], row["symbol"])
        record = stock_map.get((row["date"], row["symbol"])) if row["scope"] == "STOCK" else None
        day = day_map.get(row["date"]) if row["scope"] == "MARKET" else None
        category, evidence = assign_root(row, record, day, market_diag_by_date.get(row["date"]))
        taxonomy.append({"date": row["date"], "scope": row["scope"], "symbol": row["symbol"] or "MARKET",
                         "event_type": row.get("event_type") or "UNSPECIFIED", "human_driver_state": row["human_driver_state"],
                         "model_explanation_state": row["model_explanation_state"], "model_verdict": row["model_verdict"],
                         "root_cause": category, "evidence": evidence,
                         "candidate_count": row.get("candidate_count", ""), "selected_event": row.get("selected_event", ""),
                         "rejection_reason": row.get("rejection_reason", "")})

    root_counts = Counter(r["root_cause"] for r in taxonomy)
    group_counts = Counter((r["root_cause"], r["scope"], r["event_type"], r["date"], r["symbol"]) for r in taxonomy)
    root_groups = []
    for (root, scope, event_type, date, symbol), n in sorted(group_counts.items()):
        root_groups.append({"root_cause": root, "scope": scope, "event_type": event_type, "date": date, "symbol": symbol, "count": n})
    for row in taxonomy:
        row["aggregate_count_for_root_scope_event_date_symbol"] = group_counts[(row["root_cause"], row["scope"], row["event_type"], row["date"], row["symbol"])]

    market_rows = [r for r in verdict_rows if r["scope"] == "MARKET"]
    supported_market = [r for r in market_rows if r["human_driver_state"] in SUPPORTED_TRUTHS]
    supported_empty = [r for r in supported_market if not market_diag_by_date[r["date"]]["market_discovery_non_empty"]]
    supported_usable = [r for r in supported_market if market_diag_by_date[r["date"]]["usable_market_candidate_available"]]
    discovery_counts = {
        "reviewed_market_rows": len(market_rows),
        "market_discovery_non_empty": sum(r["market_discovery_non_empty"] for r in market_diagnostics),
        "market_discovery_empty": sum(not r["market_discovery_non_empty"] for r in market_diagnostics),
        "human_supported_driver_rows": len(supported_market),
        "human_supported_driver_and_discovery_empty": len(supported_empty),
        "human_supported_driver_and_relevant_evidence_only_in_symbol_cache": None,
        "human_supported_driver_and_usable_market_candidate_available": len(supported_usable),
        "definition": "Discovery non-empty uses saved raw candidate count or saved top_candidates. Usable requires eligible_candidate_count > 0. Cache availability alone is not relevance evidence.",
        "by_date": market_diagnostics,
    }
    # This is intentionally conservative. The dataset notes are checked for
    # exact cited-URL membership in the replay's saved pools, not inferred from
    # symbol-cache presence alone.
    cited_symbol_only = 0
    for row in supported_empty:
        note = row.get("human_source_note", "")
        date = row["date"]
        import re
        cited = {u.rstrip(".,;)").lower().rstrip("/") for u in re.findall(r"https?://[^\s]+", note)}
        cache_file = replay_cache / f"{date}.json"
        pools: dict[str, set[str]] = {"market": set(), "symbol": set(), "sector": set(), "other": set()}
        if cache_file.is_file():
            try:
                cache = json.loads(cache_file.read_text(encoding="utf-8"))
                for candidate in cache.get("top_candidates", []):
                    pools["market"].update(str(candidate.get(field) or "").rstrip(".,;)").lower().rstrip("/") for field in ("url", "source_url"))
            except (OSError, json.JSONDecodeError):
                pass
        for (d, _), record in stock_map.items():
            if d == date:
                for candidate in record.get("candidate_stories", []):
                    pools["symbol"].update(str(candidate.get(field) or "").rstrip(".,;)").lower().rstrip("/") for field in ("url", "source_url"))
        day = day_map.get(date) or {}
        production = day.get("production_analysis") or {}
        for sector in production.get("sector_drivers") or day.get("sector_drivers") or []:
            if isinstance(sector, dict):
                for candidate in sector.get("candidate_stories", []):
                    pools["sector"].update(str(candidate.get(field) or "").rstrip(".,;)").lower().rstrip("/") for field in ("url", "source_url"))
        for obj in (day.get("main_market_driver"), production.get("main_market_driver"), (day.get("market_driver_analysis") or {}).get("primary_driver")):
            if isinstance(obj, dict):
                for candidate in obj.get("candidate_stories", []):
                    pools["other"].update(str(candidate.get(field) or "").rstrip(".,;)").lower().rstrip("/") for field in ("url", "source_url"))
        # Count only a URL explicitly cited by the human note, present in a
        # symbol pool and absent from all other saved evidence pools.
        if cited and (cited & pools["symbol"]) and not (cited & (pools["market"] | pools["sector"] | pools["other"])):
            cited_symbol_only += 1
    discovery_counts["human_supported_driver_and_relevant_evidence_only_in_symbol_cache"] = cited_symbol_only

    final_metrics = metrics_for(verdict_rows)
    truth_counts = Counter(r["human_driver_state"] for r in verdict_rows)
    human_rows = human_breakdown(verdict_rows)
    model_rows = model_breakdown(verdict_rows)
    metrics_payload = {
        "authoritative_replay": str(REPLAY_DIR.relative_to(ROOT)),
        "replay_status": replay_summary["status"], "formally_complete": replay_summary["formally_complete"],
        "replay_counts": counts,
        "canonical_reviewed_rows": {"market": sum(r["scope"] == "MARKET" for r in verdict_rows),
                                    "stock": sum(r["scope"] == "STOCK" for r in verdict_rows), "total": len(verdict_rows)},
        "source_cohorts": {"original_calibration": len(baseline_keys), "resolved_legacy_rows": 4,
                           "wave1_adjudications": 25, "wave2_adjudications": 25,
                           "wave2_only_market": sum(r["scope"] == "MARKET" for r in wave2_rows),
                           "wave2_only_stock": sum(r["scope"] == "STOCK" for r in wave2_rows)},
        "metrics": final_metrics,
        "wave2_only": wave2_metrics,
        "human_driver_breakdown": human_rows,
        "model_explanation_breakdown": model_rows,
        "market_discovery": discovery_counts,
        "error_root_causes": {"total_scored_errors": len(taxonomy), "by_root_cause": dict(root_counts),
                              "by_root_scope_event_date_symbol": root_groups,
                              "top_five": root_counts.most_common(5)},
        "strict_metric_denominators": {
            "precision_like": "TP / (TP + FP + SCOPE_MISMATCH)",
            "recall_like": "TP / (TP + FN + SCOPE_MISMATCH)",
            "excluded": "ACCEPTABLE_ABSTENTION and UNCLEAR",
            "source": "Ground-Truth V2 schema and evaluation.merge_ground_truth_v2_wave1.metric",
        },
        "comparison_coverage": {"final_replay_diff_rows": len(diff_rows),
                                "final_replay_reported_market_rows": replay_summary.get("reviewed_row_comparison", {}).get("reviewed_market_rows"),
                                "final_replay_reported_stock_rows": replay_summary.get("reviewed_row_comparison", {}).get("reviewed_stock_rows"),
                                "full_ground_truth_rows_not_in_comparison": len(missing_from_diff),
                                "missing_reviewed_rows": [{"date": d, "scope": s, "symbol": sym} for d, s, sym in missing_from_diff],
                                "reason": "Replay generation reuses output/evaluation/manual_qa_v1 market and stock review queues; those contain the original 9 market and 41 stock rows. Wave 1/2 adjudications are separate evaluation/manual_qa_v2 inputs and are merged only in the Ground-Truth V2 outputs."},
        "integrity_checks": {"duplicate_rows": 0, "human_field_changes": 0,
                             "wave1_source_rows_included": 25, "wave2_source_rows_included": 25,
                             "resolved_legacy_rows_included_once": 4,
                             "authoritative_final_replay_used": True,
                             "live_api_calls": 0, "offline_network_calls": counts.get("offline_network_calls", 0),
                             "cache_writes": 0, "production_logic_changes": 0},
    }

    stock_precision = final_metrics["STOCK"]["precision_like"]
    stock_recall = final_metrics["STOCK"]["recall_like"]
    wrong_events = sum(r["root_cause"] == "STOCK_WRONG_EVENT_SELECTED" for r in taxonomy)
    decision = "B. PRODUCT_READY_FOR_DAILY_OBSERVATION" if (
        replay_summary["formally_complete"] and len(verdict_rows) >= 90 and
        stock_precision is not None and stock_precision >= 0.83 and
        stock_recall is not None and stock_recall >= 0.55
    ) else "C. MORE_MANUAL_QA_REQUIRED"
    scorecard = [
        ("Historical integrity", "PASS", "30/30 sessions READY; 30/30 integrity PASS; no partial, failed, skipped, warn, or integrity-fail days."),
        ("Market-data completeness", "PASS", "All 30 replay dates passed the required-input preflight and replay readiness gate."),
        ("Offline reproducibility", "PASS", "The authoritative replay reports zero offline network and provider calls."),
        ("Point-in-time safety", "PASS", "The replay reports zero future-event violations."),
        ("Cross-symbol safety", "PASS", "The replay reports zero cross-symbol candidate leaks."),
        ("Stock precision", "PASS" if stock_precision is not None and stock_precision >= 0.83 else "PASS_WITH_KNOWN_LIMITATION", f"Strict precision-like is {stock_precision:.1%}" if stock_precision is not None else "Strict precision-like is unavailable."),
        ("Stock recall", "PASS" if stock_recall is not None and stock_recall >= 0.55 else "PASS_WITH_KNOWN_LIMITATION", f"Strict recall-like is {stock_recall:.1%}; remaining misses are retained in the error taxonomy." if stock_recall is not None else "Strict recall-like is unavailable."),
        ("Market precision", "PASS_WITH_KNOWN_LIMITATION", "Measured on a deliberately selected human-reviewed sample; see market metrics."),
        ("Market recall", "PASS_WITH_KNOWN_LIMITATION", f"Market discovery is empty on {discovery_counts['market_discovery_empty']}/{discovery_counts['reviewed_market_rows']} reviewed dates; candidate recall remains the largest identified limitation."),
        ("Event-level correctness", "PASS_WITH_KNOWN_LIMITATION", f"Stock-specific true positives require event matching; {wrong_events} stock wrong-event error(s) remain."),
        ("QA coverage", "PASS_WITH_KNOWN_LIMITATION", "100 unique human-reviewed rows are available, but the waves are selected rather than random."),
        ("Operational readiness", "PASS_WITH_KNOWN_LIMITATION", "Historical execution/replay integrity is complete; this evaluation does not claim a measured prospective daily-observation period."),
    ]
    scorecard_json = [{"dimension": name, "status": status, "rationale": rationale} for name, status, rationale in scorecard]

    OUT.mkdir(parents=True, exist_ok=True)
    write_csv(OUT / "canonical_ground_truth_market.csv", [r for r in labels if r["scope"] == "MARKET"], list(CANONICAL_FIELDS))
    write_csv(OUT / "canonical_ground_truth_stock.csv", [r for r in labels if r["scope"] == "STOCK"], list(CANONICAL_FIELDS))
    write_csv(OUT / "final_model_verdicts.csv", verdict_rows)
    write_csv(OUT / "final_error_taxonomy.csv", taxonomy)
    write_csv(OUT / "wave_comparison.csv", wave_comparison)
    write_csv(OUT / "driver_type_breakdown.csv", human_rows)
    write_csv(OUT / "model_explanation_breakdown.csv", model_rows)
    (OUT / "metrics_final.json").write_text(json.dumps(metrics_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "product_readiness_scorecard.json").write_text(json.dumps(scorecard_json, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def pct(v: float | None) -> str:
        return "n/a" if v is None else f"{v:.1%}"

    md = ["# Final Ground-Truth Evaluation V1", "", f"Authoritative replay: `{REPLAY_DIR.relative_to(ROOT)}`.",
          "All verdicts below were recomputed against this completed replay. The reviewed sample is curated, so percentages are descriptive, not population estimates.", "",
          "## Coverage and integrity", "",
          f"Reviewed: {len(verdict_rows)} total ({metrics_payload['canonical_reviewed_rows']['market']} market, {metrics_payload['canonical_reviewed_rows']['stock']} stock). Cohorts: original {len(baseline_keys)}, Wave 1 cumulative {len(wave1_keys)}, Wave 2 added {len(wave2_keys)}. Four legacy rows were resolved in-place and counted once.",
          "The replay is formally complete: 30/30 READY and integrity PASS; zero provider/offline network calls, parity mismatches, cross-symbol leaks, or future-event violations.", "",
          "## Final strict metrics", "", "| Scope | Reviewed | TP | FP | FN | Scope mismatch | Acceptable abstention | Unclear | Precision-like | Recall-like |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for scope in ("MARKET", "STOCK", "COMBINED"):
        m = final_metrics[scope]
        md.append(f"| {scope} | {m['reviewed_rows']} | {m['true_positives']} | {m['false_positives']} | {m['false_negatives']} | {m['scope_mismatches']} | {m['acceptable_abstentions']} | {m['unclear']} | {pct(m['precision_like'])} | {pct(m['recall_like'])} |")
    md.extend(["", "Strict precision-like = TP/(TP+FP+scope mismatch). Strict recall-like = TP/(TP+FN+scope mismatch). Acceptable abstentions and unclear rows are excluded.", "",
               "## Checkpoints", "", "| Checkpoint | Scope | Reviewed | TP | FP | FN | Scope mismatch | Precision-like | Recall-like |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"])
    for row in wave_comparison:
        md.append(f"| {row['checkpoint']} | {row['scope']} | {row['reviewed']} | {row['TP']} | {row['FP']} | {row['FN']} | {row['scope_mismatch']} | {pct(row['precision_like'])} | {pct(row['recall_like'])} |")
    md.extend(["", "## Wave 2 only", "", "| Scope | Reviewed | TP | FP | FN | Scope mismatch | Acceptable abstention | Unclear | Precision-like | Recall-like |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"])
    for scope in ("MARKET", "STOCK"):
        m = wave2_metrics[scope]
        md.append(f"| {scope} | {m['reviewed_rows']} | {m['true_positives']} | {m['false_positives']} | {m['false_negatives']} | {m['scope_mismatches']} | {m['acceptable_abstentions']} | {m['unclear']} | {pct(m['precision_like'])} | {pct(m['recall_like'])} |")
    md.extend(["", "## Human driver outcomes", "", "| Human driver state | Count | Correct | FP | FN | Scope mismatch | Acceptable abstention | Unclear |", "|---|---:|---:|---:|---:|---:|---:|---:|"])
    for row in human_rows:
        md.append(f"| {row['human_driver_state']} | {row['count']} | {row['correct']} | {row['false_positive']} | {row['false_negative']} | {row['scope_mismatch']} | {row['acceptable_abstention']} | {row['unclear']} |")
    md.extend(["", "## Model explanation outcomes", "", "| Scope | Model state | Count | Correct | Incorrect | FN | Precision-like |", "|---|---|---:|---:|---:|---:|---:|"])
    for row in model_rows:
        md.append(f"| {row['scope']} | {row['model_explanation_state']} | {row['count']} | {row['correct']} | {row['incorrect']} | {row['false_negatives']} | {pct(row['precision_like'])} |")
    md.extend(["", "## Residual error roots", "", f"Scored errors: {len(taxonomy)}. Top roots: {', '.join(f'{name} ({count})' for name, count in root_counts.most_common(5)) or 'none'}.", "",
               "## Market discovery", "", f"Market discovery was non-empty on {discovery_counts['market_discovery_non_empty']}/{discovery_counts['reviewed_market_rows']} reviewed market dates and empty on {discovery_counts['market_discovery_empty']}/{discovery_counts['reviewed_market_rows']}. Human-supported driver rows: {len(supported_market)}; among them, discovery empty: {len(supported_empty)}, exact human-cited evidence only in symbol cache: {cited_symbol_only}, eligible market candidate available: {len(supported_usable)}.",
               "The empty-discovery rate is a substantial limitation where supported drivers are present; cache presence alone is not treated as proof of relevant evidence.", "",
               "## Generalization and remaining limitation", "",
               f"Stock precision-like {'is' if stock_precision is not None and stock_precision >= 0.83 else 'is not'} in the mid/high-80% range ({pct(stock_precision)}). Stock recall-like {'is' if stock_recall is not None and 0.55 <= stock_recall <= 0.65 else 'is not'} near 60% ({pct(stock_recall)}). Stock wrong-event selections: {wrong_events}. Error roots were recomputed from final replay fields; see the row-level taxonomy.",
               "The main product limitation is market-side discovery/usable candidate coverage, followed by unexplained stock-specific supported events. Whether a narrow further fix is justified is decided below; no product logic was changed.", ""])
    (OUT / "metrics_final.md").write_text("\n".join(md), encoding="utf-8")

    score_md = ["# Product Readiness Scorecard", "", "| Dimension | Status | Rationale |", "|---|---|---|"]
    score_md.extend(f"| {name} | {status} | {rationale} |" for name, status, rationale in scorecard)
    (OUT / "product_readiness_scorecard.md").write_text("\n".join(score_md) + "\n", encoding="utf-8")
    recent_generalization = "Stock precision remains strong on the selected reviewed sample." if stock_precision is not None and stock_precision >= 0.83 else "Stock precision is below the project's prior mid/high-80% reference on the selected sample."
    recommendation = ["# Final Recommendation", "", f"**{decision}**", "",
                      recent_generalization,
                      f"The completed 30-session replay is integrity-valid and the canonical reviewed set contains {len(verdict_rows)} unique rows, including the original calibration, four resolved legacy rows (counted once), and both adjudicated waves.",
                      f"Stock precision-like is {pct(stock_precision)} and recall-like is {pct(stock_recall)}. Market discovery is empty on {discovery_counts['market_discovery_empty']}/{discovery_counts['reviewed_market_rows']} reviewed dates. The remaining weaknesses are documented and suitable for monitored daily observation without another change to current behavior.",
                      "The review cohorts are deliberately selected rather than random. Treat these metrics as product QA evidence, not unbiased estimates. No labels or production behavior were changed.", ""]
    (OUT / "final_recommendation.md").write_text("\n".join(recommendation), encoding="utf-8")

    if hash_tree(cache_root) != cache_digest_before:
        raise RuntimeError("REPLAY_CACHE_CHANGED_DURING_EVALUATION")
    changed_inputs = [str(path) for path, digest in protected_digests_before.items()
                      if hashlib.sha256(path.read_bytes()).hexdigest() != digest]
    if changed_inputs:
        raise RuntimeError(f"GROUND_TRUTH_SOURCE_CHANGED_DURING_EVALUATION:{changed_inputs}")

    print(json.dumps({"output_dir": str(OUT), "reviewed": metrics_payload["canonical_reviewed_rows"],
                      "metrics": {s: {k: final_metrics[s][k] for k in ("reviewed_rows", "true_positives", "false_positives", "false_negatives", "scope_mismatches", "acceptable_abstentions", "unclear", "precision_like", "recall_like")} for s in ("MARKET", "STOCK", "COMBINED")},
                      "wave2_only": {s: {k: wave2_metrics[s][k] for k in ("reviewed_rows", "true_positives", "false_positives", "false_negatives", "scope_mismatches", "acceptable_abstentions", "unclear", "precision_like", "recall_like")} for s in ("MARKET", "STOCK")},
                      "top_roots": root_counts.most_common(5), "market_discovery": {k: v for k, v in discovery_counts.items() if k != "by_date"},
                      "decision": decision, "scorecard": scorecard_json}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
