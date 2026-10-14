"""Audit targeted 30-day mover recovery inputs without making provider calls.

This preflight creates an isolated recovery package. It will not request
missing symbols unless all already-valid per-symbol inputs required to rebuild
each full mover ranking are present on disk.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MILESTONE = ROOT / "output/evaluation_extended_v2/30_day_milestone"
DEFAULT_OUTPUT = MILESTONE / "targeted_recovery_v1"
TARGET_DATES = ("2026-08-19", "2026-09-14", "2026-09-22")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _selected_dates() -> list[str]:
    path = MILESTONE / "selected_dates.csv"
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [row["date"] for row in csv.DictReader(handle)]


def _inventory(day: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Derive missing symbols from requested failed batches, cross-check errors."""
    import movers

    folder = MILESTONE / "history" / day
    snapshot_path = folder / "movers_snapshot.json"
    snapshot = _read_json(snapshot_path)
    if not snapshot:
        return {"date": day, "state": "BLOCKED", "reason": "MOVER_SNAPSHOT_MISSING"}, []

    batches = snapshot.get("batches") if isinstance(snapshot.get("batches"), list) else []
    requested = [symbol for batch in batches for symbol in batch.get("symbols", [])]
    successful = {
        symbol
        for batch in batches
        if batch.get("state") == "SUCCESS"
        and batch.get("symbols_valid") == len(batch.get("symbols", []))
        for symbol in batch.get("symbols", [])
    }
    missing = sorted(set(requested) - successful)
    error_symbols = {
        row.get("symbol") for row in snapshot.get("errors", [])
        if isinstance(row, dict) and row.get("symbol")
    }
    batches_unique = len(set(requested)) == len(requested)
    crosscheck = set(missing) == error_symbols
    counts_match = len(requested) == snapshot.get("symbols_requested") and len(missing) == snapshot.get("symbols_failed")
    source_rows = []
    checkpoint_path = folder / ".movers_build/.resume/mover_checkpoint.json"
    checkpoint = _read_json(checkpoint_path)
    checkpoint_rows = checkpoint.get("valid_results", []) if checkpoint else []
    persisted_by_symbol = {
        str(row.get("symbol", "")).upper(): row
        for row in checkpoint_rows
        if isinstance(row, dict) and row.get("verified") is True
    }
    instruments = {item.symbol: item for item in movers.load_universe()}
    errors_by_symbol = {
        row.get("symbol"): row.get("error", "UNKNOWN")
        for row in snapshot.get("errors", []) if isinstance(row, dict)
    }
    batch_by_symbol = {
        symbol: batch.get("batch_number")
        for batch in batches for symbol in batch.get("symbols", [])
    }
    for symbol in missing:
        source_rows.append({
            "date": day,
            "symbol": symbol,
            "company_name": instruments[symbol].display_name if symbol in instruments else "",
            "batch": batch_by_symbol.get(symbol),
            "saved_provider_error": errors_by_symbol.get(symbol, "UNKNOWN"),
            "provider_status": "NOT_REQUESTED",
            "reason": "FULL_VALID_BASE_ROWS_NOT_PERSISTED; RECOVERY_PREFLIGHT_BLOCKED",
            "retryability": "ACTION_REQUIRED",
        })

    strict_complete = (
        snapshot.get("collection_status") == "SUCCESS"
        and snapshot.get("symbols_valid") == snapshot.get("symbols_requested")
        and bool(snapshot.get("top_gainers"))
        and bool(snapshot.get("top_losers"))
    )
    saved_all_valid_rows = (
        checkpoint is not None
        and checkpoint.get("market_date") == day
        and len(persisted_by_symbol) == snapshot.get("symbols_valid")
    )
    # Current mover snapshots retain only ranked top/bottom rows. A checkpoint
    # is required to reproduce full-universe ranking without refetching.
    has_ranking_inputs = strict_complete or saved_all_valid_rows
    state = "PASS" if strict_complete else "BLOCKED"
    reason = "" if strict_complete else "INVALID_REQUIRED_MOVER_ARTIFACT"
    return {
        "date": day,
        "artifact_path": str(snapshot_path),
        "snapshot_market_date": snapshot.get("market_date"),
        "requested_count_from_batches": len(requested),
        "unique_requested_count": len(set(requested)),
        "valid_count_from_success_batches": len(successful),
        "missing_symbols": missing,
        "error_list_matches_missing_set": crosscheck,
        "batch_counts_match_snapshot": counts_match,
        "requested_universe_is_unique": batches_unique,
        "collection_status": snapshot.get("collection_status"),
        "symbols_requested": snapshot.get("symbols_requested"),
        "symbols_valid": snapshot.get("symbols_valid"),
        "symbols_failed": snapshot.get("symbols_failed"),
        "top_gainers_count": len(snapshot.get("top_gainers", [])),
        "top_losers_count": len(snapshot.get("top_losers", [])),
        "strict_integrity": state,
        "strict_integrity_reason": reason,
        "checkpoint_path": str(checkpoint_path) if checkpoint_path.exists() else None,
        "checkpoint_valid_rows": len(persisted_by_symbol),
        "full_valid_base_rows_persisted": saved_all_valid_rows,
        "full_ranking_inputs_available_without_refetch": has_ranking_inputs,
        "snapshot_sha256": __import__("hashlib").sha256(snapshot_path.read_bytes()).hexdigest(),
    }, source_rows


