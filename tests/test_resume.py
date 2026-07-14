"""Tests for the resume-halted orchestrator's pure planner (and candidate finder).

The planner ``resume.plan`` is a pure function over (observed state, queue, now,
config, reset-signal) → (next queue, actions). These tests exercise the tricky
behaviour with no spawning: the reset gate, the global stagger, per-repo serial
dispatch, the transcript-progress finish signal, fresh-pid reap ordering, the
bounded requeue/fail ladder, manual-resume adoption, and done-pruning.
"""

from __future__ import annotations

import json
from pathlib import Path

from command_center.config import Config
from command_center.models import LiveSession
from command_center.resume import (
    Entry,
    Observation,
    QueueState,
    _is_drained,
    _state_path,
    candidates,
    load_state,
    plan,
    repo_of,
    save_state,
    will_auto_resume,
)
from command_center.store import Store

NOW = 1_000_000_000_000  # arbitrary epoch ms
# The reset gate is keyed per Claude account. These planner tests are single-account, so
# every entry carries the default key (""); the multi-account gating is covered by
# test_multi_account.py and test_two_accounts_gate_independently below.
ACCT = ""


def _cfg(**kw: object) -> Config:
    cfg = Config()
    for key, value in kw.items():
        setattr(cfg, key, value)
    return cfg


def _obs(
    *,
    alive: bool = False,
    raw: str = "",
    halted: bool = False,
    size: int = 0,
    cwd: str = "/r1",
    repo: str = "/r1",
    account: str = ACCT,
) -> Observation:
    return Observation(
        alive=alive,
        raw_status=raw,
        halted=halted,
        transcript_size=size,
        cwd=cwd,
        repo=repo,
        account=account,
    )


def _kinds(actions: list) -> list[str]:
    return [a.kind for a in actions]


def _launch_ids(actions: list) -> list[str]:
    return [a.session_id for a in actions if a.kind == "launch_resume"]


# --------------------------------------------------------------------------- #
# reset gate
# --------------------------------------------------------------------------- #
def test_enqueues_and_waits_for_reset() -> None:
    observed = {"a": _obs(halted=True)}
    state, actions = plan(observed, {"a"}, QueueState(), NOW, _cfg(), reset_signals=set())
    assert state.entries["a"].state == "queued"
    assert "ensure_reset_wait" in _kinds(actions)
    assert "launch_resume" not in _kinds(actions)
    assert not state.reset_confirmed_at  # gate still closed


def test_legacy_entry_account_is_backfilled_from_observation() -> None:
    """A queue entry persisted BEFORE the per-account gate is re-stamped, not stranded.

    Such an entry carries the default key ("") whatever seat it ran on; left alone it
    would wait forever on the wrong account's gate.
    """
    entries = {"a": Entry("a", repo="/r1", cwd="/r1", account=ACCT)}  # legacy: no account
    observed = {"a": _obs(halted=True, account="work")}
    state, actions = plan(
        observed, {"a"}, QueueState(entries=entries), NOW, _cfg(), reset_signals=set()
    )
    assert state.entries["a"].account == "work"  # re-stamped from the live observation
    assert [a.account for a in actions if a.kind == "ensure_reset_wait"] == ["work"]


def test_reset_signal_confirms_then_dispatches() -> None:
    observed = {"a": _obs(halted=True)}
    state, actions = plan(observed, {"a"}, QueueState(), NOW, _cfg(), reset_signals={ACCT})
    assert "confirm_reset" in _kinds(actions)
    assert state.reset_confirmed_at[ACCT] == NOW
    # confirmation + a free repo + open stagger gate → one resume dispatched same tick
    assert _launch_ids(actions) == ["a"]
    assert state.entries["a"].state == "launching"


# --------------------------------------------------------------------------- #
# stagger + per-repo serial
# --------------------------------------------------------------------------- #
def test_global_stagger_one_launch_per_tick() -> None:
    observed = {
        "a": _obs(halted=True, cwd="/r1", repo="/r1"),
        "b": _obs(halted=True, cwd="/r2", repo="/r2"),
    }
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, last_launch_at=0)
    state, actions = plan(observed, {"a", "b"}, base, NOW, _cfg(), reset_signals=set())
    assert len(_launch_ids(actions)) == 1  # only one resume per tick, regardless of repo count
    launched = _launch_ids(actions)[0]
    other = "b" if launched == "a" else "a"
    assert state.entries[launched].state == "launching"
    assert state.entries[other].state == "queued"


