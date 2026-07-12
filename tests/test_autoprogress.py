"""Tests for the auto-progress module's deterministic parts.

The cheap-model call (``llm.run_model``) is monkeypatched everywhere — no real
``claude -p`` ever runs. We exercise: JSON parsing, the transcript byte-offset
delta reader, sub-goal derivation, conservative auto-checking, and the guards
that protect user-authored checklists / never uncheck.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from command_center import autoprogress, llm
from command_center.adapters.claude import ClaudeAdapter
from command_center.store import Store


def _store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Store:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    return Store(tmp_path / "state.db")


def _jsonl(records: list[dict[str, object]]) -> str:
    return "".join(json.dumps(r) + "\n" for r in records)


def _turn(role: str, text: str) -> dict[str, object]:
    return {"type": role, "message": {"role": role, "content": text}}


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def test_parse_subgoals_strips_and_caps() -> None:
    raw = 'noise {"subgoals":["1. find valve","- throttle","test","a","b","c","d (extra)"]} tail'
    items = autoprogress.parse_subgoals(raw)
    assert items[:3] == ["find valve", "throttle", "test"]
    assert len(items) == 6  # capped at 6


def test_parse_subgoals_garbage() -> None:
    assert autoprogress.parse_subgoals(None) == []
    assert autoprogress.parse_subgoals("not json") == []
    assert autoprogress.parse_subgoals('{"subgoals": "nope"}') == []


def test_parse_satisfied_in_range_dedup() -> None:
    raw = '{"satisfied":[0,2,2,5,-1,"x"]}'
    assert autoprogress.parse_satisfied(raw, count=3) == [0, 2]  # 5,-1,"x" dropped, dedup


def test_lint_subgoal() -> None:
    assert autoprogress.lint_subgoal("improve the parser") is not None  # vague lead verb
    assert autoprogress.lint_subgoal("refactor things") is not None
    assert autoprogress.lint_subgoal("test") is not None  # too short
    assert autoprogress.lint_subgoal("test_parser passes") is None  # concrete, checkable
    assert autoprogress.lint_subgoal("emit AST node") is None


def test_parse_subgoal_items_weights() -> None:
    # Plain strings default to weight 1.
    assert autoprogress.parse_subgoal_items('{"subgoals":["emit AST","add tests"]}') == [
        ("emit AST", 1),
        ("add tests", 1),
    ]
    # Object form: essential -> weight 2, otherwise 1.
    items = autoprogress.parse_subgoal_items(
        '{"subgoals":[{"text":"test passes","importance":"essential"},'
        '{"text":"docs written","importance":"optional"}]}'
    )
    assert items == [("test passes", 2), ("docs written", 1)]


def test_verify_items_drops_vague_but_not_all() -> None:
    items = [("improve stuff", 1), ("test_x passes", 1)]
    assert autoprogress._verify_items(items) == [("test_x passes", 1)]
    # If everything is vague, keep the originals (a vague checklist beats none).
    all_vague = [("improve x", 1), ("refactor y", 2)]
    assert autoprogress._verify_items(all_vague) == all_vague


# --------------------------------------------------------------------------- #
# transcript byte-offset delta reader
# --------------------------------------------------------------------------- #
def test_read_transcript_delta_advances(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(_jsonl([_turn("user", "first"), _turn("assistant", "ack one")]), "utf-8")
    delta1, off1 = llm.read_transcript_delta(path, 0)
    assert "first" in delta1 and "ack one" in delta1
    assert off1 == path.stat().st_size

    # No new bytes -> empty delta, offset unchanged.
    delta2, off2 = llm.read_transcript_delta(path, off1)
    assert delta2 == "" and off2 == off1

    # Append a new turn -> only the new turn comes back.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_turn("user", "second new line")) + "\n")
    delta3, off3 = llm.read_transcript_delta(path, off1)
    assert "second new line" in delta3
    assert "first" not in delta3  # old content not re-read
    assert off3 == path.stat().st_size


def test_read_transcript_delta_skips_tool_blocks(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    rec = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "done the thing"},
                {"type": "tool_use", "name": "Bash"},
                {"type": "thinking", "thinking": "secret"},
            ],
        },
    }
    path.write_text(json.dumps(rec) + "\n", "utf-8")
    delta, _ = llm.read_transcript_delta(path, 0)
    assert "done the thing" in delta
    assert "secret" not in delta  # thinking dropped


def test_read_transcript_delta_truncation_resets(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(_jsonl([_turn("user", "hello world")]), "utf-8")
    # Offset larger than the file (rotation/truncation) -> read from start.
    delta, _ = llm.read_transcript_delta(path, 10_000_000)
    assert "hello world" in delta


# --------------------------------------------------------------------------- #
# store: source roundtrip
# --------------------------------------------------------------------------- #
def test_subgoal_source_roundtrip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = _store(monkeypatch, tmp_path)
    store.ensure("s1")
    store.set_subgoals("s1", ["a", "b"], source="auto")
    subs = store.list_subgoals("s1")
    assert all(sg.source == "auto" for sg in subs)
    store.set_subgoals("s1", ["manual"])  # default source = user
    assert store.list_subgoals("s1")[0].source == "user"


def test_context_offset_column(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = _store(monkeypatch, tmp_path)
    store.ensure("s1")
    store.update_fields("s1", context_offset=4242)
    got = store.get("s1")
    assert got is not None and got.context_offset == 4242


# --------------------------------------------------------------------------- #
# derivation
# --------------------------------------------------------------------------- #
def test_derive_when_no_checklist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = _store(monkeypatch, tmp_path)
    store.ensure("s1")
    store.update_fields("s1", aim="ship the parser")
    path = tmp_path / "t.jsonl"
    path.write_text(_jsonl([_turn("user", "let's start")]), "utf-8")

    monkeypatch.setattr(
        llm, "run_model", lambda *_a, **_k: '{"subgoals":["parse tokens","emit AST","add tests"]}'
    )
    res = autoprogress.run_for_session(store, "s1", path, model="m")
    assert res.derived == ["parse tokens", "emit AST", "add tests"]
    subs = store.list_subgoals("s1")
    assert [s.text for s in subs] == ["parse tokens", "emit AST", "add tests"]
    assert all(s.source == "auto" for s in subs)
    # Offset baselined so the next pass doesn't grade pre-derivation history.
    got = store.get("s1")
    assert got is not None and got.context_offset == path.stat().st_size


def test_derive_grades_existing_transcript_immediately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A checklist derived from a retrospective AIM ticks already-done work in the SAME pass.

    Regression: the bar used to stick at 0/N because the derive pass baselined the offset
    past the completed work and only a needs_summary-gated daemon re-grade (which never
    fires again for an idle/finished session) could catch it. Now derive runs an inline
    full-regrade over the whole transcript.
    """
    store = _store(monkeypatch, tmp_path)
    store.ensure("s1")
    store.update_fields("s1", aim="add a conftest and make the suite pass")
    path = tmp_path / "t.jsonl"
    # The satisfying work is ALREADY in the transcript when the checklist is derived.
    path.write_text(
        _jsonl([_turn("assistant", "added tests/conftest.py; 279 tests pass")]), "utf-8"
    )

    def _fake(prompt: str, *_a: object, **_k: object) -> str:
        # Derive call -> the checklist; grading call -> both items satisfied.
        if "grading the progress" in prompt:
            return '{"satisfied":[0,1]}'
        return '{"subgoals":["conftest.py exists","pytest suite passes"]}'

    monkeypatch.setattr(llm, "run_model", _fake)
    res = autoprogress.run_for_session(store, "s1", path, model="m")
    assert res.derived == ["conftest.py exists", "pytest suite passes"]
    assert set(res.checked) == {"conftest.py exists", "pytest suite passes"}
    subs = store.list_subgoals("s1")
    assert all(s.checked for s in subs)  # bar reaches 100% in the derive pass, not 0%
    # Offset still baselined to the end so later turns grade only new content.
    got = store.get("s1")
    assert got is not None and got.context_offset == path.stat().st_size