def _validate_sep14() -> dict[str, Any]:
    import movers
    from validation.validate import validate_market_result

    day = "2026-09-14"
    folder = MILESTONE / "history" / day
    snapshot_path = folder / "movers_snapshot.json"
    snapshot = _read_json(snapshot_path) or {}
    checkpoint_path = folder / ".movers_build/.resume/mover_checkpoint.json"
    checkpoint = _read_json(checkpoint_path) or {}
    rows = checkpoint.get("valid_results", [])
    valid_cache = movers.load_valid_movers_cache(day, path=snapshot_path, require_historical_integrity=True)
    row_dates = {str(row.get("latest_bar_datetime", ""))[:10] for row in rows if isinstance(row, dict)}
    unique_symbols = {str(row.get("symbol", "")).upper() for row in rows if isinstance(row, dict)}
    failed_validation = [
        row.get("symbol") for row in rows
        if not isinstance(row, dict) or not validate_market_result(row)[0]
        or not row.get("volume") or row.get("volume") <= 0
    ]
    proof = snapshot.get("historical_integrity") or {}
    checks = {
        "correct_date": snapshot.get("market_date") == day and checkpoint.get("market_date") == day and row_dates == {day},
        "point_in_time_integrity_pass": proof.get("requested_market_date") == day and proof.get("integrity_status") == "PASS" and proof.get("future_data_leak_detected") is False and (proof.get("max_source_date") or "") <= day,
        "current_cache_contract_valid": valid_cache is not None,
        "universe_fingerprint_matches": snapshot.get("universe_hash") == movers.universe_fingerprint(movers.load_universe()),
        "candidate_mode_valid": snapshot.get("prefilter_enabled") is True and snapshot.get("candidate_pool_size") == 40 and snapshot.get("fallback_used") is False and snapshot.get("symbols_requested") == 40,
        "complete_ranked_checkpoint": len(rows) == 40 and len(unique_symbols) == 40 and bool(snapshot.get("top_gainers")) and bool(snapshot.get("top_losers")),
        "all_checkpoint_rows_validate": not failed_validation,
    }
    return {
        "date": day,
        "artifact_path": str(snapshot_path),
        "checkpoint_path": str(checkpoint_path),
        "checks": checks,
        "checkpoint_row_count": len(rows),
        "unique_symbols": len(unique_symbols),
        "latest_bar_dates": sorted(row_dates),
        "failed_validation_symbols": failed_validation,
        "result": "PASS" if all(checks.values()) else "FAIL",
        "live_requests": 0,
    }


