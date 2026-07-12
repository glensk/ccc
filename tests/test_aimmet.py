"""Tests for the impartial AIM-met checker (no real LLM — assess/check_met monkeypatched)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from command_center import aimmet, llm
from command_center.config import Config
from command_center.models import Session
from command_center.store import Store


def _verdict(met: bool) -> dict:
    return {"met": met, "reason": "because"}


def test_parse_verdict() -> None:
    yes = aimmet.parse_verdict('{"met":true,"reason":"tests pass"}')
    assert yes is not None and yes["met"] is True and yes["reason"] == "tests pass"
    no = aimmet.parse_verdict('{"met":false,"reason":"still failing"}')
    assert no is not None and no["met"] is False
    string_true = aimmet.parse_verdict('{"met":"yes"}')  # string booleans tolerated
    assert string_true is not None and string_true["met"] is True
    assert aimmet.parse_verdict("not json at all") is None
    assert aimmet.parse_verdict('{"reason":"no met key"}') is None  # missing the verdict key


def test_build_facts_defaults() -> None:
    facts = aimmet.build_facts(None, None, "")
    assert facts["original_aim"] == "(unknown)"
    assert facts["current_aim"] == "(unknown)"
    assert facts["evidence"] == "(no transcript evidence)"


def test_check_met_false_does_not_escalate(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake(_facts: object, _model: object, **_k: object) -> dict:
        calls.append(1)
        return _verdict(False)

    monkeypatch.setattr(aimmet, "assess", fake)
    verdict = aimmet.check_met({}, "model")
    assert verdict is not None and verdict["met"] is False
    assert len(calls) == 1  # a false first pass short-circuits — no extra calls


def test_check_met_escalates_and_confirms(monkeypatch: pytest.MonkeyPatch) -> None:
    seq = iter([_verdict(True), _verdict(True), _verdict(False)])
    monkeypatch.setattr(aimmet, "assess", lambda f, m, **_k: next(seq))
    verdict = aimmet.check_met({}, "model")
    assert verdict is not None
    assert verdict["met"] is True  # 2 of 3 confirm met
    assert verdict["votes"] == "2/3"


def test_check_met_escalation_can_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    seq = iter([_verdict(True), _verdict(False), _verdict(False)])
    monkeypatch.setattr(aimmet, "assess", lambda f, m, **_k: next(seq))
    verdict = aimmet.check_met({}, "model")
    assert verdict is not None
    assert verdict["met"] is False  # a lone false-positive cannot flip DONE
    assert verdict["votes"] == "1/3"


def test_check_met_none_when_no_pass_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aimmet, "assess", lambda f, m, **_k: None)
    assert aimmet.check_met({}, "model") is None


def test_check_met_forwards_note_to_every_assess(monkeypatch: pytest.MonkeyPatch) -> None:
    """The session's first AIM (note) reaches every escalation pass — log context only."""
    seen_notes: list[object] = []
    seq = iter([_verdict(True), _verdict(True), _verdict(True)])

    def fake(_facts: object, _model: object, *, note: str = "") -> dict:
        seen_notes.append(note)
        return next(seq)

    monkeypatch.setattr(aimmet, "assess", fake)
    aimmet.check_met({}, "model", note="ship the parser")
    assert seen_notes == ["ship the parser", "ship the parser", "ship the parser"]


def test_assess_passes_aim_met_purpose(monkeypatch: pytest.MonkeyPatch) -> None:
    """``assess`` labels its ai.py call ``aim-met`` and forwards the note (log metadata)."""
    seen: dict[str, object] = {}

    def _capture(_prompt: str, _model: str, *, purpose: str = "", note: str = "") -> str:
        seen["purpose"] = purpose
        seen["note"] = note
        return '{"met":false,"reason":"not yet"}'

    monkeypatch.setattr(llm, "run_model", _capture)
    verdict = aimmet.assess(aimmet.build_facts("orig", "cur", "ev"), "m", note="orig")
    assert verdict is not None and verdict["met"] is False
    assert seen["purpose"] == "aim-met"
    assert seen["note"] == "orig"