def test_stagger_gate_closed_blocks_launch() -> None:
    observed = {"a": _obs(halted=True)}
    base = QueueState(
        reset_confirmed_at={ACCT: NOW - 1}, last_launch_at=NOW - 1000
    )  # 1s ago < 120s
    state, actions = plan(
        observed, {"a"}, base, NOW, _cfg(resume_stagger_sec=120), reset_signals=set()
    )
    assert "launch_resume" not in _kinds(actions)
    assert state.entries["a"].state == "queued"


def test_per_repo_serial_holds_sibling_launches_other_repo() -> None:
    # /r1 already has a running resume; its queued sibling waits, /r2's head goes.
    entries = {"A": Entry("A", repo="/r1", cwd="/r1", state="running", baseline_offset=100)}
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, last_launch_at=0, entries=entries)
    observed = {
        "A": _obs(
            alive=True, raw="busy", size=100, cwd="/r1", repo="/r1"
        ),  # not grown → stays running
        "B": _obs(halted=True, cwd="/r1", repo="/r1"),
        "C": _obs(halted=True, cwd="/r2", repo="/r2"),
    }
    state, actions = plan(observed, {"B", "C"}, base, NOW, _cfg(), reset_signals=set())
    assert _launch_ids(actions) == ["C"]  # /r1 is busy; only /r2 dispatches
    assert state.entries["A"].state == "running"
    assert state.entries["B"].state == "queued"


def test_alive_halted_head_is_reaped_before_relaunch() -> None:
    observed = {"a": _obs(alive=True, raw="busy", halted=True)}  # stuck live HALTED REPL
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, last_launch_at=0)
    _state, actions = plan(observed, {"a"}, base, NOW, _cfg(), reset_signals=set())
    kinds = _kinds(actions)
    assert "reap" in kinds and "launch_resume" in kinds
    assert kinds.index("reap") < kinds.index("launch_resume")  # kill the REPL first
    assert all(a.session_id == "a" for a in actions if a.kind in ("reap", "launch_resume"))


# --------------------------------------------------------------------------- #
# finish signal (transcript progress + idle), done pruning
# --------------------------------------------------------------------------- #
def test_finish_on_progress_then_idle_frees_repo() -> None:
    entries = {"A": Entry("A", repo="/r1", cwd="/r1", state="running", baseline_offset=100)}
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, entries=entries)
    observed = {"A": _obs(alive=True, raw="idle", size=200)}  # grew past baseline, now idle
    state, actions = plan(observed, set(), base, NOW, _cfg(), reset_signals=set())
    assert "A" not in state.entries  # done entries are pruned
    assert any(a.kind == "notify" and "finished" in a.detail for a in actions)
    assert _is_drained(state)


def test_parked_after_progress_counts_as_finished() -> None:
    entries = {"A": Entry("A", repo="/r1", cwd="/r1", state="running", baseline_offset=100)}
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, entries=entries)
    observed = {"A": _obs(alive=False, size=200)}  # progressed then the process exited
    state, _actions = plan(observed, set(), base, NOW, _cfg(), reset_signals=set())
    assert "A" not in state.entries  # done (had progress) → freed


# --------------------------------------------------------------------------- #
# re-halt, timeouts, the bounded requeue/fail ladder
# --------------------------------------------------------------------------- #
def test_rehalt_requeues_and_clears_reset() -> None:
    entries = {"A": Entry("A", repo="/r1", cwd="/r1", state="running", baseline_offset=100)}
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, entries=entries)
    observed = {"A": _obs(alive=True, raw="busy", halted=True, size=150)}  # 429'd again
    state, actions = plan(observed, {"A"}, base, NOW, _cfg(), reset_signals=set())
    assert state.entries["A"].state == "queued"
    assert state.entries["A"].attempts == 1
    assert not state.reset_confirmed_at  # that account's limit is back → re-gate it
    assert "ensure_reset_wait" in _kinds(actions)
    assert "launch_resume" not in _kinds(actions)


