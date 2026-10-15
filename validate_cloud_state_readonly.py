"""Fail-closed, read-only-business-state check for the production D1 Worker.

The Worker persists signed-request nonces to prevent replay. This check does not
change quota, run, delivery, or ownership state, but does create that short-lived
authentication record.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from persistence.d1_client import D1StateClient, D1StateError

EXPECTED_WORKER_URL = "https://marketbot-state-api.yossihabar1994.workers.dev"
OWNERSHIP_FIELDS = (
    "cloud_quota_exclusive",
    "windows_sender_disabled",
    "cloud_delivery_enabled",
)


def validate_state_payload(state: dict[str, Any]) -> dict[str, Any]:
    """Require supported schema and all production ownership gates closed."""
    if state.get("schema_version") != 1:
        raise ValueError("CLOUD_STATE_SCHEMA_INVALID")
    ownership = state.get("ownership")
    if not isinstance(ownership, dict):
        raise ValueError("CLOUD_STATE_OWNERSHIP_INVALID")
    for field in OWNERSHIP_FIELDS:
        if field not in ownership or type(ownership[field]) is not bool:
            raise ValueError("CLOUD_STATE_OWNERSHIP_INVALID")
        if ownership[field] is not False:
            raise ValueError("CLOUD_OWNERSHIP_GATE_NOT_CLOSED")
    if not isinstance(state.get("runs"), dict):
        raise ValueError("CLOUD_STATE_RUNS_INVALID")
    return {
        "schema_version": 1,
        "ownership": {field: False for field in OWNERSHIP_FIELDS},
        "run_count": len(state["runs"]),
    }


def main() -> int:
    """Perform exactly one authenticated state.get against the pinned Worker."""
    configured_url = os.getenv("MARKETBOT_D1_STATE_URL", "").strip().rstrip("/")
    if configured_url != EXPECTED_WORKER_URL:
        print("CLOUD_STATE_READONLY_FAILED reason=CLOUD_WORKER_ENDPOINT_NOT_PINNED")
        return 1
    if not os.getenv("MARKETBOT_D1_STATE_HMAC_SECRET", ""):
        print("CLOUD_STATE_READONLY_FAILED reason=CLOUD_STATE_CREDENTIAL_MISSING")
        return 1
    try:
        state = D1StateClient.from_env().read()
        result = validate_state_payload(state)
    except (D1StateError, ValueError) as exc:
        # D1StateError messages are sanitized machine codes; never print request
        # headers, URLs with credentials, or response payloads.
        reason = str(exc)
        if not reason.isascii() or len(reason) > 100:
            reason = "CLOUD_STATE_VALIDATION_FAILED"
        print(f"CLOUD_STATE_READONLY_FAILED reason={reason}")
        return 1
    print(
        "CLOUD_STATE_READONLY=PASS schema_version=1 "
        "ownership=0,0,0 nonce_bookkeeping=expected"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
