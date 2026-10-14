"""DXY-only historical cache recovery using Yahoo Finance as source of truth."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from collectors.yahoo import YahooClient
from config import MARKET_SYMBOLS

ROOT = Path(__file__).resolve().parents[1]
MILESTONE = ROOT / "output/evaluation_extended_v2/30_day_milestone"


def atomic_write(path: Path, content: str) -> None:
    """Atomically replace a UTF-8 text artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def atomic_json(path: Path, payload: Any) -> None:
    """Atomically write JSON."""
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def validate_dxy_record(record: Any, market_date: str, *, expected_previous_date: str | None = None) -> tuple[str, str | None]:
    """Classify a canonical DXY record and return status plus precise reason."""
    try:
        target = date.fromisoformat(market_date)
    except ValueError:
        return "INVALID_DATE", "REQUESTED_MARKET_DATE_INVALID"
    if record is None:
        return "MISSING", "DXY_RECORD_MISSING"
    if not isinstance(record, dict):
        return "INVALID_SCHEMA", "DXY_RECORD_NOT_OBJECT"
    required = ("source", "symbol", "provider_symbol", "market_date", "close", "previous_close", "change_pct", "latest_observation_date", "previous_observation_date", "source_timezone", "source_timestamp_utc")
    if any(key not in record for key in required):
        return "INVALID_SCHEMA", "DXY_REQUIRED_FIELD_MISSING"
    if (record.get("source"), record.get("symbol"), record.get("provider_symbol")) != ("Yahoo Finance / ICE Futures delayed", "DXY", "DX-Y.NYB"):
        return "INVALID_SCHEMA", "DXY_SOURCE_OR_SYMBOL_MISMATCH"
    try:
        normalized = date.fromisoformat(str(record["market_date"]))
        latest = date.fromisoformat(str(record["latest_observation_date"]))
        previous = date.fromisoformat(str(record["previous_observation_date"]))
    except (TypeError, ValueError):
        return "INVALID_DATE", "DXY_OBSERVATION_DATE_INVALID"
    if normalized != target or latest != target or previous >= target:
        return "INVALID_DATE", "DXY_OBSERVATION_DATE_MISMATCH"
    if expected_previous_date and previous.isoformat() != expected_previous_date:
        return "MISSING_PREVIOUS_SESSION", "DXY_PREVIOUS_SESSION_NOT_IMMEDIATELY_PRIOR_VALID_BAR"
    close, previous_close, pct = (_number(record.get(key)) for key in ("close", "previous_close", "change_pct"))
    if close is None or previous_close is None or pct is None or close <= 0 or previous_close <= 0:
        return "INVALID_VALUE", "DXY_CLOSE_OR_CHANGE_INVALID"
    expected_pct = ((close / previous_close) - 1) * 100
    if not math.isclose(pct, expected_pct, rel_tol=0, abs_tol=0.00001):
        return "INVALID_VALUE", "DXY_CHANGE_PERCENT_MISMATCH"
    if record.get("verified") is not True or record.get("validation_passed") is not True:
        return "INVALID_VALUE", "DXY_VALIDATION_FLAGS_NOT_PASSED"
    if not str(record.get("source_timezone", "")).strip() or not str(record.get("source_timestamp_utc", "")).endswith("+00:00"):
        return "INVALID_SCHEMA", "DXY_SOURCE_TIMESTAMP_METADATA_INVALID"
    return "AVAILABLE_VALID", None


