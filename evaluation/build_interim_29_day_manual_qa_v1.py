"""Build a cache-only manual-QA package for the 29 completed Phase B dates.

The evaluation runs in a temporary scratch directory. Only the requested QA
artifacts are written into output/qa/interim_29_day_manual_qa_v1; project
provider caches and milestone snapshots are read-only inputs.
"""

from __future__ import annotations

import csv
import json
import shutil
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "output/qa/interim_29_day_manual_qa_v1"
MILESTONE = ROOT / "output/evaluation_extended_v2/30_day_milestone"
RESUME_DIR = MILESTONE / "resume_after_dxy"
AUTO_DIR = RESUME_DIR / "auto_resume"
LABEL_DIR = ROOT / "output/evaluation/manual_qa_v1"
LABEL_FIELDS = {
    "CORRECT_CAUSAL", "PLAUSIBLE_CAUSAL", "FALSE_POSITIVE", "FALSE_NEGATIVE",
    "NO_PUBLIC_CATALYST", "UNCLEAR",
}
PENDING_DATE = "2026-09-22"
PRIORITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({key: _json_value(value) for key, value in row.items()} for row in rows)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _dated_path(scratch_eval: Path, prefix: str, day: str) -> Path:
    return scratch_eval / "history" / day / f"{prefix}.json"


def _copy_readonly_inputs(dates: list[str], scratch_eval: Path) -> None:
    from cache_config import cache_root

    source_history = MILESTONE / "history"
    for day in dates:
        for name in ("market_snapshot.json", "movers_snapshot.json", "market_driver_snapshot.json", "daily_report_data.json"):
            source = source_history / day / name
            if source.is_file():
                target = scratch_eval / "history" / day / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    market_cache = cache_root() / "market_driver_news"
    target_cache = scratch_eval.parent / "cache" / "market_driver_news"
    for day in dates:
        candidates = list(market_cache.glob(f"*{day}*.json")) if market_cache.is_dir() else []
        exact = market_cache / f"{day}.json"
        if exact.is_file() and exact not in candidates:
            candidates.append(exact)
        for source in candidates:
            target = target_cache / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _json_value(value: Any) -> Any:
    return value if isinstance(value, (str, int, float, bool)) or value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else default
    except (TypeError, ValueError):
        return default


def _explanation_source(record: dict[str, Any]) -> str:
    status = str(record.get("overall_explanation_status") or "UNEXPLAINED")
    if record.get("inherited_sector_driver"):
        return "SECTOR_EXPLAINED"
    if record.get("inherited_market_driver"):
        return "MARKET_EXPLAINED"
    if status == "STOCK_SPECIFIC" or record.get("selected_event"):
        return "STOCK_SPECIFIC"
    return "UNEXPLAINED"


def _priority_stock(record: dict[str, Any], is_new: bool) -> str:
    move = abs(_float(record.get("move_pct")))
    source = _explanation_source(record)
    if move >= 6 or (is_new and source == "STOCK_SPECIFIC") or (move >= 4 and source == "UNEXPLAINED"):
        return "HIGH"
    if move >= 4 or record.get("verification_status") == "PLAUSIBLE_NOT_VERIFIED" or source == "SECTOR_EXPLAINED":
        return "MEDIUM"
    return "LOW"


def _priority_market(row: dict[str, Any], is_new: bool) -> str:
    moves = [abs(_float(row.get(f"{symbol}_change_pct"))) for symbol in ("SPY", "QQQ", "DIA", "IWM")]
    if max(moves or [0]) >= 1.0 or (is_new and row.get("market_driver_status") in {"CONFIRMED", "PLAUSIBLE_NOT_VERIFIED"}):
        return "HIGH"
    if row.get("primary_market_driver") or row.get("market_regimes"):
        return "MEDIUM"
    return "LOW"


def _market_rows(days: list[dict[str, Any]], market_labels: dict[str, dict[str, str]], old_market_keys: set[str]) -> list[dict[str, Any]]:
    import build_historical_evaluation_batch as batch

    rows = batch._market_rows(days)
    result = []
    for row in rows:
        day = next(item for item in days if item["market_date"] == row["date"])
        main = day.get("main_market_driver") or {}
        story = (main.get("candidate_stories") or [{}])[0]
        old = market_labels.get(row["date"], {})
        is_new = row["date"] not in old_market_keys
        label = old.get("market_manual_label", old.get("manual_label", ""))
        row.update({
            "sector_breadth_summary": f"up={row.get('breadth_up')}; down={row.get('breadth_down')}",
            "market_regime": row.get("market_regimes", ""),
            "selected_market_driver": row.get("primary_market_driver"),
            "selected_evidence": story.get("title") or story.get("canonical_event_title"),
            "evidence_timestamp": story.get("published_at"),
            "causal_status": main.get("causal_gate") or main.get("causal_status"),
            "explanation_status": "EXPLAINED" if main else "UNEXPLAINED",
            "market_manual_label": label,
            "manual_label": label,
            "manual_note": old.get("review_notes", old.get("manual_note", "")),
            "manual_rationale": old.get("rationale", old.get("review_rationale", "")),
            "source_row_existed": row["date"] in old_market_keys,
            "review_status": "ALREADY_LABELED" if label else ("EXISTING_UNLABELED" if not is_new else "NEW_UNLABELED"),
            "needs_manual_review": not bool(label),
            "priority": _priority_market(row, is_new),
            "gold_label_review_recommended": bool(is_new and (main or row.get("market_driver_status"))),
        })
        result.append(row)
    return result


