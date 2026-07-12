"""Tests for the impartial drift checker (no real LLM — score_drift is monkeypatched)."""

from __future__ import annotations

import pytest

from command_center import drift, llm


def _verdict(severity: str) -> dict:
    return {
        "severity": severity,
        "drift": severity != "none",
        "reason": f"{severity} reason",
        "dimensions": {},
        "dropped": [],
        "weakened": [],
    }


def test_parse_verdict() -> None:
    clean = drift.parse_verdict('{"severity":"none"}')
    assert clean is not None and clean["drift"] is False
    high = drift.parse_verdict(
        '{"severity":"high","reason":"dropped the tests","dropped":["run tests"]}'
    )
    assert high is not None
    assert high["drift"] is True and high["severity"] == "high"
    assert high["dropped"] == ["run tests"]
    assert drift.parse_verdict("not json at all") is None  # unparseable
    assert drift.parse_verdict('{"severity":"weird"}') is None  # invalid severity


def test_build_facts_renders_anchors_and_checks() -> None:
    facts = drift.build_facts(
        "original aim", "current aim", ["first", "second"], [("x", True), ("y", False)], ["x", "z"]
    )
    assert facts["original_aim"] == "original aim"
    assert "v1. first" in facts["evolution"] and "v2. second" in facts["evolution"]
    assert "[x] x" in facts["old"] and "[ ] y" in facts["old"]
    assert "- x" in facts["new"] and "- z" in facts["new"]


def test_check_drift_clean_does_not_escalate(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake(_facts: object, _model: object, **_k: object) -> dict:
        calls.append(1)
        return _verdict("none")

    monkeypatch.setattr(drift, "score_drift", fake)
    verdict = drift.check_drift({}, "model")
    assert verdict is not None and verdict["drift"] is False
    assert len(calls) == 1  # a clean first pass short-circuits — no extra calls


def test_check_drift_escalates_and_confirms(monkeypatch: pytest.MonkeyPatch) -> None:
    seq = iter([_verdict("high"), _verdict("medium"), _verdict("none")])
    monkeypatch.setattr(drift, "score_drift", lambda f, m, **_k: next(seq))
    verdict = drift.check_drift({}, "model")
    assert verdict is not None
    assert verdict["drift"] is True  # 2 of 3 confirm drift
    assert verdict["severity"] == "high"  # worst surviving severity
    assert verdict["votes"] == "2/3"


def test_check_drift_escalation_can_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    seq = iter([_verdict("low"), _verdict("none"), _verdict("none")])
    monkeypatch.setattr(drift, "score_drift", lambda f, m, **_k: next(seq))
    verdict = drift.check_drift({}, "model")
    assert verdict is not None
    assert verdict["drift"] is False  # majority cleared the initial flag
    assert verdict["votes"] == "1/3"


def test_check_drift_none_when_no_pass_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drift, "score_drift", lambda f, m, **_k: None)
    assert drift.check_drift({}, "model") is None


def test_check_drift_forwards_note_to_every_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """The session's first AIM (note) reaches every escalation pass — log context only."""
    seen_notes: list[object] = []
    seq = iter([_verdict("high"), _verdict("high"), _verdict("high")])

    def fake(_facts: object, _model: object, *, note: str = "") -> dict:
        seen_notes.append(note)
        return next(seq)

    monkeypatch.setattr(drift, "score_drift", fake)
    drift.check_drift({}, "model", note="ship the parser")
    assert seen_notes == ["ship the parser", "ship the parser", "ship the parser"]


def test_score_drift_passes_subgoal_drift_purpose(monkeypatch: pytest.MonkeyPatch) -> None:
    """``score_drift`` labels its ai.py call ``subgoal-drift`` and forwards the note."""
    seen: dict[str, object] = {}

    def _capture(_prompt: str, _model: str, *, purpose: str = "", note: str = "") -> str:
        seen["purpose"] = purpose
        seen["note"] = note
        return '{"severity":"none"}'

    monkeypatch.setattr(llm, "run_model", _capture)
    facts = drift.build_facts("orig", "cur", ["orig", "cur"], [("x", True)], ["x"])
    verdict = drift.score_drift(facts, "m", note="orig")
    assert verdict is not None and verdict["drift"] is False
    assert seen["purpose"] == "subgoal-drift"
    assert seen["note"] == "orig"
