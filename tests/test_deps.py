"""Unit tests for the job-dependency logic (:mod:`command_center.deps`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import deps
from command_center.models import Session
from command_center.store import Store


def _sess(session_id: str, **kw: object) -> Session:
    return Session(session_id=session_id, **kw)  # type: ignore[arg-type]


# ---- dependency_state (the 4-state machine) -------------------------------
def test_dependency_state_missing() -> None:
    assert deps.dependency_state(None) == deps.MISSING


def test_dependency_state_unmet() -> None:
    # A live / parked / future job that is not done and not archived.
    assert deps.dependency_state(_sess("p")) == deps.UNMET


def test_dependency_state_satisfied_real_completion() -> None:
    # done=1 AND not archived — the only satisfied case (mark-done on a real session).
    assert deps.dependency_state(_sess("p", done=True)) == deps.SATISFIED


def test_dependency_state_cancelled_covers_both_paths() -> None:
    # delete-job (archived, not done) and mark-done-on-draft (archived AND done) both
    # read as cancelled — archived is checked BEFORE done.
    assert deps.dependency_state(_sess("p", archived=True)) == deps.CANCELLED
    assert deps.dependency_state(_sess("p", archived=True, done=True)) == deps.CANCELLED


def test_aim_met_never_counts() -> None:
    # An impartial "looks done" verdict is NOT a real completion.
    assert deps.dependency_state(_sess("p", aim_met=True)) == deps.UNMET


def test_is_unsatisfied() -> None:
    assert deps.is_unsatisfied(deps.UNMET)
    assert deps.is_unsatisfied(deps.CANCELLED)
    assert deps.is_unsatisfied(deps.MISSING)
    assert not deps.is_unsatisfied(deps.SATISFIED)
    assert not deps.is_unsatisfied("")


# ---- would_create_cycle ---------------------------------------------------
def _getter(sessions: dict[str, Session]):
    return lambda sid: sessions.get(sid)


def test_cycle_self_dependency() -> None:
    get = _getter({"a": _sess("a")})
    assert deps.would_create_cycle(get, "a", "a") is True


def test_cycle_two_node() -> None:
    # a depends on b, b already depends on a → a→b would close the loop.
    sessions = {"a": _sess("a"), "b": _sess("b", depends_on="a")}
    assert deps.would_create_cycle(_getter(sessions), "a", "b") is True


def test_no_cycle_long_chain() -> None:
    # a→b→c→d, none of them referencing the new node x.
    sessions = {
        "a": _sess("a"),
        "b": _sess("b", depends_on="a"),
        "c": _sess("c", depends_on="b"),
        "d": _sess("d", depends_on="c"),
    }
    assert deps.would_create_cycle(_getter(sessions), "x", "d") is False


def test_no_cycle_dangling_reference() -> None:
    # The chain ends at a missing row — a dangling reference is not a cycle.
    assert deps.would_create_cycle(_getter({}), "a", "does-not-exist") is False


def test_no_cycle_empty_reference() -> None:
    assert deps.would_create_cycle(_getter({}), "a", "") is False


# ---- resolve_dependency_ref -----------------------------------------------
_A = "3a8b7c12-1234-5678-9abc-def012345678"
_B1 = "abcd1111-1234-5678-9abc-def012345678"
_B2 = "abcd2222-1234-5678-9abc-def012345678"


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "s.db")
    store.ensure(_A, cwd="/repo/a")
    return store


def _get(store: Store, sid: str) -> Session:
    got = store.get(sid)
    assert got is not None
    return got


def test_resolve_full_uuid(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert deps.resolve_dependency_ref(store, _A) == _A
    store.close()


def test_resolve_unique_prefix(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # The 4-hex display hash is a unique prefix of the full UUID string.
    assert deps.resolve_dependency_ref(store, "3a8b") == _A
    store.close()


def test_resolve_ambiguous_prefix_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure(_B1, cwd="/repo/b1")
    store.ensure(_B2, cwd="/repo/b2")
    with pytest.raises(deps.DependencyError, match="ambiguous"):
        deps.resolve_dependency_ref(store, "abcd")
    store.close()


def test_resolve_unknown_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(deps.DependencyError, match="no session matches"):
        deps.resolve_dependency_ref(store, "ffff")
    with pytest.raises(deps.DependencyError):
        deps.resolve_dependency_ref(store, "   ")
    store.close()


def test_resolve_finds_archived(tmp_path: Path) -> None:
    # A dependency can point at an archived row (list_sessions include_archived=True).
    store = _store(tmp_path)
    store.update_fields(_A, archived=True)
    assert deps.resolve_dependency_ref(store, "3a8b") == _A
    store.close()


# ---- launch_blocker -------------------------------------------------------
def test_launch_blocker_none_when_no_dependency(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert deps.launch_blocker(store, store.get(_A)) is None  # type: ignore[arg-type]
    store.close()


def test_launch_blocker_none_when_satisfied(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure(_B1, cwd="/repo/b1")
    store.update_fields(_B1, done=True, aim="parent done")
    store.update_fields(_A, depends_on=_B1)
    assert deps.launch_blocker(store, store.get(_A)) is None  # type: ignore[arg-type]
    store.close()


def test_launch_blocker_reports_unmet(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure(_B1, cwd="/repo/b1")
    store.update_fields(_B1, aim="parent still running")
    store.update_fields(_A, depends_on=_B1)
    block = deps.launch_blocker(store, _get(store, _A))
    assert block is not None
    assert block.state == deps.UNMET
    assert block.parent_id == _B1
    assert block.parent_hash == "abcd"
    assert block.parent_aim == "parent still running"
    store.close()


def test_launch_blocker_missing_parent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.update_fields(_A, depends_on="00000000-0000-0000-0000-000000000000")
    block = deps.launch_blocker(store, _get(store, _A))
    assert block is not None
    assert block.state == deps.MISSING
    assert block.parent_aim == ""
    store.close()