def test_rehalt_at_attempt_cap_fails() -> None:
    entries = {
        "A": Entry("A", repo="/r1", cwd="/r1", state="running", baseline_offset=100, attempts=2)
    }
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, entries=entries)
    observed = {"A": _obs(alive=True, raw="busy", halted=True, size=150)}
    state, actions = plan(
        observed, {"A"}, base, NOW, _cfg(resume_max_attempts=3), reset_signals=set()
    )
    assert state.entries["A"].state == "failed"
    assert any(a.kind == "notify" and "failed" in a.detail for a in actions)


def test_launch_timeout_when_dead_requeues() -> None:
    entries = {
        "A": Entry("A", repo="/r", cwd="/r", state="launching", baseline_offset=100, launched_at=0)
    }
    # Recent last_launch_at closes the stagger gate, so the requeue isn't re-dispatched
    # this same tick — isolating the timeout→requeue transition.
    base = QueueState(
        reset_confirmed_at={ACCT: NOW - 1}, last_launch_at=NOW - 1000, entries=entries
    )
    observed = {"A": _obs(alive=False, size=100)}  # never came up, no progress
    cfg = _cfg(resume_launch_timeout_sec=900)  # NOW - 0 >> 900s
    state, actions = plan(observed, set(), base, NOW, cfg, reset_signals=set())
    assert state.entries["A"].state == "queued"
    assert state.entries["A"].attempts == 1
    assert "launch_resume" not in _kinds(actions)


def test_live_but_slow_turn_is_not_failed() -> None:
    # Guards the bug: a launched resume that is ALIVE but hasn't produced output yet
    # (long tool call / slow probe) must never be reaped/failed by the launch timeout.
    entries = {
        "A": Entry("A", repo="/r", cwd="/r", state="launching", baseline_offset=100, launched_at=0)
    }
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, entries=entries)
    observed = {"A": _obs(alive=True, raw="busy", size=100)}  # alive, no growth yet
    cfg = _cfg(resume_launch_timeout_sec=900)
    state, actions = plan(observed, set(), base, NOW, cfg, reset_signals=set())
    assert state.entries["A"].state == "launching"  # left alone
    assert not any(a.kind == "notify" and "failed" in a.detail for a in actions)


# --------------------------------------------------------------------------- #
# manual-resume adoption (O9), done re-queue, drained helper
# --------------------------------------------------------------------------- #
def test_manual_resume_is_adopted_not_relaunched() -> None:
    entries = {"A": Entry("A", repo="/r1", cwd="/r1", state="queued")}
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, entries=entries)
    observed = {"A": _obs(alive=True, raw="busy", size=50)}  # user resumed it out-of-band
    state, actions = plan(observed, set(), base, NOW, _cfg(), reset_signals=set())
    assert state.entries["A"].state == "running"
    assert "launch_resume" not in _kinds(actions)  # no second claude --resume on a live id


def test_previously_done_session_requeues_when_halted_again() -> None:
    entries = {"A": Entry("A", repo="/r1", cwd="/r1", state="done")}
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, entries=entries)
    observed = {"A": _obs(halted=True)}  # hit the limit again in a later window
    state, _actions = plan(observed, {"A"}, base, NOW, _cfg(), reset_signals=set())
    assert state.entries["A"].state in ("queued", "launching")  # revived, not stuck done


def test_is_drained() -> None:
    assert _is_drained(QueueState())
    assert _is_drained(QueueState(entries={"x": Entry("x", "/r", "/r", state="failed")}))
    assert not _is_drained(QueueState(entries={"x": Entry("x", "/r", "/r", state="queued")}))
    assert not _is_drained(QueueState(entries={"x": Entry("x", "/r", "/r", state="running")}))


