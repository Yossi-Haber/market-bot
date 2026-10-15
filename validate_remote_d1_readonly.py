"""Safely validate the deployed D1 Worker using authenticated state.get reads only."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from persistence.d1_client import D1StateClient, D1StateError


DEFAULT_WORKER_URL = "https://marketbot-state-api.yossihabar1994.workers.dev"
DEFAULT_REPORT = Path("output/qa/github_actions_migration_phase3a/remote_validation_report.md")
EXPECTED_OWNERSHIP_KEYS = {
    "cloud_quota_exclusive",
    "windows_sender_disabled",
    "cloud_delivery_enabled",
}


@dataclass
class StatusRecordingSession:
    """Transparent requests adapter that records status codes, never request data."""

    wrapped: Any
    statuses: list[int] = field(default_factory=list)

    def post(self, *args: Any, **kwargs: Any) -> Any:
        response = self.wrapped.post(*args, **kwargs)
        self.statuses.append(int(response.status_code))
        return response


def summarize_read(state: dict[str, Any]) -> dict[str, Any]:
    """Reduce state.get output to non-sensitive validation facts."""
    if state.get("schema_version") != 1:
        raise D1StateError("D1_STATE_SCHEMA_INVALID")
    ownership = state.get("ownership")
    if not isinstance(ownership, dict) or set(ownership) != EXPECTED_OWNERSHIP_KEYS:
        raise D1StateError("D1_OWNERSHIP_SCHEMA_INVALID")
    if any(type(value) is not bool for value in ownership.values()):
        raise D1StateError("D1_OWNERSHIP_VALUES_INVALID")
    runs = state.get("runs")
    if not isinstance(runs, dict):
        raise D1StateError("D1_STATE_SCHEMA_INVALID")
    return {
        "schema_version": 1,
        "run_count": len(runs),
        "ownership": {key: ownership[key] for key in sorted(EXPECTED_OWNERSHIP_KEYS)},
    }


def validate_read_only(client: D1StateClient, statuses: list[int]) -> dict[str, Any]:
    """Perform two authenticated reads; no quota, run, delivery, or admin operation."""
    first = client.read()
    first_summary = summarize_read(first)
    if not statuses or statuses[-1] != 200:
        raise D1StateError("D1_STATE_READ_HTTP_STATUS_INVALID")

    # Re-read state as the final gate check. Authentication may append request
    # nonce rows, but state.get does not alter quota, run, delivery, or ownership.
    final = client.read()
    final_summary = summarize_read(final)
    if not statuses or statuses[-1] != 200:
        raise D1StateError("D1_FINAL_STATE_READ_HTTP_STATUS_INVALID")

    return {
        "operation": "state.get",
        "http_statuses": list(statuses),
        "schema_compatible": True,
        "first_read": first_summary,
        "final_read": final_summary,
        "reads_consistent": first_summary == final_summary,
        "ownership_unchanged_and_closed": (
            first_summary["ownership"] == final_summary["ownership"]
            and all(value is False for value in final_summary["ownership"].values())
        ),
        "state_changing_operations": 0,
        "note": "Worker authentication stores request nonce records even for reads; no quota/run/delivery/ownership operation was called.",
    }


def write_report(
    path: Path,
    result: dict[str, Any],
    verdict: str,
    error_code: str | None = None,
    worker_url: str = DEFAULT_WORKER_URL,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    observed_requests = len(result.get("http_statuses", []))
    lines = [
        "# Phase 3A — Remote Cloudflare D1 integration validation",
        "",
        f"- Generated (UTC): {datetime.now(timezone.utc).isoformat()}",
        f"- Worker: `{worker_url}`",
        f"- Verdict: **{verdict}**",
        "- Secret values: not recorded or displayed",
        "- Provider/Telegram/GitHub calls: 0",
        f"- Remote Worker/D1 HTTP requests in this validation: {observed_requests}",
        "",
    ]
    if "final_read" in result:
        lines.extend(
            [
                f"- Operation: `{result['operation']}` only",
                f"- HTTP statuses: `{','.join(map(str, result['http_statuses']))}`",
                f"- Schema version: `{result['final_read']['schema_version']}` (compatible={result['schema_compatible']})",
                f"- Run claims visible: `{result['final_read']['run_count']}` (count only; records omitted)",
                f"- Ownership flags: `{json.dumps(result['final_read']['ownership'], sort_keys=True)}`",
                f"- Reads consistent: `{result['reads_consistent']}`",
                f"- Ownership unchanged and closed: `{result['ownership_unchanged_and_closed']}`",
                f"- State-changing business operations: `{result['state_changing_operations']}`",
                f"- Physical nonce bookkeeping: `{len(result['http_statuses'])} authenticated requests may append nonce rows`",
                f"- Note: {result['note']}",
            ]
        )
    else:
        lines.extend(
            [
                "- Authenticated operations: not completed",
                f"- HTTP status codes observed: `{','.join(map(str, result.get('http_statuses', []))) or 'none'}`",
                "- Ownership values: not re-read in this validation",
                "- State-changing operations: 0",
                "- Authentication nonce rows appended: 0",
            ]
        )
    if error_code:
        lines.append(f"- Safe result code: `{error_code}`")
    lines.extend(
        [
            "",
            "## Scope and limitations",
            "",
            "The Worker API has no isolated test namespace. This utility therefore calls only authenticated `state.get` and does not exercise writes, idempotency, concurrent claims, validation failures, or persistence of synthetic records. Those require a separate disposable D1 database/Worker or a Worker-side test namespace with hard isolation from production tables.",
            "",
            "Even `state.get` authentication inserts a nonce into `request_nonces`; that security bookkeeping is the only expected remote mutation. It does not touch ownership, quota reservations, run claims, or delivery rows.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def get_secret(*, prompt_for_secret: bool = False) -> str:
    secret = os.getenv("MARKETBOT_D1_STATE_HMAC_SECRET", "") or os.getenv("STATE_API_HMAC_SECRET", "")
    if not secret and prompt_for_secret:
        if not sys.stdin.isatty():
            raise D1StateError("D1_SECRET_MISSING_NONINTERACTIVE")
        secret = getpass.getpass("Worker caller HMAC secret (input hidden): ")
    if not secret:
        raise D1StateError("D1_WORKER_CONFIGURATION_MISSING_USE_SECURE_PROMPT")
    if len(secret.encode("utf-8")) < 32:
        raise D1StateError("D1_WORKER_SECRET_INVALID")
    return secret


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-url", default=DEFAULT_WORKER_URL)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--prompt-for-secret", action="store_true", help="Prompt with hidden input if MARKETBOT_D1_STATE_HMAC_SECRET is unset.")
    args = parser.parse_args()

    result: dict[str, Any] = {}
    session: StatusRecordingSession | None = None
    try:
        secret = get_secret(prompt_for_secret=args.prompt_for_secret)
        session = StatusRecordingSession(wrapped=requests)
        client = D1StateClient(args.worker_url, secret, session=session)
        result = validate_read_only(client, session.statuses)
        verdict = "REMOTE_INTEGRATION_VALIDATED" if result["reads_consistent"] and result["ownership_unchanged_and_closed"] else "BLOCKERS_TO_RESOLVE"
        write_report(args.report, result, verdict, worker_url=args.worker_url)
        print(f"VERDICT={verdict}")
        print(f"OPERATION={result['operation']}")
        print(f"HTTP_STATUSES={','.join(map(str, result['http_statuses']))}")
        print(f"SCHEMA_VERSION={result['final_read']['schema_version']}")
        print(f"RUN_COUNT={result['final_read']['run_count']}")
        print(f"OWNERSHIP_FLAGS={json.dumps(result['final_read']['ownership'], sort_keys=True)}")
        print(f"READS_CONSISTENT={result['reads_consistent']}")
        print("STATE_CHANGING_OPERATIONS=0")
        print(f"REPORT={args.report}")
        return 0 if verdict == "REMOTE_INTEGRATION_VALIDATED" else 1
    except Exception as exc:
        error_code = str(exc) if isinstance(exc, D1StateError) else "REMOTE_READ_FAILED"
        if session is not None:
            result["http_statuses"] = list(session.statuses)
        write_report(args.report, result, "BLOCKERS_TO_RESOLVE", error_code, args.worker_url)
        print("VERDICT=BLOCKERS_TO_RESOLVE")
        print(f"SAFE_ERROR={error_code}")
        print("STATE_CHANGING_OPERATIONS=0")
        print(f"REPORT={args.report}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