def _stock_rows(days: list[dict[str, Any]], stock_labels: dict[tuple[str, str], dict[str, str]], old_stock_keys: set[tuple[str, str]]) -> list[dict[str, Any]]:
    import build_historical_evaluation_batch as batch

    base_rows = batch._stock_rows(days)
    result = []
    for row in base_rows:
        move = _float(row.get("move_pct"))
        if abs(move) < 4:
            continue
        key = (str(row["date"]), str(row["symbol"]))
        label_row = stock_labels.get(key, {})
        day = next(item for item in days if item["market_date"] == row["date"])
        record = next(item for item in day.get("stock_records", []) if str(item.get("symbol")) == str(row["symbol"]))
        mover = next((item for group in ("top_gainers", "top_losers") for item in (day.get("_movers", {}).get(group, []) or []) if item.get("symbol") == row["symbol"]), {})
        source_kind = _explanation_source(record)
        is_new = key not in old_stock_keys
        label = label_row.get("manual_label", "")
        selected_event = record.get("selected_event")
        event_type = record.get("event_type")
        candidates = record.get("candidate_stories") or []
        chosen_candidate = next((item for item in candidates if (item.get("canonical_event_title") or item.get("title")) == selected_event), {})
        row.update({
            "sector": mover.get("sector", mover.get("sector_name", "")),
            "current_final_explanation": row.get("overall_explanation_status"),
            "explanation_source": source_kind,
            "selected_event": selected_event,
            "event_type": event_type or chosen_candidate.get("event_type") or chosen_candidate.get("category"),
            "source": record.get("source") or chosen_candidate.get("source_name") or chosen_candidate.get("source"),
            "publication_time": record.get("selected_published_at") or chosen_candidate.get("published_at"),
            "sector_market_inheritance": ";".join(value for value in (row.get("inherited_sector_driver"), row.get("inherited_market_driver")) if value),
            "manual_label": label,
            "manual_note": label_row.get("review_notes", label_row.get("manual_note", "")),
            "manual_rationale": label_row.get("rationale", label_row.get("review_rationale", "")),
            "source_row_existed": key in old_stock_keys,
            "review_status": "ALREADY_LABELED" if label else ("EXISTING_UNLABELED" if not is_new else "NEW_UNLABELED"),
            "needs_manual_review": not bool(label),
            "priority": _priority_stock(record, is_new),
            "gold_label_review_recommended": bool(is_new and (source_kind == "STOCK_SPECIFIC" or record.get("verification_status") == "PLAUSIBLE_NOT_VERIFIED")),
            "why_system_selected_it": (
                f"abs(move_pct)={abs(move):.2f}% ; explanation={source_kind} ; "
                f"verification={record.get('verification_status') or 'NOT_APPLICABLE'}"
            ),
        })
        result.append(row)
    return result


def _diagnostics(days: list[dict[str, Any]], old_stock_keys: set[tuple[str, str]]) -> list[dict[str, Any]]:
    allowed = {"ANALYST_ACTION", "PRODUCT", "REGULATORY", "EARNINGS", "GUIDANCE", "EARNINGS_RESULTS", "GUIDANCE_CHANGE"}
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for day in days:
        for record in day.get("stock_records", []):
            symbol = str(record.get("symbol", ""))
            key = (day["market_date"], symbol)
            move = _float(record.get("move_pct"))
            source_kind = _explanation_source(record)
            candidates = record.get("candidate_stories") or []
            event_candidates = [item for item in candidates if str(item.get("event_type") or item.get("category") or "").upper() in allowed]
            inherited = []
            if record.get("inherited_sector_driver"):
                inherited.append(("SECTOR_INHERITANCE", record.get("inherited_sector_driver")))
            if record.get("inherited_market_driver"):
                inherited.append(("MARKET_INHERITANCE", record.get("inherited_market_driver")))
            diagnostic_specs = [(str(item.get("event_type") or item.get("category") or "OTHER").upper(), item) for item in event_candidates]
            diagnostic_specs.extend(inherited)
            if abs(move) >= 3 and source_kind == "UNEXPLAINED":
                diagnostic_specs.append(("UNEXPLAINED_LARGE_MOVE", {}))
            if key not in old_stock_keys and source_kind == "STOCK_SPECIFIC":
                diagnostic_specs.append(("NEW_STOCK_SPECIFIC", next((item for item in candidates if item.get("causal_eligible")), {})))
            for event_type, item in diagnostic_specs:
                event = item.get("canonical_event_title") or item.get("title") or item.get("event") or event_type
                signature = (day["market_date"], symbol, f"{event_type}:{event}")
                if signature in seen:
                    continue
                seen.add(signature)
                rows.append({
                    "date": day["market_date"], "symbol": symbol, "move_pct": move,
                    "explanation_type": source_kind, "event_type": event_type,
                    "selected_event": event, "source": item.get("source_name") or item.get("source") or record.get("source"),
                    "published_at": item.get("published_at") or record.get("selected_published_at"),
                    "verification_status": record.get("verification_status"),
                    "causal_status": record.get("causal_gate"), "diagnostic_only": True,
                    "manual_label": "", "manual_note": "",
                })
    return sorted(rows, key=lambda item: (item["date"], item["symbol"], item["event_type"], str(item["selected_event"])))