# --------------------------------------------------------------------------- #
# state persistence round-trip + candidate finder
# --------------------------------------------------------------------------- #
def test_state_roundtrip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    state = QueueState(
        reset_confirmed_at={"private": NOW, "work": NOW - 3},
        last_launch_at=NOW - 5,
        reset_wait_pid={"work": 4242},
        entries={
            "a": Entry(
                "a", "/r1", "/r1", state="running", attempts=1, baseline_offset=9, account="work"
            )
        },
    )
    save_state(state)
    loaded = load_state()
    assert loaded.reset_confirmed_at == {"private": NOW, "work": NOW - 3}
    assert loaded.reset_wait_pid == {"work": 4242}
    assert loaded.entries["a"].state == "running"
    assert loaded.entries["a"].baseline_offset == 9
    assert loaded.entries["a"].account == "work"


def test_state_load_migrates_legacy_scalar_gate(tmp_path: Path, monkeypatch) -> None:
    """A pre-multi-account queue file (scalar gate) upgrades in place instead of crashing."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    legacy = {
        "reset_confirmed_at": NOW,  # was a bare int: "the one gate"
        "last_launch_at": NOW - 5,
        "reset_wait_pid": 4242,
        "entries": {"a": {"session_id": "a", "repo": "/r1", "cwd": "/r1", "state": "queued"}},
    }
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(legacy), encoding="utf-8")
    loaded = load_state()
    assert loaded.reset_confirmed_at == {ACCT: NOW}  # adopted as the default account's
    assert loaded.reset_wait_pid == {ACCT: 4242}
    assert loaded.entries["a"].account == ACCT  # unstamped entry → default account


def test_repo_of_falls_back_to_cwd_for_non_repo(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-git-repo"
    plain.mkdir()
    assert repo_of(str(plain)) == str(plain)


class _StubAdapter:
    """Minimal adapter: only what candidates() touches."""

    def __init__(self, halted_ids: set[str], cwd: str) -> None:
        self._halted = halted_ids
        self._cwd = cwd

    def discover(self) -> list[LiveSession]:
        return []

    def is_halted(self, cwd: str, session_id: str) -> bool:
        return session_id in self._halted

    def transcript_path(
        self, cwd: str, session_id: str, config_dir: str | None = None
    ) -> Path | None:
        return Path(self._cwd) / f"{session_id}.jsonl"  # treated as present


def test_candidates_filters(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    cwd = str(tmp_path)
    for sid in ("halted", "done", "draft", "fine"):
        store.ensure(sid)
        store.update_fields(sid, cwd=cwd)
    store.update_fields("done", done=True)
    store.update_fields("draft", draft=True)
    adapter = _StubAdapter(halted_ids={"halted", "done", "draft"}, cwd=cwd)
    found = {c.session_id for c in candidates(store, adapter)}  # type: ignore[arg-type]
    assert found == {"halted"}  # done/draft excluded; "fine" isn't halted


# --------------------------------------------------------------------------- #
# the ||▶ icon gate — it must promise exactly what the watcher would do
# --------------------------------------------------------------------------- #
def test_will_auto_resume_follows_the_config_gate(tmp_path: Path) -> None:
    """The green ▶ appears only when resume_halted is ON — the shipped default is OFF."""
    store = Store(tmp_path / "s.db")
    store.ensure("h")
    store.update_fields("h", cwd=str(tmp_path))
    session = store.get("h")
    assert session is not None
    adapter = _StubAdapter(halted_ids={"h"}, cwd=str(tmp_path))

    off = _cfg(resume_halted=False)  # ccc's shipped default (INERT_DEFAULT_KEYS)
    on = _cfg(resume_halted=True)
    assert not will_auto_resume(session, adapter, off)  # type: ignore[arg-type]
    assert will_auto_resume(session, adapter, on)  # type: ignore[arg-type]


def test_will_auto_resume_refuses_done_and_draft(tmp_path: Path) -> None:
    """Icon eligibility tracks candidates(): a done/draft session is never revived."""
    store = Store(tmp_path / "s.db")
    for sid in ("done", "draft"):
        store.ensure(sid)
        store.update_fields(sid, cwd=str(tmp_path))
    store.update_fields("done", done=True)
    store.update_fields("draft", draft=True)
    adapter = _StubAdapter(halted_ids={"done", "draft"}, cwd=str(tmp_path))
    on = _cfg(resume_halted=True)
    for sid in ("done", "draft"):
        session = store.get(sid)
        assert session is not None
        assert not will_auto_resume(session, adapter, on)  # type: ignore[arg-type]