def build_dxy_records(rows: list[dict[str, Any]], target_dates: list[str], *, acquired_at_utc: str | None = None) -> dict[str, dict[str, Any]]:
    """Build exact-date records using the immediately prior valid Yahoo bar."""
    parsed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            day = date.fromisoformat(str(row.get("market_date"))).isoformat()
        except (TypeError, ValueError):
            continue
        if day in parsed:
            parsed[day]["_duplicate_date"] = True
        else:
            parsed[day] = dict(row)
    valid_days = [day for day, row in sorted(parsed.items()) if (_number(row.get("close")) or 0) > 0 and not row.get("_duplicate_date")]
    results: dict[str, dict[str, Any]] = {}
    acquired = acquired_at_utc or datetime.now(timezone.utc).isoformat()
    for target in target_dates:
        row = parsed.get(target)
        if row is None:
            results[target] = {"status": "MISSING", "reason": "YAHOO_TARGET_SESSION_NOT_RETURNED", "raw_row": None}
            continue
        if row.get("_duplicate_date"):
            results[target] = {"status": "INVALID_SCHEMA", "reason": "DUPLICATE_YAHOO_SESSION_DATE", "raw_row": row}
            continue
        close = _number(row.get("close"))
        if close is None or close <= 0:
            results[target] = {"status": "INVALID_VALUE", "reason": "YAHOO_TARGET_CLOSE_MISSING_OR_INVALID", "raw_row": row}
            continue
        prior = [day for day in valid_days if day < target]
        if not prior:
            results[target] = {"status": "MISSING_PREVIOUS_SESSION", "reason": "NO_PRIOR_VALID_YAHOO_SESSION_IN_RANGE", "raw_row": row}
            continue
        previous_day = prior[-1]
        previous_row = parsed[previous_day]
        previous_close = _number(previous_row.get("close"))
        if previous_close is None or previous_close <= 0:
            results[target] = {"status": "INVALID_VALUE", "reason": "YAHOO_PREVIOUS_CLOSE_MISSING_OR_INVALID", "raw_row": row}
            continue
        record = {
            "symbol": "DXY", "provider_symbol": "DX-Y.NYB", "display_name": "US Dollar Index", "category": "macro",
            "source": "Yahoo Finance / ICE Futures delayed", "market_date": target,
            "close": round(close, 6), "previous_close": round(previous_close, 6),
            "change": round(close - previous_close, 6), "change_pct": round(((close / previous_close) - 1) * 100, 6),
            "open": _number(row.get("open")), "high": _number(row.get("high")), "low": _number(row.get("low")),
            "volume": _number(row.get("volume")) or 0, "latest_observation_date": target, "previous_observation_date": previous_day,
            "selected_observation_date": target, "selected_source_date": target, "previous_source_date": previous_day,
            "canonical_market_date": target, "freshness": "current", "asof_lag_days": 0,
            "source_timezone": row.get("source_timezone"), "source_timestamp_utc": row.get("source_timestamp_utc"),
            "acquired_at_utc": acquired, "verified": True, "validation_passed": True,
            "integrity": {"status": "PASS", "market_date": target, "previous_valid_session": previous_day, "future_data_used": False},
        }
        status, reason = validate_dxy_record(record, target, expected_previous_date=previous_day)
        results[target] = {"status": status, "reason": reason, "record": record, "raw_row": row, "previous_raw_row": previous_row}
    return results