def _metric(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(row.get("manual_label", "")) for row in rows)
    tp = counts["CORRECT_CAUSAL"] + counts["PLAUSIBLE_CAUSAL"]
    fp, fn = counts["FALSE_POSITIVE"], counts["FALSE_NEGATIVE"]
    return {
        "labeled_rows": sum(counts[label] for label in LABEL_FIELDS),
        "eligible_labeled_rows": sum(counts[label] for label in LABEL_FIELDS),
        "label_counts": {label: counts[label] for label in sorted(LABEL_FIELDS)},
        "positive_supported": tp, "false_positives": fp, "false_negatives": fn,
        "precision_like": tp / (tp + fp) if tp + fp else None,
        "recall_like": tp / (tp + fn) if tp + fn else None,
        "no_public_catalyst": counts["NO_PUBLIC_CATALYST"], "unclear": counts["UNCLEAR"],
        "unlabeled_rows": sum(not str(row.get("manual_label", "")).strip() for row in rows),
    }


def _root_cause(scope: str, label: str, row: dict[str, Any]) -> tuple[str, str]:
    """Assign an evidence-driven, provisional cause from current replay state."""
    if scope == "MARKET":
        candidates = int(row.get("eligible_market_candidates") or row.get("market_candidates_raw") or 0)
        if label == "FALSE_NEGATIVE":
            return ("MARKET_PROMOTION_MISS", "current market cache/candidate set exists but no selected driver") if candidates else ("MARKET_DISCOVERY_MISS", "no selected driver and no eligible market candidates")
        return ("MARKET_PROMOTION_FALSE_POSITIVE", "a market driver is currently selected; human label marks its explanation incorrect")
    if label == "FALSE_POSITIVE":
        if row.get("inherited_sector_driver"):
            return "SECTOR_INHERITANCE_FALSE_POSITIVE", "current explanation inherits a sector driver"
        if row.get("inherited_market_driver"):
            return "MARKET_INHERITANCE_FALSE_POSITIVE", "current explanation inherits a market driver"
        if row.get("selected_event"):
            return "STOCK_SELECTION_OR_CAUSAL_FALSE_POSITIVE", "a stock-specific event is currently selected"
        return "EXPLANATION_STATUS_FALSE_POSITIVE", "current explanation has no selected stock event"
    candidates = row.get("candidate_stories") or []
    if candidates:
        reasons = " ".join(str(item.get("causal_reason", "")) for item in candidates).upper()
        if "TIMING" in reasons:
            return "TIMING_ERROR", "current mover-symbol candidates exist but timing is ineligible"
        if "SUBJECT" in reasons or "RELEVANCE" in reasons:
            return "SUBJECT_MATCH_ERROR", "current candidates were rejected at subject relevance"
        return "CANDIDATE_PROMOTION_OR_CAUSAL_GATE_MISS", "current mover-symbol candidates exist but no event is selected"
    if _float(row.get("candidate_count")) > 0:
        reason = str(row.get("causal_reason") or "").upper()
        if "TIMING" in reason:
            return "TIMING_ERROR", "current mover-symbol candidates exist but timing is ineligible"
        if "SUBJECT" in reason or "RELEVANCE" in reason:
            return "SUBJECT_MATCH_ERROR", "current candidates were rejected at subject relevance"
        return "CANDIDATE_PROMOTION_OR_CAUSAL_GATE_MISS", "current mover-symbol candidates exist but no event is selected"
    if row.get("inherited_sector_driver"):
        return "SECTOR_INHERITANCE_FALSE_NEGATIVE", "sector inheritance appears in current explanation despite FN label"
    if row.get("inherited_market_driver"):
        return "MARKET_INHERITANCE_FALSE_NEGATIVE", "market inheritance appears in current explanation despite FN label"
    return "STOCK_DISCOVERY_MISS", "no mover-symbol candidate or selected event in current replay"


