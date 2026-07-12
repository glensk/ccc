"""Cross-session file locks: store mechanics + the Pre/PostToolUse hooks + `ccc handoff`."""

from __future__ import annotations

import json
from argparse import Namespace
from collections.abc import Iterator
from pathlib import Path

import pytest

from command_center import cli, config, gitcommit, hooks
from command_center.models import now_ms
from command_center.store import Store

TTL = 30 * 60 * 1000  # ms


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    """A store under a temp CLAUDE_HOME — hooks/CLI open their own Store() at the same path."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    s = Store()
    try:
        yield s
    finally:
        s.close()


# ---- store mechanics ----------------------------------------------------
def test_acquire_contended_then_released(store: Store) -> None:
    """The AIM's reproducing case: A holds F → B blocked → A releases → B acquires."""
    store.ensure("A")
    store.ensure("B")
    live = {"A", "B"}
    t = 1_000_000
    assert store.acquire_file_lock("A", "/f.py", t, live, TTL) is None  # A takes it
    assert store.acquire_file_lock("B", "/f.py", t, live, TTL) == "A"  # B blocked by live A
    assert store.acquire_file_lock("A", "/f.py", t + 1, live, TTL) is None  # A re-acquire refreshes
    assert store.release_file_lock("A", "/f.py") is True
    assert store.acquire_file_lock("B", "/f.py", t + 2, live, TTL) is None  # now B may take it


def test_dead_holder_is_reclaimed(store: Store) -> None:
    store.ensure("A")
    store.ensure("B")
    t = 1_000_000
    assert store.acquire_file_lock("A", "/f.py", t, {"A", "B"}, TTL) is None
    assert store.acquire_file_lock("B", "/f.py", t, {"B"}, TTL) is None  # A not live → reclaimed


def test_stale_lock_is_reclaimed(store: Store) -> None:
    store.ensure("A")
    store.ensure("B")
    t = 1_000_000
    assert store.acquire_file_lock("A", "/f.py", t, {"A", "B"}, TTL) is None
    # Past the TTL, A is reclaimable even though it is still live.
    assert store.acquire_file_lock("B", "/f.py", t + TTL + 1, {"A", "B"}, TTL) is None


def test_waiters_and_release_all(store: Store) -> None:
    store.ensure("A")
    store.ensure("B")
    t = 1_000_000
    store.acquire_file_lock("A", "/f.py", t, {"A", "B"}, TTL)
    store.add_waiter("B", "/f.py", t)
    store.add_waiter("B", "/f.py", t)  # idempotent
    waiters = store.waiters_on_my_locks("A")
    assert [(w.session_id, w.file_path) for w in waiters] == [("B", "/f.py")]
    assert store.release_all_file_locks("A") == 1
    assert store.waiters_on_my_locks("A") == []  # A holds nothing now


def test_list_file_locks_filters_invalid(store: Store) -> None:
    store.ensure("A")
    t = 1_000_000
    store.acquire_file_lock("A", "/f.py", t, {"A"}, TTL)
    assert [lock.file_path for lock in store.list_file_locks({"A"}, TTL, t)] == ["/f.py"]
    assert store.list_file_locks(set(), TTL, t) == []  # holder not live
    assert store.list_file_locks({"A"}, TTL, t + TTL + 1) == []  # stale


# ---- PreToolUse hook ----------------------------------------------------
def test_pre_tool_use_denies_when_held(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store.ensure("A")
    store.ensure("B")
    monkeypatch.setattr(hooks, "_live_ids", lambda: {"A", "B"})
    store.acquire_file_lock("A", "/repo/f.py", now_ms(), {"A", "B"}, TTL)
    rc = hooks.handle_pre_tool_use(
        {
            "session_id": "B",
            "cwd": "/repo",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/repo/f.py"},
        }
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    decision = out["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "/repo/f.py" in decision["permissionDecisionReason"]
    assert [w.session_id for w in store.waiters_on_my_locks("A")] == ["B"]  # B queued


def test_pre_tool_use_allows_when_free(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store.ensure("B")
    monkeypatch.setattr(hooks, "_live_ids", lambda: {"B"})
    rc = hooks.handle_pre_tool_use(
        {
            "session_id": "B",
            "cwd": "/repo",
            "tool_name": "Write",
            "tool_input": {"file_path": "/repo/g.py"},
        }
    )
    assert rc == 0
    assert capsys.readouterr().out == ""  # no decision → the edit proceeds
    assert [lk.session_id for lk in store.list_file_locks({"B"}, TTL, now_ms())] == [
        "B"
    ]  # B holds it


def test_pre_tool_use_fails_open_when_disabled(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(hooks.config, "load_config", lambda: config.Config(file_lock_enabled=False))
    rc = hooks.handle_pre_tool_use(
        {"session_id": "B", "tool_name": "Edit", "tool_input": {"file_path": "/repo/f.py"}}
    )
    assert rc == 0
    assert capsys.readouterr().out == ""
    assert store.list_file_locks({"B"}, TTL, now_ms()) == []  # nothing acquired


# ---- PostToolUse hook (eager-handoff nudge) -----------------------------
def test_post_tool_edit_nudges_handoff(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    store.ensure("A")
    store.ensure("B")
    store.acquire_file_lock("A", "/repo/f.py", now_ms(), {"A", "B"}, TTL)
    store.add_waiter("B", "/repo/f.py", now_ms())
    rc = hooks.handle_post_tool_use(
        {"session_id": "A", "tool_name": "Edit", "tool_input": {"file_path": "/repo/f.py"}}
    )
    assert rc == 0
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "ccc handoff /repo/f.py" in ctx


# ---- ccc handoff --------------------------------------------------------
def test_handoff_commits_then_releases(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store.ensure("A")
    store.acquire_file_lock("A", "/repo/f.py", now_ms(), {"A"}, TTL)
    monkeypatch.setattr(gitcommit, "commit_and_push", lambda repo, paths, msg, **k: (True, "ok"))
    rc = cli.cmd_handoff(Namespace(file="/repo/f.py", message="", session="A"))
    assert rc == 0
    assert store.list_file_locks({"A"}, TTL, now_ms()) == []  # released after commit


def test_handoff_keeps_lock_when_commit_fails(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store.ensure("A")
    store.acquire_file_lock("A", "/repo/f.py", now_ms(), {"A"}, TTL)
    monkeypatch.setattr(gitcommit, "commit_and_push", lambda repo, paths, msg, **k: (False, "boom"))
    rc = cli.cmd_handoff(Namespace(file="/repo/f.py", message="m", session="A"))
    assert rc == 1
    assert [lk.session_id for lk in store.list_file_locks({"A"}, TTL, now_ms())] == ["A"]  # kept
