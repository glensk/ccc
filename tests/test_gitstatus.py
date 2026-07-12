"""Tests for the compact git-status helper (real git in a temp repo)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from command_center import gitstatus


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


def test_short_non_repo_and_states(tmp_path: Path) -> None:
    # Not a git repo -> blank.
    assert gitstatus.short(str(tmp_path)) == ("", "grey42")
    assert gitstatus.short("") == ("", "grey42")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "init")
    # Clean repo -> ✓ (bypass the cache for the fresh state).
    gitstatus._cache.clear()
    assert gitstatus.short(str(repo)) == ("✓", "green")

    # Dirty repo -> ●
    (repo / "a.txt").write_text("changed", encoding="utf-8")
    gitstatus._cache.clear()
    assert gitstatus.short(str(repo)) == ("●", "#ff8800")
