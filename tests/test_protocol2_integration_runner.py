"""Safety boundary checks; stateful protocol assertions run in local workerd."""
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def runner(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "cloudflare/marketbot-state-worker/scripts"
    monkeypatch.syspath_prepend(str(scripts))
    return importlib.import_module("protocol2_integration")


@pytest.mark.parametrize("args", [
    ["--worker-url", "https://marketbot-state-api.yossihabar1994.workers.dev"],
    ["--database-id", "3f40e99b-8fb3-4fb8-90c7-ea1b4e019061"],
    ["--worker-url", "https://unrelated.invalid"],
    ["--local", "--database-id", "3f40e99b-8fb3-4fb8-90c7-ea1b4e019061"],
])
def test_production_or_unrecognized_target_rejected_before_credentials(runner, monkeypatch, args):
    monkeypatch.setattr(runner.legacy, "get_test_credentials", lambda: pytest.fail("credentials requested"))
    monkeypatch.setattr(runner.legacy.requests, "post", lambda *a, **k: pytest.fail("network requested"))
    with pytest.raises(ValueError, match="TEST_TARGET_REJECTED"):
        runner.main(args)


def test_default_plan_has_no_network(runner, monkeypatch):
    monkeypatch.setattr(runner.legacy.requests, "post", lambda *a, **k: pytest.fail("network requested"))
    assert runner.main([]) == 0


def test_remote_requires_explicit_database_before_credentials(runner, monkeypatch):
    monkeypatch.setattr(runner.legacy, "get_test_credentials", lambda: pytest.fail("credentials requested"))
    with pytest.raises(ValueError, match="EXPLICIT_TEST_DATABASE_REQUIRED"):
        runner.main(["--execute-isolated-remote"])


def test_local_transport_cannot_contact_production_and_disables_redirects(runner, monkeypatch):
    seen = []
    monkeypatch.setattr(runner.legacy.requests, "post", lambda *a, **k: seen.append((a, k)))
    transport = runner.LocalTransport()
    with pytest.raises(ValueError, match="LOCAL_TARGET_REJECTED"):
        transport.post(runner.legacy.PRODUCTION_URL + "/v1/state")
    assert not seen
    transport.post(runner.legacy.TEST_URL + "/v1/state", data=b"synthetic")
    assert seen == [((runner.LOCAL_URL,), {"data": b"synthetic", "allow_redirects": False})]


def test_remote_state_preflight_failure_prevents_mutation_suite(runner, monkeypatch):
    monkeypatch.setattr(runner.legacy, "get_test_credentials", lambda: ("x" * 48, "y" * 48))
    def reject(**kwargs):
        raise ValueError("ISOLATION_REJECTED")
    monkeypatch.setattr(runner.legacy, "assert_remote_test_database_isolated", reject)
    monkeypatch.setattr(runner, "run_suite", lambda *a: pytest.fail("mutation suite entered"))
    with pytest.raises(ValueError, match="ISOLATION_REJECTED"):
        runner.main(["--execute-isolated-remote", "--database-id", runner.legacy.TEST_DATABASE_ID])


@pytest.mark.parametrize("change", [
    {"delivery_protocol_version": 1}, {"schema_version": 2},
    {"test_mutation_mode": False},
    {"ownership": {"cloud_quota_exclusive": True}},
    {"runs": {"2099-02-01": {"status": "RUNNING"}}},
])
def test_invalid_worker_identity_or_reused_date_stops_before_mutation(runner, change):
    class Client:
        def call(self, op, payload, **kwargs):
            assert op == "state.get", "mutation attempted before identity validation"
            return {"schema_version": 1, "delivery_protocol_version": 2,
                    "test_mutation_mode": True, "runs": {},
                    "ownership": dict.fromkeys(("cloud_quota_exclusive", "windows_sender_disabled", "cloud_delivery_enabled"), False),
                    **change}
    with pytest.raises(AssertionError):
        runner.run_suite(Client())


def test_journal_records_synthetic_attempt_without_secret(runner):
    result = runner.legacy._safe_journal_context({"attempt_id": "3c" + "a" * 30,
                                                "sender_stopped": True,
                                                "secret": "never-record-this"})
    assert result == {"attempt_id": "3c" + "a" * 30, "sender_stopped": True}