def _taxonomy(market_rows: list[dict[str, Any]], stock_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for scope, items in (("MARKET", market_rows), ("STOCK", stock_rows)):
        for item in items:
            label = str(item.get("manual_label", item.get("market_manual_label", ""))).strip()
            if label not in {"FALSE_POSITIVE", "FALSE_NEGATIVE"}:
                continue
            cause, basis = _root_cause(scope, label, item)
            rows.append({"scope": scope, "date": item["date"], "symbol": item.get("symbol", ""), "manual_label": label, "current_explanation": item.get("explanation_source") or item.get("explanation_status"), "selected_event": item.get("selected_event") or item.get("selected_evidence"), "event_type": item.get("event_type", ""), "primary_root_cause": cause, "classification_basis": basis, "confidence": "PROVISIONAL_RULE_BASED"})
    return rows


def _coverage_rows(market_rows: list[dict[str, Any]], stock_rows: list[dict[str, Any]], diagnostics: list[dict[str, Any]], dates: list[str], pending: list[str], market_labels: dict[str, dict[str, str]], stock_labels: dict[tuple[str, str], dict[str, str]], old_market_keys: set[str], old_stock_keys: set[tuple[str, str]]) -> list[dict[str, Any]]:
    out = []
    for scope, rows, labels, old_keys in (("MARKET", market_rows, market_labels, old_market_keys), ("STOCK", stock_rows, stock_labels, old_stock_keys)):
        for row in rows:
            key = row["date"] if scope == "MARKET" else (row["date"], row["symbol"])
            label = row.get("market_manual_label" if scope == "MARKET" else "manual_label", "")
            is_gold = bool(row.get("gold_label_review_recommended"))
            status = "ALREADY_LABELED" if label else ("EXISTING_UNLABELED" if key in old_keys else "NEW_UNLABELED")
            out.append({"date": row["date"], "scope": scope, "symbol": row.get("symbol", ""), "move_pct": row.get("move_pct"), "coverage_status": status, "manual_label": label, "manual_note": row.get("manual_note", ""), "priority": row.get("priority", ""), "gold_label_review_recommended": is_gold, "scored": bool(label and row.get("integrity_status") != "FAIL")})
            if is_gold and not label:
                out.append({"date": row["date"], "scope": scope, "symbol": row.get("symbol", ""), "move_pct": row.get("move_pct"), "coverage_status": "GOLD_LABEL_REVIEW_RECOMMENDED", "manual_label": "", "manual_note": row.get("manual_note", ""), "priority": row.get("priority", ""), "gold_label_review_recommended": True, "scored": False})
    for day in pending:
        out.append({"date": day, "scope": "MARKET", "symbol": "", "move_pct": "", "coverage_status": "PENDING_NOT_SCORED", "manual_label": "", "manual_note": "", "priority": "PENDING", "gold_label_review_recommended": False, "scored": False})
    for row in diagnostics:
        if not row.get("manual_label"):
            out.append({"date": row["date"], "scope": "DIAGNOSTIC", "symbol": row["symbol"], "move_pct": row["move_pct"], "coverage_status": "DIAGNOSTIC_ONLY_UNLABELED", "manual_label": "", "manual_note": "", "priority": "LOW", "gold_label_review_recommended": False, "scored": False})
    return out


def _review_priority(market_rows: list[dict[str, Any]], stock_rows: list[dict[str, Any]], diagnostics: list[dict[str, Any]], new_market_days: set[str]) -> list[dict[str, Any]]:
    rows = []
    for item in stock_rows:
        if item.get("manual_label"):
            continue
        is_new = item.get("review_status") == "NEW_UNLABELED"
        if is_new and item["priority"] == "HIGH":
            group = "HIGH_PRIORITY_NEW_STOCK"
        elif item["priority"] == "MEDIUM":
            group = "MEDIUM_PRIORITY_STOCK"
        elif item.get("gold_label_review_recommended"):
            group = "GOLD_LABEL_REVIEW_RECOMMENDED"
        else:
            group = "OTHER_UNLABELED_STOCK"
        rows.append({"date": item["date"], "scope": "STOCK", "symbol": item["symbol"], "priority": item["priority"], "move_pct": item["move_pct"], "explanation_type": item["explanation_source"], "event_type": item.get("event_type"), "gold_label_review_recommended": item.get("gold_label_review_recommended"), "review_group": group})
    for item in market_rows:
        if not item.get("manual_label"):
            is_new = item["date"] in new_market_days
            group = "NEW_MARKET_DAY" if is_new else ("GOLD_LABEL_REVIEW_RECOMMENDED" if item.get("gold_label_review_recommended") else "OTHER_UNLABELED_MARKET")
            rows.append({"date": item["date"], "scope": "MARKET", "symbol": "", "priority": item["priority"], "move_pct": max((_float(item.get(f"{symbol}_change_pct")) for symbol in ("SPY", "QQQ", "DIA", "IWM")), key=abs, default=0), "explanation_type": item.get("explanation_status"), "event_type": "", "gold_label_review_recommended": item.get("gold_label_review_recommended"), "review_group": group})
    for item in diagnostics:
        rows.append({"date": item["date"], "scope": "DIAGNOSTIC", "symbol": item["symbol"], "priority": "LOW", "move_pct": item["move_pct"], "explanation_type": item["explanation_type"], "event_type": item["event_type"], "gold_label_review_recommended": False, "review_group": "DIAGNOSTIC_ONLY"})
    group_order = {"HIGH_PRIORITY_NEW_STOCK": 0, "NEW_MARKET_DAY": 1, "MEDIUM_PRIORITY_STOCK": 2, "GOLD_LABEL_REVIEW_RECOMMENDED": 3, "OTHER_UNLABELED_STOCK": 4, "OTHER_UNLABELED_MARKET": 4, "DIAGNOSTIC_ONLY": 5}
    return sorted(rows, key=lambda item: (group_order.get(item["review_group"], 9), PRIORITY_ORDER.get(item["priority"], 9), item["date"], item["symbol"]))


def _metrics_markdown(metrics: dict[str, Any]) -> list[str]:
    lines = ["| Scope | Labeled | Scored labels | Precision-like | Recall-like | FP | FN | NO_PUBLIC_CATALYST | UNCLEAR | Unlabeled |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for scope in ("MARKET", "STOCK", "COMBINED"):
        item = metrics[scope]
        fmt = lambda value: "n/a" if value is None else f"{value:.1%}"
        lines.append(f"| {scope} | {item['labeled_rows']} | {item['eligible_labeled_rows']} | {fmt(item['precision_like'])} | {fmt(item['recall_like'])} | {item['false_positives']} | {item['false_negatives']} | {item['no_public_catalyst']} | {item['unclear']} | {item['unlabeled_rows']} |")
    return lines


def build() -> dict[str, Any]:
    from cache_config import cache_root
    import build_historical_evaluation_batch as batch
    import build_historical_market_cache as historical
    import evaluate_market_drivers as replay
    import evaluate_market_drivers_product as product

    progress_rows = read_csv(AUTO_DIR / "auto_resume_progress.csv")
    dates_all = [row["date"] for row in read_csv(MILESTONE / "selected_dates.csv")]
    # Prefer runner's canonical date list if the selected-date manifest is absent.
    if not dates_all:
        from evaluation import run_extended_dataset_v2 as runner
        dates_all = runner.load_dates()
    progress_by_date = {row.get("date", ""): row for row in progress_rows}
    dates = [day for day in dates_all if progress_by_date.get(day, {}).get("status") == "COMPLETE"]
    pending = [day for day in dates_all if progress_by_date.get(day, {}).get("status") == "DAILY_QUOTA_PAUSED"]
    if len(dates) != 29 or pending != [PENDING_DATE]:
        raise RuntimeError(f"INTERIM_DATE_SET_MISMATCH complete={len(dates)} pending={pending}")

    market_source = read_csv(LABEL_DIR / "market_day_review_queue_manual_qa_v1.csv")
    stock_source = read_csv(LABEL_DIR / "stock_review_queue_manual_qa_v1.csv")
    market_labels = {row.get("date", ""): row for row in market_source if row.get("date")}
    stock_labels = {(row.get("date", ""), row.get("symbol", "")): row for row in stock_source if row.get("date") and row.get("symbol")}
    old_market_keys = set(market_labels)
    old_stock_keys = set(stock_labels)

    with tempfile.TemporaryDirectory(prefix="market_bot_interim_qa_") as scratch:
        scratch_root = Path(scratch)
        scratch_eval = scratch_root / "evaluation"
        _copy_readonly_inputs(dates, scratch_eval)
        with replay.offline_network_guard():
            replay_result = product.evaluate_product(dates[0], dates[-1], market_dates=dates, output_dir=scratch_eval)
        days = replay_result.get("days", [])
        skipped = replay_result.get("skipped_dates", [])
        if len(days) != 29 or skipped:
            raise RuntimeError(f"INTERIM_OFFLINE_REPLAY_INCOMPLETE days={len(days)} skipped={len(skipped)}")
        for day in days:
            day["integrity"] = batch.evaluate_integrity(day, output_dir=scratch_eval)
            if day["integrity"].get("integrity_status") == "FAIL" and day.get("status") == "READY":
                day["status"] = "PARTIAL"
            day["_movers"] = replay._dated_snapshot(scratch_eval, "movers_snapshot", day["market_date"]) or {}
        original_history_dir = historical.HISTORY_DIR
        historical.HISTORY_DIR = scratch_eval / "history"
        try:
            market_rows = _market_rows(days, market_labels, old_market_keys)
        finally:
            historical.HISTORY_DIR = original_history_dir
        stock_rows = _stock_rows(days, stock_labels, old_stock_keys)
        diagnostics = _diagnostics(days, old_stock_keys)

    # Ensure label merge used source values verbatim and never synthesizes labels.
    if any(row["market_manual_label"] != market_labels.get(row["date"], {}).get("market_manual_label", market_labels.get(row["date"], {}).get("manual_label", "")) for row in market_rows):
        raise RuntimeError("MARKET_LABEL_PRESERVATION_FAILED")
    if any(row["manual_label"] != stock_labels.get((row["date"], row["symbol"]), {}).get("manual_label", "") for row in stock_rows):
        raise RuntimeError("STOCK_LABEL_PRESERVATION_FAILED")
    if any(row["manual_note"] != market_labels.get(row["date"], {}).get("review_notes", market_labels.get(row["date"], {}).get("manual_note", "")) for row in market_rows):
        raise RuntimeError("MARKET_NOTE_PRESERVATION_FAILED")
    if any(row["manual_note"] != stock_labels.get((row["date"], row["symbol"]), {}).get("review_notes", stock_labels.get((row["date"], row["symbol"]), {}).get("manual_note", "")) for row in stock_rows):
        raise RuntimeError("STOCK_NOTE_PRESERVATION_FAILED")

    market_metrics_rows = [row for row in market_rows if row.get("manual_label") and row.get("integrity_status") != "FAIL"]
    stock_metrics_rows = [row for row in stock_rows if row.get("manual_label") and row.get("integrity_status") != "FAIL"]
    combined_metrics_rows = market_metrics_rows + stock_metrics_rows
    metrics = {"MARKET": _metric(market_metrics_rows), "STOCK": _metric(stock_metrics_rows), "COMBINED": _metric(combined_metrics_rows)}
    for scope, all_rows in (("MARKET", market_rows), ("STOCK", stock_rows)):
        metrics[scope]["unlabeled_rows"] = sum(not row.get("manual_label") for row in all_rows)
    metrics["COMBINED"]["unlabeled_rows"] = metrics["MARKET"]["unlabeled_rows"] + metrics["STOCK"]["unlabeled_rows"]

    new_market_days = {row["date"] for row in market_rows if row["date"] not in old_market_keys}
    new_stock_keys = {(row["date"], row["symbol"]) for row in stock_rows if (row["date"], row["symbol"]) not in old_stock_keys}
    coverage_rows = _coverage_rows(market_rows, stock_rows, diagnostics, dates, pending, market_labels, stock_labels, old_market_keys, old_stock_keys)
    priority_rows = _review_priority(market_rows, stock_rows, diagnostics, new_market_days)
    taxonomy = _taxonomy(market_rows, stock_rows)
    root_causes = Counter(row["primary_root_cause"] for row in taxonomy)
    residual = {"FALSE_POSITIVE": Counter(), "FALSE_NEGATIVE": Counter()}
    for row in taxonomy:
        residual[row["manual_label"]][row["primary_root_cause"]] += 1

    # Main market/stock review rows are unique by their required exact keys.
    if len({row["date"] for row in market_rows}) != len(market_rows):
        raise RuntimeError("DUPLICATE_MARKET_REVIEW_DATE")
    if len({(row["date"], row["symbol"]) for row in stock_rows}) != len(stock_rows):
        raise RuntimeError("DUPLICATE_STOCK_REVIEW_KEY")

    compact: list[dict[str, Any]] = []
    for row in priority_rows:
        if row["review_group"] == "DIAGNOSTIC_ONLY":
            source = next((item for item in diagnostics if item["date"] == row["date"] and item["symbol"] == row["symbol"] and item["event_type"] == row["event_type"]), {})
            compact.append({"date": row["date"], "scope": "DIAGNOSTIC", "symbol": row["symbol"], "move_pct": row["move_pct"], "current_explanation": row["explanation_type"], "selected_event": source.get("selected_event"), "source": source.get("source"), "published_at": source.get("published_at"), "why_system_selected_it": "Diagnostic-only: included for possible gold-label review; excluded from denominators unless manually labeled.", "manual_label": "", "manual_note": "", "review_priority": row["priority"]})
        elif row["scope"] == "STOCK":
            source = next(item for item in stock_rows if item["date"] == row["date"] and item["symbol"] == row["symbol"])
            compact.append({"date": source["date"], "scope": "STOCK", "symbol": source["symbol"], "move_pct": source["move_pct"], "current_explanation": source["current_final_explanation"], "selected_event": source["selected_event"], "source": source["source"], "published_at": source["publication_time"], "why_system_selected_it": source["why_system_selected_it"], "manual_label": source["manual_label"], "manual_note": source["manual_note"], "review_priority": row["priority"]})
        else:
            source = next(item for item in market_rows if item["date"] == row["date"])
            compact.append({"date": source["date"], "scope": "MARKET", "symbol": "", "move_pct": row.get("move_pct"), "current_explanation": source.get("market_regime"), "selected_event": source.get("selected_evidence"), "source": source.get("selected_market_story_source"), "published_at": source.get("evidence_timestamp"), "why_system_selected_it": f"New market-day row; driver_status={source.get('market_driver_status') or 'NO_SELECTED_DRIVER'}", "manual_label": source.get("market_manual_label", ""), "manual_note": source.get("manual_note", ""), "review_priority": row["priority"]})

    statuses = Counter(day.get("status", "UNKNOWN") for day in days)
    integrity = Counter(day.get("integrity", {}).get("integrity_status", "UNKNOWN") for day in days)
    offline_calls = int(replay_result.get("metrics", {}).get("offline_network_calls", 0) or 0)
    if offline_calls != 0:
        raise RuntimeError(f"OFFLINE_NETWORK_CALLS_NOT_ZERO:{offline_calls}")
    original_labeled_market = sum(bool((row.get("market_manual_label") or row.get("manual_label") or "").strip()) for row in market_source)
    original_labeled_stock = sum(bool((row.get("manual_label") or "").strip()) for row in stock_source)
    matched_labeled_market = sum(bool(row.get("market_manual_label")) for row in market_rows)
    matched_labeled_stock = sum(bool(row.get("manual_label")) for row in stock_rows)
    if matched_labeled_market != original_labeled_market or matched_labeled_stock != original_labeled_stock:
        raise RuntimeError(
            "EXISTING_LABEL_NOT_IN_REPLAY_SCOPE "
            f"market={matched_labeled_market}/{original_labeled_market} "
            f"stock={matched_labeled_stock}/{original_labeled_stock}"
        )
    existing_labeled_rows_preserved = matched_labeled_market + matched_labeled_stock
    unlabeled_market_new = sum(not row.get("market_manual_label") and row["date"] not in old_market_keys for row in market_rows)
    unlabeled_stock_new = sum(not row.get("manual_label") and (row["date"], row["symbol"]) not in old_stock_keys for row in stock_rows)
    priority_counts = Counter(row.get("priority", "LOW") for row in priority_rows if row.get("scope") in {"STOCK", "MARKET"} and row["review_group"] != "DIAGNOSTIC_ONLY")
    gold_rows = [row for row in market_rows + stock_rows if row.get("gold_label_review_recommended") and not row.get("manual_label")]
    useful_first_pass_estimate = len([row for row in stock_rows if (row["date"], row["symbol"]) in new_stock_keys and row.get("priority") == "HIGH"]) + len(new_market_days)

    summary = {
        "package": "Interim 29-Day Manual QA Package V1", "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "INTERIM_PENDING_2026-09-22", "completed_dates": dates,
        "pending_dates": [{"date": day, "status": "PENDING", "scoring": "NOT_SCORED"} for day in pending],
        "excluded_pending_date_from_metrics": PENDING_DATE,
        "counts": {
            "completed_dates": len(dates), "market_review_rows": len(market_rows), "stock_review_rows_abs_move_ge_4pct": len(stock_rows),
            "diagnostic_rows": len(diagnostics), "original_labeled_market_rows": original_labeled_market,
            "original_labeled_stock_rows": original_labeled_stock, "existing_labeled_rows_preserved": existing_labeled_rows_preserved,
            "matched_labeled_market_rows": matched_labeled_market, "matched_labeled_stock_rows": matched_labeled_stock,
            "new_unlabeled_market_rows": unlabeled_market_new, "new_unlabeled_stock_rows": unlabeled_stock_new,
            "high_priority": priority_counts["HIGH"], "medium_priority": priority_counts["MEDIUM"], "low_priority": priority_counts["LOW"],
            "gold_label_review_candidates": len(gold_rows), "estimated_manual_decisions_for_useful_first_pass": useful_first_pass_estimate,
        },
        "readiness_counts": dict(statuses), "integrity_counts": dict(integrity), "interim_labeled_metrics": metrics,
        "current_error_taxonomy": {
            "false_positives": {"count": sum(residual["FALSE_POSITIVE"].values()), "root_causes": dict(residual["FALSE_POSITIVE"])},
            "false_negatives": {"count": sum(residual["FALSE_NEGATIVE"].values()), "root_causes": dict(residual["FALSE_NEGATIVE"])},
            "method": "Provisional rule-based reclassification from current replay outputs and candidate/gate state; no historical root-cause assignments were carried forward.",
        },
        "new_rows_breakdown": {
            "market_dates": sorted(new_market_days),
            "stock": [{"date": row["date"], "symbol": row["symbol"], "move_pct": row["move_pct"], "explanation_type": row["explanation_source"], "event_type": row.get("event_type")} for row in stock_rows if (row["date"], row["symbol"]) in new_stock_keys],
        },
        "provider_activity": {"live_provider_calls": 0, "offline_network_calls": offline_calls, "fmp_requests": 0},
        "manual_label_inputs": [str(LABEL_DIR / "market_day_review_queue_manual_qa_v1.csv"), str(LABEL_DIR / "stock_review_queue_manual_qa_v1.csv")],
        "output_paths": {name: str(OUTPUT_DIR / name) for name in (
            "interim_qa_summary.md", "interim_qa_summary.json", "market_review_queue.csv", "stock_review_queue.csv",
            "diagnostic_event_sample.csv", "compact_manual_review_queue.csv", "current_error_taxonomy.csv", "label_coverage.csv", "review_priority_summary.csv",
        )},
    }

    write_csv(OUTPUT_DIR / "market_review_queue.csv", market_rows, list(dict.fromkeys(key for row in market_rows for key in row)))
    write_csv(OUTPUT_DIR / "stock_review_queue.csv", stock_rows, list(dict.fromkeys(key for row in stock_rows for key in row)))
    write_csv(OUTPUT_DIR / "diagnostic_event_sample.csv", diagnostics, list(dict.fromkeys(key for row in diagnostics for key in row)) or ["date", "symbol", "event_type"])
    write_csv(OUTPUT_DIR / "compact_manual_review_queue.csv", compact, ["date", "scope", "symbol", "move_pct", "current_explanation", "selected_event", "source", "published_at", "why_system_selected_it", "manual_label", "manual_note", "review_priority"])
    write_csv(OUTPUT_DIR / "current_error_taxonomy.csv", taxonomy, ["scope", "date", "symbol", "manual_label", "current_explanation", "selected_event", "event_type", "primary_root_cause", "classification_basis", "confidence"])
    write_csv(OUTPUT_DIR / "label_coverage.csv", coverage_rows, ["date", "scope", "symbol", "move_pct", "coverage_status", "manual_label", "manual_note", "priority", "gold_label_review_recommended", "scored"])
    write_csv(OUTPUT_DIR / "review_priority_summary.csv", priority_rows, ["date", "scope", "symbol", "priority", "move_pct", "explanation_type", "event_type", "gold_label_review_recommended", "review_group"])

    lines = [
        "# Interim 29-Day Manual QA Package V1", "",
        f"Generated: {summary['generated_at']}", "",
        "This is an interim, cache-only replay of 29 completed dates. Sep 22 is retained as PENDING / NOT_SCORED and excluded from review denominators and all metrics.", "",
        "## Dates", "", ", ".join(dates), "", f"Pending: {PENDING_DATE} (PENDING / NOT_SCORED).", "",
        "## Review package counts", "",
        f"- Market rows: {len(market_rows)}.", f"- Stock rows with |move| >= 4%: {len(stock_rows)}.", f"- Diagnostic event sample rows: {len(diagnostics)}.",
        f"- Original labeled market/stock rows: {original_labeled_market}/{original_labeled_stock}.",
        f"- Matched labeled rows preserved: market={matched_labeled_market}, stock={matched_labeled_stock}.",
        f"- New unlabeled rows: market={unlabeled_market_new}, stock={unlabeled_stock_new}.",
        f"- Review priority counts (main queue, unlabeled): HIGH={priority_counts['HIGH']}, MEDIUM={priority_counts['MEDIUM']}, LOW={priority_counts['LOW']}.",
        f"- Gold-label review recommendations: {len(gold_rows)}.",
        f"- Estimated useful first-pass manual decisions: about {useful_first_pass_estimate} (new high-priority stocks plus new market days).", "",
        "## Interim metrics from labeled, eligible rows only", "", *_metrics_markdown(metrics), "",
        "NO_PUBLIC_CATALYST and UNCLEAR labels are reported but do not enter precision-like or recall-like denominators. Sep 22 and unlabeled/diagnostic-only rows are excluded.", "",
        "## Current residual error taxonomy", "",
        f"- False positives: {summary['current_error_taxonomy']['false_positives']['count']} — {dict(residual['FALSE_POSITIVE'])}.",
        f"- False negatives: {summary['current_error_taxonomy']['false_negatives']['count']} — {dict(residual['FALSE_NEGATIVE'])}.",
        "- Root-cause classifications are provisional and based on current replay evidence, not carried-forward labels.", "",
        "## Manual workflow", "", "Start with HIGH-priority new stock rows, then new market days, MEDIUM-priority stocks, gold-label candidates, and diagnostic-only cases. Do not copy a suggested review priority into the causal label.", "",
        "Primary file: `compact_manual_review_queue.csv`.", "",
        "## Safety checks", "", "- Live provider calls: 0.", f"- Offline network calls: {offline_calls}.", "- Production caches/snapshots: read-only; evaluation scratch data was temporary.", "- Manual-label source CSVs were read-only; no labels were assigned.", "- Main queue keys are unique by date and date+symbol.", "",
    ]
    (OUTPUT_DIR / "interim_qa_summary.md").parent.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "interim_qa_summary.md").write_text("\n".join(lines), encoding="utf-8")
    write_json(OUTPUT_DIR / "interim_qa_summary.json", summary)
    return summary


def main() -> int:
    result = build()
    print(json.dumps(result["counts"], ensure_ascii=False, indent=2))
    print(f"OUTPUT_DIR={OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