def test_derive_sets_weights(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = _store(monkeypatch, tmp_path)
    store.ensure("s1")
    store.update_fields("s1", aim="ship the parser")
    path = tmp_path / "t.jsonl"
    path.write_text(_jsonl([_turn("user", "go")]), "utf-8")
    monkeypatch.setattr(
        llm,
        "run_model",
        lambda *_a, **_k: (
            '{"subgoals":[{"text":"test_x passes","importance":"essential"},'
            '{"text":"emit AST node","importance":"optional"}]}'
        ),
    )
    autoprogress.run_for_session(store, "s1", path, model="m")
    subs = store.list_subgoals("s1")
    assert [(s.text, s.weight) for s in subs] == [("test_x passes", 2), ("emit AST node", 1)]


def test_derive_and_grade_pass_purpose_labels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The derive + grade ai.py calls carry ``subgoal-derive`` / ``subgoal-grade`` + the note.

    The note is the session's first/original AIM (log context only) — ``set_aim`` seeds
    ``aim_history`` so ``concise_note`` resolves it here.
    """
    store = _store(monkeypatch, tmp_path)
    store.ensure("s1")
    store.set_aim("s1", "ship the parser")  # seeds aim_history row 1
    path = tmp_path / "t.jsonl"
    path.write_text(_jsonl([_turn("assistant", "done, all tests pass")]), "utf-8")

    calls: list[tuple[str, str]] = []

    def _capture(prompt: str, _model: str, *, purpose: str = "", note: str = "") -> str:
        calls.append((purpose, note))
        if "grading the progress" in prompt:  # the assess prompt
            return '{"satisfied":[0]}'
        return '{"subgoals":["parse tokens"]}'  # the derive prompt

    monkeypatch.setattr(llm, "run_model", _capture)
    autoprogress.run_for_session(store, "s1", path, model="m")
    purposes = [p for p, _ in calls]
    assert "subgoal-derive" in purposes  # first pass derives the checklist
    assert "subgoal-grade" in purposes  # inline full-regrade grades it
    assert all(note == "ship the parser" for _, note in calls)  # first AIM reaches both


def test_last_progress_at_only_on_real_grade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _store(monkeypatch, tmp_path)
    store.ensure("s1")
    store.update_fields("s1", aim="x")
    path = tmp_path / "t.jsonl"
    path.write_text("", "utf-8")  # empty: the derive's inline regrade has nothing to grade

    # Derive pass establishes the checklist; with no transcript evidence yet its inline
    # full-regrade is a no-op, so it must NOT stamp the debounce clock.
    monkeypatch.setattr(
        llm, "run_model", lambda *_a, **_k: '{"subgoals":["test_x passes","emit AST node"]}'
    )
    autoprogress.run_for_session(store, "s1", path, model="m")
    after_derive = store.get("s1")
    assert after_derive is not None and after_derive.last_progress_at == 0

    # A real grading pass (new content) stamps last_progress_at.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_turn("assistant", "finished it")) + "\n")
    monkeypatch.setattr(llm, "run_model", lambda *_a, **_k: '{"satisfied":[0]}')
    autoprogress.run_for_session(store, "s1", path, model="m")
    after_grade = store.get("s1")
    assert after_grade is not None and after_grade.last_progress_at > 0


def test_derive_dry_run_writes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = _store(monkeypatch, tmp_path)
    store.ensure("s1")
    store.update_fields("s1", aim="do the thing")

    def _boom(*_a: object, **_k: object) -> str:
        raise AssertionError("LLM must not be called in dry-run derive")

    monkeypatch.setattr(llm, "run_model", _boom)
    res = autoprogress.run_for_session(store, "s1", None, model="m", dry_run=True)
    assert res.derived == []
    assert store.list_subgoals("s1") == []


# --------------------------------------------------------------------------- #
# assessment / guards
# --------------------------------------------------------------------------- #
def test_assess_checks_only_satisfied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = _store(monkeypatch, tmp_path)
    store.ensure("s1")
    store.update_fields("s1", aim="x")
    store.set_subgoals("s1", ["g0", "g1", "g2"], source="auto")
    path = tmp_path / "t.jsonl"
    path.write_text(_jsonl([_turn("assistant", "finished g0 and g2")]), "utf-8")

    monkeypatch.setattr(llm, "run_model", lambda *_a, **_k: '{"satisfied":[0,2]}')
    res = autoprogress.run_for_session(store, "s1", path, model="m")
    assert sorted(res.checked) == ["g0", "g2"]
    assert store.progress("s1") == (2, 3)


def test_assess_never_unchecks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = _store(monkeypatch, tmp_path)
    store.ensure("s1")
    store.update_fields("s1", aim="x")
    store.set_subgoals("s1", ["g0", "g1"], source="auto")
    subs = store.list_subgoals("s1")
    store.set_subgoal_checked(subs[0].id, True)  # g0 already done
    store.set_subgoal_checked(subs[1].id, True)  # g1 already done
    path = tmp_path / "t.jsonl"
    path.write_text(_jsonl([_turn("assistant", "redoing g0")]), "utf-8")

    # Model says only g0 satisfied -> g1 must stay checked (never uncheck).
    monkeypatch.setattr(llm, "run_model", lambda *_a, **_k: '{"satisfied":[0]}')
    res = autoprogress.run_for_session(store, "s1", path, model="m")
    assert res.checked == []  # nothing newly checked; all-checked short-circuit
    assert store.progress("s1") == (2, 2)


def test_user_checklist_left_untouched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = _store(monkeypatch, tmp_path)
    store.ensure("s1")
    store.update_fields("s1", aim="x")
    store.set_subgoals("s1", ["manual goal"])  # source=user
    path = tmp_path / "t.jsonl"
    path.write_text(_jsonl([_turn("assistant", "did manual goal")]), "utf-8")

    def _boom(*_a: object, **_k: object) -> str:
        raise AssertionError("must not grade a user-authored checklist")

    monkeypatch.setattr(llm, "run_model", _boom)
    res = autoprogress.run_for_session(store, "s1", path, model="m")
    assert "user-authored" in res.note
    assert store.progress("s1") == (0, 1)


def test_no_aim_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = _store(monkeypatch, tmp_path)
    store.ensure("s1")  # no aim
    res = autoprogress.run_for_session(store, "s1", None, model="m")
    assert not res.changed()
    assert store.list_subgoals("s1") == []


def test_no_new_content_skips_grading(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = _store(monkeypatch, tmp_path)
    store.ensure("s1")
    store.update_fields("s1", aim="x")
    store.set_subgoals("s1", ["g0"], source="auto")
    path = tmp_path / "t.jsonl"
    path.write_text(_jsonl([_turn("user", "hi")]), "utf-8")
    store.update_fields("s1", context_offset=path.stat().st_size)  # offset at EOF

    def _boom(*_a: object, **_k: object) -> str:
        raise AssertionError("no new content -> no LLM call")

    monkeypatch.setattr(llm, "run_model", _boom)
    res = autoprogress.run_for_session(store, "s1", path, model="m")
    assert res.note == "no new transcript content"


# --------------------------------------------------------------------------- #
# ceremony filter (A) — never invent PR/commit/deploy steps the AIM didn't ask for
# --------------------------------------------------------------------------- #
def test_drop_ceremony_strips_process_keeps_substance() -> None:
    items = [
        ("merge logic selects latest reset", 1),  # substance: "merge logic", NOT a PR merge
        ("all unit tests pass", 1),
        ("pull request merged to main", 1),  # ceremony
        ("code changes committed to branch", 1),  # ceremony
        ("deploy to production", 1),  # ceremony
    ]
    kept = autoprogress._drop_ceremony(items, aim="fix the usage indicator")
    assert [t for t, _ in kept] == ["merge logic selects latest reset", "all unit tests pass"]


def test_drop_ceremony_kept_when_aim_asks_for_it() -> None:
    items = [("write the fix", 1), ("open a PR", 1)]
    # The AIM explicitly wants a PR, so the ceremony step is legitimate — keep all.
    assert autoprogress._drop_ceremony(items, aim="fix bug and open a PR") == items


def test_derive_drops_ceremony_steps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = _store(monkeypatch, tmp_path)
    store.ensure("s1")
    store.update_fields("s1", aim="fix the usage indicator flip-flop")
    path = tmp_path / "t.jsonl"
    path.write_text(_jsonl([_turn("user", "go")]), "utf-8")
    monkeypatch.setattr(
        llm,
        "run_model",
        lambda *_a, **_k: (
            '{"subgoals":["add tiebreaker","tests pass","Pull request merged to main"]}'
        ),
    )
    res = autoprogress.run_for_session(store, "s1", path, model="m")
    assert res.derived == ["add tiebreaker", "tests pass"]  # the PR step never gets stored


# --------------------------------------------------------------------------- #
# full re-grade (D) — grade the whole transcript, leave the delta offset alone
# --------------------------------------------------------------------------- #
def test_full_regrade_uses_whole_transcript_and_keeps_offset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _store(monkeypatch, tmp_path)
    store.ensure("s1")
    store.update_fields("s1", aim="x")
    store.set_subgoals("s1", ["g0", "g1"], source="auto")
    path = tmp_path / "t.jsonl"
    path.write_text(_jsonl([_turn("assistant", "did g0 way back"), _turn("user", "ok")]), "utf-8")
    # Offset already at EOF: a normal delta grade would see nothing; full_regrade re-reads all.
    store.update_fields("s1", context_offset=path.stat().st_size)

    seen: dict[str, str] = {}

    def _capture(prompt: str, _model: str, **_k: object) -> str:
        seen["prompt"] = prompt
        return '{"satisfied":[0]}'

    monkeypatch.setattr(llm, "run_model", _capture)
    res = autoprogress.run_for_session(store, "s1", path, model="m", full_regrade=True)
    assert res.checked == ["g0"]  # graded against the whole transcript despite EOF offset
    assert "did g0 way back" in seen["prompt"]
    got = store.get("s1")
    # Offset untouched so the per-turn delta grader still advances normally afterwards.
    assert got is not None and got.context_offset == path.stat().st_size


def test_full_regrade_no_checklist_is_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = _store(monkeypatch, tmp_path)
    store.ensure("s1")
    store.update_fields("s1", aim="x")  # no checklist yet
    path = tmp_path / "t.jsonl"
    path.write_text(_jsonl([_turn("user", "hi")]), "utf-8")

    def _boom(*_a: object, **_k: object) -> str:
        raise AssertionError("full_regrade must never derive a checklist")

    monkeypatch.setattr(llm, "run_model", _boom)
    res = autoprogress.run_for_session(store, "s1", path, model="m", full_regrade=True)
    assert res.note == "no checklist to re-grade"
    assert store.list_subgoals("s1") == []


def test_predicate_ticks_without_llm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = _store(monkeypatch, tmp_path)
    store.ensure("s1")
    store.update_fields("s1", aim="x")
    store.set_subgoals("s1", ["g0", "g1"], source="auto")
    subs = store.list_subgoals("s1")
    store.set_subgoal_check(subs[0].id, "exit 0")  # passes -> ticks
    store.set_subgoal_check(subs[1].id, "exit 1")  # fails -> stays unchecked
    path = tmp_path / "t.jsonl"
    path.write_text(_jsonl([_turn("user", "hi")]), "utf-8")

    def _boom(*_a: object, **_k: object) -> str:
        raise AssertionError("no LLM call when all pending sub-goals are predicate-gated")

    monkeypatch.setattr(llm, "run_model", _boom)
    res = autoprogress.run_for_session(store, "s1", path, model="m")
    assert res.checked == ["g0"]
    assert store.progress("s1") == (1, 2)


def test_llm_never_overrides_predicate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = _store(monkeypatch, tmp_path)
    store.ensure("s1")
    store.update_fields("s1", aim="x")
    store.set_subgoals("s1", ["g0", "g1"], source="auto")
    subs = store.list_subgoals("s1")
    store.set_subgoal_check(subs[0].id, "exit 1")  # predicate fails -> must stay unchecked
    path = tmp_path / "t.jsonl"
    path.write_text(_jsonl([_turn("assistant", "did g0 and g1")]), "utf-8")
    # LLM claims BOTH satisfied, but g0 is predicate-gated (and failing) -> only g1 ticks.
    monkeypatch.setattr(llm, "run_model", lambda *_a, **_k: '{"satisfied":[0,1]}')
    res = autoprogress.run_for_session(store, "s1", path, model="m")
    assert res.checked == ["g1"]
    assert store.progress("s1") == (1, 2)


def test_predicate_runs_for_user_checklist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = _store(monkeypatch, tmp_path)
    store.ensure("s1")
    store.update_fields("s1", aim="x")
    store.set_subgoals("s1", ["g0"], source="user")  # user-authored
    sub = store.list_subgoals("s1")[0]
    store.set_subgoal_check(sub.id, "exit 0")

    def _boom(*_a: object, **_k: object) -> str:
        raise AssertionError("no LLM grading of a user-authored checklist")

    monkeypatch.setattr(llm, "run_model", _boom)
    res = autoprogress.run_for_session(store, "s1", tmp_path / "none.jsonl", model="m")
    assert res.checked == ["g0"]  # predicate ticks even a user checklist
    assert store.progress("s1") == (1, 1)


def test_run_pass_respects_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = _store(monkeypatch, tmp_path)
    for i in range(5):
        sid = f"s{i}"
        store.ensure(sid)
        store.update_fields(sid, aim="goal", last_response_at=1000 + i)

    from command_center import config

    monkeypatch.setattr(config, "load_config", lambda: config.Config(max_autoprogress_per_run=2))
    monkeypatch.setattr(llm, "run_model", lambda *_a, **_k: '{"subgoals":["a","b"]}')
    results = autoprogress.run_pass(store, ClaudeAdapter(), dry_run=False)
    assert len(results) == 2  # capped
    # Freshest first: s4, s3.
    assert {r.session_id for r in results} == {"s4", "s3"}
