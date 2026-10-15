from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from persistence.schema import StateValidationError, new_state, seal_state, validate_state


def test_schema_checksum_and_versioned_genesis():
    state = new_state(now=datetime(2026, 10, 14, 12, tzinfo=timezone.utc))
    assert validate_state(state)["schema_version"] == 1
    damaged = deepcopy(state)
    damaged["generation"] = 1
    with pytest.raises(StateValidationError, match="CHECKSUM_MISMATCH"):
        validate_state(damaged)


@pytest.mark.parametrize("field", ["TWELVE_DATA_API_KEY", "chat_id", "authorization", "bot_token"])
def test_schema_rejects_secret_and_raw_recipient_fields(field):
    state = new_state()
    state["audit"] = [{field: "synthetic-secret"}]
    with pytest.raises(StateValidationError, match="SENSITIVE_FIELD"):
        validate_state(seal_state(state))


def test_schema_rejects_stale_and_incompatible_state():
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    state = new_state(now=now - timedelta(days=31))
    with pytest.raises(StateValidationError, match="STALE"):
        validate_state(state, now=now)
    state = new_state(now=now)
    state["schema_version"] = 2
    with pytest.raises(StateValidationError, match="VERSION_UNSUPPORTED"):
        validate_state(seal_state(state), now=now)


def test_state_never_accepts_raw_recipient_slot():
    state = new_state()
    state["deliveries"]["2026-10-14"] = {"recipients": {"123456789": {"status": "PENDING"}}}
    with pytest.raises(StateValidationError, match="SLOT_INVALID"):
        validate_state(seal_state(state))