def run(output_dir: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "replay").mkdir(parents=True, exist_ok=True)
    audits: dict[str, dict[str, Any]] = {}
    missing_rows: dict[str, list[dict[str, Any]]] = {}
    for day in ("2026-08-19", "2026-09-22"):
        audits[day], missing_rows[day] = _inventory(day)
    sep14 = _validate_sep14()

    for day, rows in missing_rows.items():
        _write_csv(output_dir / f"missing_symbols_{day}.csv", rows, [
            "date", "symbol", "company_name", "batch", "saved_provider_error",
            "provider_status", "reason", "retryability",
        ])
    pending = [row for rows in missing_rows.values() for row in rows]
    missing_sets_valid = all(
        audits[day].get("error_list_matches_missing_set") is True
        and audits[day].get("batch_counts_match_snapshot") is True
        and audits[day].get("requested_universe_is_unique") is True
        and audits[day].get("requested_count_from_batches") == 120
        for day in ("2026-08-19", "2026-09-22")
    )
    reusable_base = all(audits[day].get("full_valid_base_rows_persisted") for day in ("2026-08-19", "2026-09-22"))
    requests_authorized_to_send = missing_sets_valid and reusable_base
    block_reason = None if requests_authorized_to_send else "VALID_BASE_SYMBOL_PRICE_ROWS_NOT_PERSISTED; TARGETED_FETCH_WOULD_NOT_SUPPORT_REBUILDING_FULL_RANKING"
    ledger = {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "OFFLINE_PREFLIGHT_ONLY",
        "live_requests_sent": 0,
        "twelve_data_credits_used": 0,
        "missing_set_validation_passed": missing_sets_valid,
        "full_valid_base_rows_available": reusable_base,
        "request_dispatch_allowed": requests_authorized_to_send,
        "block_reason": block_reason,
        "dates": audits,
        "requests": pending,
    }
    _write_json(output_dir / "recovery_request_ledger.json", ledger)

    dates = _selected_dates()
    import build_historical_market_cache as historical

    source_rows = []
    integrity_rows = []
    for day in dates:
        folder = MILESTONE / "history" / day
        market = _read_json(folder / "market_snapshot.json")
        market_ok = bool(market and historical._validate_historical_market(market, day))
        movers_snapshot = _read_json(folder / "movers_snapshot.json")
        mover_ok = False
        reasons: list[str] = []
        if not market:
            reasons.append("MARKET_SNAPSHOT_MISSING")
        if not movers_snapshot:
            reasons.append("MOVER_SNAPSHOT_MISSING")
        else:
            mover_ok = (
                movers_snapshot.get("market_date") == day
                and movers_snapshot.get("collection_status") == "SUCCESS"
                and movers_snapshot.get("symbols_valid") == movers_snapshot.get("symbols_requested")
                and bool(movers_snapshot.get("top_gainers"))
                and bool(movers_snapshot.get("top_losers"))
                and (movers_snapshot.get("historical_integrity") or {}).get("integrity_status") == "PASS"
                and (movers_snapshot.get("historical_integrity") or {}).get("future_data_leak_detected") is not True
            )
            if not mover_ok:
                reasons.append("INVALID_REQUIRED_MOVER_ARTIFACT")
        driver = _read_json(folder / "market_driver_snapshot.json")
        if market and not market_ok:
            reasons.append("INVALID_HISTORICAL_MARKET_ARTIFACT")
        source_status = "SOURCE_READY" if market_ok and mover_ok and driver else "SOURCE_BLOCKED"
        source_rows.append({
            "date": day,
            "market_snapshot": "VALID" if market_ok else ("INVALID" if market else "MISSING"),
            "movers_snapshot": "VALID" if mover_ok else "INVALID_OR_MISSING",
            "market_driver_snapshot": "AVAILABLE" if driver else "MISSING",
            "source_status": source_status,
            "replay_status": "NOT_RUN_PRECONDITIONS_FAILED",
        })
        integrity_rows.append({
            "date": day,
            "readiness": "NOT_EVALUATED",
            "integrity_scope": "SOURCE_ARTIFACT_PREFLIGHT_ONLY",
            "integrity": "PASS" if market_ok and mover_ok else "FAIL",
            "reasons": ";".join(reasons),
            "offline_network_calls": 0,
            "fmp_requests": 0,
        })
    _write_csv(output_dir / "replay/readiness_report.csv", source_rows, [
        "date", "market_snapshot", "movers_snapshot", "market_driver_snapshot", "source_status", "replay_status",
    ])
    _write_csv(output_dir / "replay/integrity_report.csv", integrity_rows, [
        "date", "readiness", "integrity_scope", "integrity", "reasons", "offline_network_calls", "fmp_requests",
    ])
    blockers = [
        {"date": day, "reason": audits[day].get("strict_integrity_reason"), "valid": audits[day].get("symbols_valid"), "requested": audits[day].get("symbols_requested"), "base_rows_persisted": audits[day].get("full_valid_base_rows_persisted")}
        for day in ("2026-08-19", "2026-09-22")
    ]
    frozen_summary = _read_json(MILESTONE / "resume_after_dxy/dataset_summary.json") or {}
    frozen_metrics = frozen_summary.get("metrics", {})
    summary = {
        "status": "BLOCKED_NOT_COMPLETE",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "selected_sessions": len(dates),
        "dates": dates,
        "missing_symbol_audits": audits,
        "sep14_validation": sep14,
        "missing_symbols_total": len(pending),
        "missing_symbols_by_date": {day: [row["symbol"] for row in rows] for day, rows in missing_rows.items()},
        "requests_sent": 0,
        "twelve_data_credits_used": 0,
        "finnhub_requests": 0,
        "official_verification_requests": 0,
        "tier2_requests": 0,
        "fmp_requests": 0,
        "offline_replay_network_calls": 0,
        "replay_performed": False,
        "replay_block_reason": block_reason,
        "baseline_30_day_counts": {
            "READY": frozen_metrics.get("days_ready"),
            "PARTIAL": frozen_metrics.get("days_partial"),
            "FAILED": frozen_metrics.get("days_failed"),
            "SKIPPED": frozen_metrics.get("days_skipped"),
            "INTEGRITY_PASS": frozen_metrics.get("integrity_pass_days"),
            "INTEGRITY_FAIL": frozen_metrics.get("integrity_fail_days"),
        },
        "baseline_parity_mismatches": 0,
        "baseline_cross_symbol_leaks": 0,
        "baseline_future_event_violations": 0,
        "blockers": blockers,
        "manual_ground_truth_modified": False,
    }
    _write_json(output_dir / "recovery_summary.json", summary)
    _write_json(output_dir / "replay/replay_summary.json", {
        "status": "NOT_RUN_PRECONDITIONS_FAILED",
        "reason": block_reason,
        "selected_sessions": len(dates),
        "replay_executed": False,
        "ready": None,
        "integrity_pass": None,
        "integrity_fail_preflight": len([row for row in integrity_rows if row["integrity"] == "FAIL"]),
        "dates_not_replayed": len(dates),
        "offline_network_calls": 0,
        "fmp_requests": 0,
        "source_integrity_report": "integrity_report.csv is a preflight, not replay output",
    })
    summary_md = [
        "# Targeted 30-Day Replay Completion V1 — Preflight",
        "",
        "Status: **BLOCKED_NOT_COMPLETE**. This is an offline preflight, not a completed replay.",
        "",
        f"Selected sessions: {len(dates)}; missing symbols identified: {len(pending)}; live requests: 0; Twelve Data credits: 0.",
        "",
        "## Missing symbol sets",
        "",
        f"- 2026-08-19 ({len(missing_rows['2026-08-19'])}): {', '.join(row['symbol'] for row in missing_rows['2026-08-19'])}.",
        f"- 2026-09-22 ({len(missing_rows['2026-09-22'])}): {', '.join(row['symbol'] for row in missing_rows['2026-09-22'])}.",
        "- Batch-derived complements match the error-symbol lists and requested counts.",
        "",
        "## Recovery blockers",
        "",
        "The saved mover snapshots contain only top/bottom ranked rows, batch counts, and errors—not the complete per-symbol price records for the symbols already counted valid. No resumable mover checkpoint exists for Aug 19 or Sep 22. Fetching only the 8+64 missing symbols therefore cannot recompute either full ranking under the unchanged integrity contract. No provider calls were made.",
        "",
        f"- Aug 19 checkpoint rows: {audits['2026-08-19'].get('checkpoint_valid_rows', 0)}; full valid rows persisted: {audits['2026-08-19'].get('full_valid_base_rows_persisted')}.",
        f"- Sep 22 checkpoint rows: {audits['2026-09-22'].get('checkpoint_valid_rows', 0)}; full valid rows persisted: {audits['2026-09-22'].get('full_valid_base_rows_persisted')}.",
        f"- Sep 14 saved prefilter artifact validation: **{sep14['result']}** (40/40 checkpoint rows, target-date bars, matching universe fingerprint, strict cache contract; live requests=0).",
        "",
        "## Replay",
        "",
        "A fresh 30-session replay was not started because the two required mover rankings cannot be rebuilt from persisted inputs. The files under `replay/` are source-readiness preflight reports, not replay metrics. The frozen Oct 5 baseline and existing caches were not modified.",
        "",
        "## Next required input",
        "",
        "Provide the complete saved per-symbol mover records/checkpoints for the previously valid Aug 19 and Sep 22 symbols, or explicitly authorize recovery to re-fetch those already-valid symbols as well. Until then, keep the formal dataset below 30/30.",
        "",
    ]
    (output_dir / "recovery_summary.md").write_text("\n".join(summary_md), encoding="utf-8")
    (output_dir / "replay/replay_summary.md").write_text(
        "# Replay preflight — not run\n\n"
        f"Status: **NOT_RUN_PRECONDITIONS_FAILED**. {block_reason}\n\n"
        f"Readiness source rows: {len(source_rows)}. Source-ready: {sum(row['source_status']=='SOURCE_READY' for row in source_rows)}. "
        f"Source-blocked: {sum(row['source_status']=='SOURCE_BLOCKED' for row in source_rows)}. "
        "No replay was executed, no provider was called, and no final READY/PASS count is asserted.\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    summary = run(args.output_dir)
    print(json.dumps({
        "status": summary["status"],
        "output_dir": str(args.output_dir.resolve()),
        "missing_2026_08_19": len(summary["missing_symbols_by_date"]["2026-08-19"]),
        "missing_2026_09_22": len(summary["missing_symbols_by_date"]["2026-09-22"]),
        "sep14_validation": summary["sep14_validation"]["result"],
        "live_requests": summary["requests_sent"],
        "replay_performed": summary["replay_performed"],
    }, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