def _write_transcript(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def test_build_evidence_includes_tool_results(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    _write_transcript(
        path,
        [
            {"type": "user", "message": {"role": "user", "content": "please run the tests"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "running them now"},
                        {"type": "tool_use", "name": "Bash"},
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "content": [{"type": "text", "text": "5 passed"}]}
                    ],
                },
            },
        ],
    )
    evidence = aimmet.build_evidence(path)
    assert "please run the tests" in evidence
    assert "running them now" in evidence
    assert "[tool:Bash]" in evidence
    assert "5 passed" in evidence  # the tool RESULT is kept (unlike _read_transcript_tail)


def test_build_evidence_truncates_tool_results(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    _write_transcript(
        path,
        [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "x" * 500}],
                },
            }
        ],
    )
    evidence = aimmet.build_evidence(path, tool_result_cap=200)
    assert "x" * 200 in evidence
    assert "x" * 201 not in evidence  # capped


def test_build_evidence_missing_file(tmp_path: Path) -> None:
    assert aimmet.build_evidence(tmp_path / "nope.jsonl") == ""


def test_eligible() -> None:
    cfg = Config()  # aim_score_threshold default 50
    assert aimmet.eligible(Session(session_id="s", aim="ship it", aim_score=80), cfg) is True
    assert aimmet.eligible(Session(session_id="s", aim=None, aim_score=80), cfg) is False
    assert aimmet.eligible(Session(session_id="s", aim="x", aim_score=20), cfg) is False  # vague
    assert aimmet.eligible(Session(session_id="s", aim="x", aim_score=-1), cfg) is False  # unscored
    assert aimmet.eligible(Session(session_id="s", aim="x", aim_score=80, draft=True), cfg) is False
    assert aimmet.eligible(Session(session_id="s", aim="x", aim_score=80, done=True), cfg) is False
    archived = Session(session_id="s", aim="x", aim_score=80, archived=True)
    assert aimmet.eligible(archived, cfg) is False


class _StubAdapter:
    def __init__(self, path: Path) -> None:
        self._path = path

    def transcript_path(self, _cwd: str, _sid: str) -> Path:
        return self._path


def _ready_session(tmp_path: Path) -> tuple[Store, Path]:
    store = Store(tmp_path / "state.db")
    store.ensure("s1", cwd="/repo")
    store.set_aim("s1", "make the build green")
    store.update_fields("s1", aim_score=80, last_response_at=1000)
    tpath = tmp_path / "t.jsonl"
    _write_transcript(
        tpath,
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done, tests pass"}],
                },
            }
        ],
    )
    return store, tpath


def test_run_for_session_writes_verdict_then_new_turn_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, tpath = _ready_session(tmp_path)
    monkeypatch.setattr(aimmet, "check_met", lambda f, m, **_k: {"met": True, "reason": "ok"})
    session = store.get("s1")
    assert session is not None
    verdict = aimmet.run_for_session(store, _StubAdapter(tpath), session, Config())
    assert verdict is not None and verdict["met"] is True
    got = store.get("s1")
    assert got is not None and got.aim_met is True and got.aim_assessed_at > 0
    # New-turn gate: with no turn since (last_response_at <= aim_assessed_at) it skips.
    reread = store.get("s1")
    assert reread is not None
    assert aimmet.run_for_session(store, _StubAdapter(tpath), reread, Config()) is None


def test_run_for_session_discards_on_aim_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, tpath = _ready_session(tmp_path)
    session = store.get("s1")
    assert session is not None

    def racey(_facts: object, _model: object, **_k: object) -> dict:
        store.set_aim("s1", "a totally different goal")  # AIM changes while the LLM "runs"
        return {"met": True, "reason": "ok"}

    monkeypatch.setattr(aimmet, "check_met", racey)
    verdict = aimmet.run_for_session(store, _StubAdapter(tpath), session, Config())
    assert verdict is None  # stale-write guard discarded it
    got = store.get("s1")
    assert got is not None and got.aim_met is False  # nothing written back
