"""Local bare-Git test fixtures; never touch a configured project cache/state."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from persistence.schema import new_state


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise AssertionError(f"git fixture setup failed: {args[0]}")
    return result.stdout


@pytest.fixture
def state_repos(tmp_path):
    """Return isolated bare remote and two clones with an existing state branch."""
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(seed)], check=True, capture_output=True)
    git("config", "user.name", "Offline Test", cwd=seed)
    git("config", "user.email", "offline-test@example.invalid", cwd=seed)
    git("remote", "add", "origin", str(remote), cwd=seed)
    git("checkout", "--orphan", "marketbot-state", cwd=seed)
    (seed / "marketbot-state.json").write_text(json.dumps(new_state(), indent=2) + "\n", encoding="utf-8")
    git("add", "--", "marketbot-state.json", cwd=seed)
    git("commit", "-m", "test state genesis", cwd=seed)
    git("push", "-u", "origin", "marketbot-state", cwd=seed)
    clones = []
    for label in ("one", "two"):
        clone = tmp_path / label
        subprocess.run(["git", "clone", "--branch", "marketbot-state", str(remote), str(clone)], check=True, capture_output=True)
        git("config", "user.name", "Offline Test", cwd=clone)
        git("config", "user.email", "offline-test@example.invalid", cwd=clone)
        clones.append(clone)
    return remote, clones[0], clones[1]
