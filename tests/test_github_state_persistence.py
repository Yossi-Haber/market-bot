import json

import pytest

from persistence.github_state import GitHubStateStore, StateUnavailable
from tests.persistence_fixtures import git


def test_two_competing_runners_resolve_fast_forward_conflict(state_repos):
    _, first_repo, second_repo = state_repos
    first = GitHubStateStore(first_repo)
    second = GitHubStateStore(second_repo)

    def claim_one(state):
        state["runs"]["2026-10-13"] = {"status": "CLAIMED", "run_id": "1" * 32}

    def claim_two(state):
        state["runs"]["2026-10-14"] = {"status": "CLAIMED", "run_id": "2" * 32}

    first.update(claim_one)
    second.update(claim_two, fetch=False)
    result = GitHubStateStore(second_repo).read(fetch=True)
    assert set(result["runs"]) == {"2026-10-13", "2026-10-14"}


def test_missing_or_corrupt_state_fails_closed(state_repos):
    _, repo, _ = state_repos
    store = GitHubStateStore(repo)
    store.path.write_text("{bad json", encoding="utf-8")
    with pytest.raises(StateUnavailable, match="CORRUPT"):
        store.read()


def test_publish_failure_does_not_return_accepted_state_claim(state_repos, monkeypatch):
    _, repo, _ = state_repos
    store = GitHubStateStore(repo)
    real = store._git

    def fail_push(*args, **kwargs):
        if args and args[0] == "push":
            raise StateUnavailable("synthetic push failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "_git", fail_push)
    with pytest.raises(StateUnavailable, match="synthetic push failure|PUBLICATION_FAILED"):
        store.update(lambda state: state["runs"].update({"2026-10-14": {"status": "CLAIMED"}}))
    # A local orphan commit is not accepted as durable state.
    assert GitHubStateStore(repo).read(fetch=True)["runs"] == {}


def test_state_branch_publication_contains_only_state_json(state_repos):
    _, repo, _ = state_repos
    store = GitHubStateStore(repo)
    store.update(lambda state: state["runs"].update({"2026-10-14": {"status": "CLAIMED"}}))
    assert git("ls-files", cwd=repo).splitlines() == ["marketbot-state.json"]
    serialized = json.dumps(store.read(fetch=True))
    assert "TELEGRAM_BOT_TOKEN" not in serialized
    assert "chat_id" not in serialized