def _load(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _existing_valid(item: Any, target: str) -> bool:
    """Validate both current and established DXY cache records without rewriting them."""
    if not isinstance(item, dict):
        return False
    # The existing production schema predates explicit raw timestamp provenance.
    # It remains valid when its provider identity, exact date, finite values,
    # locally consistent percentage and validation flags are intact.
    if (
        item.get("source") != "Yahoo Finance / ICE Futures delayed"
        or item.get("symbol") != "DXY"
        or item.get("provider_symbol") != "DX-Y.NYB"
        or item.get("verified") is not True
        or item.get("validation_passed") is not True
    ):
        return False
    try:
        if date.fromisoformat(str(item.get("market_date") or item.get("canonical_market_date"))) != date.fromisoformat(target):
            return False
        latest = date.fromisoformat(str(item.get("latest_observation_date")))
        previous = date.fromisoformat(str(item.get("previous_observation_date")))
    except (TypeError, ValueError):
        return False
    close, previous_close, pct = (_number(item.get(key)) for key in ("close", "previous_close", "change_pct"))
    return bool(
        latest.isoformat() == target
        and previous < latest
        and close is not None and previous_close is not None and pct is not None
        and close > 0 and previous_close > 0
        and math.isclose(pct, ((close / previous_close) - 1) * 100, rel_tol=0, abs_tol=0.00001)
    )


def _patch_snapshot(path: Path, target: str, record: dict[str, Any]) -> bool:
    """Replace DXY only, and refresh enclosing integrity metadata consistently."""
    snapshot = _load(path)
    if not snapshot or snapshot.get("market_date") != target or not isinstance(snapshot.get("market"), dict):
        return False
    market = snapshot["market"]
    macro = market.setdefault("macro", {})
    if _existing_valid(macro.get("DXY"), target):
        return False
    macro["DXY"] = record
    all_items = [item for group in market.values() if isinstance(group, dict) for item in group.values() if isinstance(item, dict)]
    all_valid = all(item.get("verified") is True and item.get("validation_passed") is True for item in all_items)
    metadata = snapshot.get("cache_metadata")
    if isinstance(metadata, dict):
        metadata.update({"collection_status": "SUCCESS" if all_valid else "FAILED", "validation_status": "PASSED" if all_valid else "FAILED", "validation_passed": all_valid, "cache_status": "DXY_RECOVERY"})
    summary = snapshot.get("summary")
    if isinstance(summary, dict):
        summary.update({"successful": sum(item.get("verified") is True for item in all_items), "failed": sum(item.get("verified") is not True for item in all_items)})
    integrity = snapshot.get("historical_integrity")
    if isinstance(integrity, dict):
        entries = integrity.setdefault("asof_macro_series", [])
        if isinstance(entries, list):
            entry = next((item for item in entries if isinstance(item, dict) and item.get("symbol") == "DXY"), None)
            if entry is None:
                entries.append({"symbol": "DXY", "selected_source_date": target, "asof_lag_days": 0})
            else:
                entry.update({"selected_source_date": target, "asof_lag_days": 0})
        source_dates = [str(item.get("selected_source_date") or item.get("latest_bar_datetime") or item.get("latest_observation_date") or "")[:10] for item in all_items]
        source_dates = [item for item in source_dates if item]
        future = bool(integrity.get("future_data_leak_detected")) or any(item > target for item in source_dates)
        integrity.update({"max_source_date": max(source_dates) if source_dates else None, "future_data_leak_detected": future, "integrity_status": "FAIL" if future else "PASS"})
    atomic_json(path, snapshot)
    return True


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = ["date", "yahoo_record_found", "normalized_market_date", "source_timestamp_utc", "source_timezone", "close", "previous_close", "change_pct", "cache_status", "validation_status", "reason"]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def recover(milestone: Path, dates: list[str], yahoo_client: Any, logger: logging.Logger) -> dict[str, Any]:
    """Fetch one bounded Yahoo range, patch only DXY, and emit audit reports."""
    start = (date.fromisoformat(min(dates)) - timedelta(days=7)).isoformat()
    end = max(dates)
    response = yahoo_client.fetch_history_range(MARKET_SYMBOLS["macro"]["DXY"], start, end, logger)
    if response.get("status") == "AVAILABLE_VALID":
        outcomes = build_dxy_records(response.get("rows", []), dates)
    else:
        outcomes = {day: {"status": "SOURCE_ERROR", "reason": response.get("error") or response.get("status"), "raw_row": None} for day in dates}
    audit: list[dict[str, Any]] = []
    repaired = preserved = 0
    for target in dates:
        outcome = outcomes[target]
        snapshot_path = milestone / "history" / target / "market_snapshot.json"
        snapshot = _load(snapshot_path)
        existing = snapshot.get("market", {}).get("macro", {}).get("DXY") if snapshot else None
        raw = outcome.get("raw_row") or {}
        record = outcome.get("record")
        if _existing_valid(existing, target):
            record, status, reason, cache_status = existing, "AVAILABLE_VALID", "", "PRESERVED_VALID"
            preserved += 1
        elif outcome.get("status") == "AVAILABLE_VALID" and isinstance(record, dict) and _patch_snapshot(snapshot_path, target, record):
            status, reason, cache_status = "AVAILABLE_VALID", "", "REPAIRED"
            repaired += 1
        else:
            status, reason, cache_status = outcome.get("status", "SOURCE_ERROR"), outcome.get("reason", ""), "UNCHANGED_INVALID"
        audit.append({
            "date": target, "yahoo_record_found": bool(raw) or cache_status == "PRESERVED_VALID",
            "normalized_market_date": (record or {}).get("market_date") if isinstance(record, dict) else raw.get("market_date"),
            "source_timestamp_utc": (record or {}).get("source_timestamp_utc") if isinstance(record, dict) else raw.get("source_timestamp_utc"),
            "source_timezone": (record or {}).get("source_timezone") if isinstance(record, dict) else raw.get("source_timezone"),
            "close": (record or {}).get("close") if isinstance(record, dict) else raw.get("close"),
            "previous_close": (record or {}).get("previous_close") if isinstance(record, dict) else None,
            "change_pct": (record or {}).get("change_pct") if isinstance(record, dict) else None,
            "cache_status": cache_status, "validation_status": status, "reason": reason,
        })
    report_dir = milestone / "dxy_recovery_v1"
    _write_csv(report_dir / "dxy_coverage.csv", audit)
    samples = []
    for target in ("2026-08-17", "2026-09-01", "2026-09-15", "2026-09-21"):
        outcome = outcomes.get(target, {})
        samples.append({"date": target, "raw_row": outcome.get("raw_row"), "previous_raw_row": outcome.get("previous_raw_row"), "result": next((r for r in audit if r["date"] == target), None)})
    diagnosis = [
        "# DXY Historical Cache Recovery V1 — Diagnosis", "",
        "## Root cause", "",
        "The configured Yahoo symbol is DX-Y.NYB. Existing code in YahooClient.fetch_instrument() requests range=10d even for historical market dates, then _observations() discards all dates newer than the requested date. For older dates this leaves fewer than two bars; fetch_instrument() reports that as YAHOO_RESPONSE_FORMAT_CHANGED. The collector never requested the target history. The root cause is historical range semantics (B), not symbol, timezone, validator, or cache schema.", "",
        f"The bounded probe for {start} through {end} inclusive returned {len(response.get('rows', []))} rows. Yahoo metadata reports exchangeTimezoneName=America/New_York. Epoch timestamps are converted to that exchange timezone before deriving market_date; the sample daily timestamp 04:00 UTC maps to 00:00 America/New_York on the same date. Rows outside the requested range are discarded; null-close weekend placeholders are not valid sessions. Each change uses the preceding valid close row, not the prior calendar day.", "",
        "## Representative-date trace", "",
    ]
    for sample in samples:
        diagnosis.extend([f"### {sample['date']}", "", f"- Raw current row: {json.dumps(sample['raw_row'], ensure_ascii=False, sort_keys=True)}", f"- Previous valid row: {json.dumps(sample['previous_raw_row'], ensure_ascii=False, sort_keys=True)}", f"- Normalized/cache validation: {json.dumps(sample['result'], ensure_ascii=False, sort_keys=True)}", ""])
    diagnosis.extend(["## Validation/cache path", "", "Range rows are normalized by YahooClient.fetch_history_range(), converted into exact-session DXY records by build_dxy_records(), checked by validate_dxy_record(), and patched into only the matching staged history market_snapshot.json using same-directory temporary-file replacement. A valid existing DXY record is never rewritten. Enclosing cache metadata is recomputed from existing instrument validation flags; no unrelated market component is collected.", ""])
    atomic_write(report_dir / "dxy_diagnosis.md", "\n".join(diagnosis))
    invalid = [row for row in audit if row["validation_status"] != "AVAILABLE_VALID"]
    summary = {
        "source": "Yahoo Finance / ICE Futures delayed", "yahoo_symbol": "DX-Y.NYB",
        "request": {"start_inclusive": start, "end_inclusive": end, "requests": int(getattr(yahoo_client, "request_count", 0))},
        "selected_dates_total": len(dates), "valid_dates": len(dates) - len(invalid), "coverage_rate": round((len(dates) - len(invalid)) / max(1, len(dates)), 4),
        "repaired_dates": repaired, "preserved_valid_dates": preserved,
        "remaining_invalid_dates": [{"date": row["date"], "status": row["validation_status"], "reason": row["reason"]} for row in invalid],
        "twelve_data_credits": 0, "finnhub_requests": 0, "fmp_requests": 0,
        "ready_for_phase_b_resume": not invalid,
    }
    atomic_json(report_dir / "dxy_recovery_summary.json", summary)
    lines = ["# DXY Historical Cache Recovery V1 — Summary", "", f"- Yahoo symbol: DX-Y.NYB; bounded range: {start} through {end}.", f"- Valid DXY dates: {summary['valid_dates']}/{len(dates)} ({summary['coverage_rate']:.0%}).", f"- Repaired: {repaired}; valid existing records preserved: {preserved}; remaining invalid: {len(invalid)}.", "- Twelve Data credits: 0; Finnhub requests: 0; FMP requests: 0.", f"- READY_FOR_PHASE_B_RESUME = {str(summary['ready_for_phase_b_resume']).lower()}.", ""]
    if invalid:
        lines.extend(["## Remaining invalid dates", "", *[f"- {r['date']}: {r['status']} — {r['reason']}" for r in invalid], ""])
    atomic_write(report_dir / "dxy_recovery_summary.md", "\n".join(lines))
    return summary


def main() -> int:
    """Run DXY-only historical recovery for the selected 30-date milestone."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--milestone-dir", type=Path, default=MILESTONE)
    args = parser.parse_args()
    with (args.milestone_dir / "selected_dates.csv").open(encoding="utf-8-sig", newline="") as handle:
        dates = [row["date"] for row in csv.DictReader(handle)]
    if not dates:
        parser.error("NO_SELECTED_DATES")
    logger = logging.getLogger("dxy_recovery")
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler())
    summary = recover(args.milestone_dir, dates, YahooClient(), logger)
    print(f"DXY coverage: {summary['valid_dates']}/{summary['selected_dates_total']}")
    print(f"READY_FOR_PHASE_B_RESUME={str(summary['ready_for_phase_b_resume']).lower()}")
    return 0 if summary["ready_for_phase_b_resume"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
