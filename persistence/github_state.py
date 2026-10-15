"""Fast-forward-only Git state store for an isolated state-branch checkout."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from persistence.schema import advance_state, new_state, validate_state

STATE_BRANCH = "marketbot-state"
STATE_FILENAME = "marketbot-state.json"
MAX_CONFLICT_RETRIES = 3


class StateUnavailable(RuntimeError):
    """Critical state cannot safely be read or published."""


class StateConflict(StateUnavailable):
    """A concurrent writer changed state and bounded retry was exhausted."""


class GitHubStateStore:
    """Use a dedicated clone whose only tracked file is the bounded state JSON."""

    def __init__(self, repo_path: Path, *, branch: str = STATE_BRANCH, filename: str = STATE_FILENAME, max_conflict_retries: int = MAX_CONFLICT_RETRIES) -> None:
        if branch != STATE_BRANCH or filename != STATE_FILENAME:
            raise ValueError("STATE_BRANCH_OR_FILENAME_NOT_ALLOWED")
        self.repo_path = Path(repo_path).resolve()
        self.branch = branch
        self.path = self.repo_path / filename
        self.max_conflict_retries = max(1, min(int(max_conflict_retries), 5))

    def read(self, *, fetch: bool = False) -> dict[str, Any]:
        """Read a validated state snapshot; optionally fetch the remote tip first."""
        self._require_repository()
        if fetch:
            self._fetch_existing_branch()
            self._checkout_remote_tip()
        self._require_isolated_checkout()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise StateUnavailable("STATE_FILE_MISSING; operator bootstrap required") from None
        except (OSError, json.JSONDecodeError):
            raise StateUnavailable("STATE_FILE_CORRUPT; operator recovery required") from None
        try:
            return validate_state(payload)
        except ValueError as exc:
            raise StateUnavailable(str(exc)) from None

    def update(self, mutate: Callable[[dict[str, Any]], Any], *, fetch: bool = True) -> tuple[dict[str, Any], Any]:
        """Apply a mutation and publish with ordinary fast-forward pushes only."""
        self._require_repository()
        if not self._has_remote_branch():
            raise StateUnavailable("STATE_BRANCH_MISSING; remote branch creation is disabled")
        for attempt in range(self.max_conflict_retries):
            if fetch or attempt:
                self._fetch_existing_branch()
                self._checkout_remote_tip()
            state = self.read()
            candidate = json.loads(json.dumps(state, ensure_ascii=False))
            try:
                result = mutate(candidate)
            except Exception:
                raise
            try:
                published = validate_state(advance_state(candidate))
            except ValueError as exc:
                raise StateUnavailable(f"STATE_UPDATE_REJECTED:{exc}") from None
            try:
                self._atomic_write(published)
                self._git("add", "--", self.path.name)
                self._git("-c", "user.name=Market Bot State", "-c", "user.email=marketbot-state@localhost", "commit", "-m", f"marketbot state generation {published['generation']}")
                self._git("push", "origin", f"HEAD:refs/heads/{self.branch}")
                return validate_state(published), result
            except StateConflict:
                if attempt + 1 >= self.max_conflict_retries:
                    raise
                continue
            except StateUnavailable:
                raise
            except Exception as exc:
                self._restore_remote_tip_best_effort()
                raise StateUnavailable("STATE_PUBLICATION_FAILED; no external operation is authorized") from None
        raise StateConflict("STATE_CONFLICT_RETRY_EXHAUSTED")

    def _require_repository(self) -> None:
        if not (self.repo_path / ".git").exists():
            raise StateUnavailable("STATE_REPOSITORY_MISSING")
        current = self._git("branch", "--show-current", check=False).strip()
        if current != self.branch:
            raise StateUnavailable("STATE_CHECKOUT_NOT_ON_MARKETBOT_STATE")

    def _require_isolated_checkout(self) -> None:
        tracked = self._git("ls-files", check=False).splitlines()
        if tracked != [self.path.name]:
            raise StateUnavailable("STATE_CHECKOUT_NOT_ISOLATED")

    def _has_remote_branch(self) -> bool:
        result = self._git("ls-remote", "--heads", "origin", f"refs/heads/{self.branch}", check=False)
        return bool(result.strip())

    def _fetch_existing_branch(self) -> None:
        if not self._has_remote_branch():
            raise StateUnavailable("STATE_BRANCH_MISSING; remote branch creation is disabled")
        self._git("fetch", "--no-tags", "origin", f"refs/heads/{self.branch}:refs/remotes/origin/{self.branch}")

    def _checkout_remote_tip(self) -> None:
        self._git("checkout", "-B", self.branch, f"refs/remotes/origin/{self.branch}")

    def _restore_remote_tip_best_effort(self) -> None:
        self._git("fetch", "--no-tags", "origin", f"refs/heads/{self.branch}:refs/remotes/origin/{self.branch}", check=False)
        self._git("reset", "--hard", f"refs/remotes/origin/{self.branch}", check=False)

    def _atomic_write(self, payload: dict[str, Any]) -> None:
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".marketbot-state-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def _git(self, *args: str, check: bool = True) -> str:
        try:
            result = subprocess.run(["git", "-C", str(self.repo_path), *args], capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise StateUnavailable("STATE_GIT_COMMAND_FAILED") from None
        if check and result.returncode:
            combined = f"{result.stdout}\n{result.stderr}".lower()
            if "non-fast-forward" in combined or "fetch first" in combined or "rejected" in combined:
                raise StateConflict("STATE_FAST_FORWARD_CONFLICT")
            raise StateUnavailable("STATE_GIT_COMMAND_FAILED")
        return result.stdout


def bootstrap_state_for_operator(repo_path: Path) -> Path:
    """Create a genesis file only in an already-existing state branch checkout.

    This helper never creates a branch or pushes. The operator must review and
    publish genesis state through a separately authorized bootstrap procedure.
    """
    store = GitHubStateStore(repo_path)
    store._require_repository()
    if store._git("ls-files", check=False).splitlines() not in ([], [store.path.name]):
        raise StateUnavailable("STATE_CHECKOUT_NOT_ISOLATED")
    if store.path.exists():
        raise StateUnavailable("STATE_FILE_ALREADY_EXISTS")
    store._atomic_write(new_state())
    return store.path
