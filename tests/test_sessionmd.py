"""Unit tests for the full-session renderer (:mod:`command_center.sessionmd`)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from command_center import sessionmd
from command_center.models import SessionEvent


@pytest.fixture(autouse=True)
def _clear_render_cache() -> Iterator[None]:
    """Reset the module-level render cache so tests never cross-pollute."""
    sessionmd._RENDER_CACHE.clear()  # pylint: disable=protected-access
    yield
    sessionmd._RENDER_CACHE.clear()  # pylint: disable=protected-access


def _p(text: str) -> SessionEvent:
    return SessionEvent(kind="prompt", text=text)


def _t(text: str) -> SessionEvent:
    return SessionEvent(kind="text", text=text)


def _tool(name: str, tool_input: dict, result: str | None = None) -> SessionEvent:
    return SessionEvent(kind="tool", tool_name=name, tool_input=tool_input, tool_result=result)


# ---------------------------------------------------------------------------
# turn structure
# ---------------------------------------------------------------------------
def test_turn_headers_numbering_and_newest_gold() -> None:
    events = [_p("first ask"), _t("reply one"), _p("second ask"), _t("reply two")]
    body = sessionmd.render_session(events)
    assert "## (1) you\n\nfirst ask" in body
    assert "## claude\n\nreply one" in body
    assert "## (2) you\n\nsecond ask" in body
    # The NEWEST prompt body is tagged "last" (rendered gold in the panel).
    segments = sessionmd.session_segments(events)
    tags = {text: tag for text, tag in segments}
    assert tags["first ask\n\n"] == sessionmd.TAG_TEXT
    assert tags["second ask\n\n"] == sessionmd.TAG_LAST


def test_render_equals_segments_concat() -> None:
    events = [_p("ask"), _t("answer"), _tool("Bash", {"command": "ls"}, "a b")]
    segments = sessionmd.session_segments(events)
    assert sessionmd.render_session(events) == "".join(text for text, _tag in segments)


def test_preamble_events_before_first_prompt() -> None:
    body = sessionmd.render_session([_t("resumed tail"), _p("then an ask")])
    assert body.startswith("## claude\n\nresumed tail")
    assert "## (1) you\n\nthen an ask" in body


def test_empty_events_degrade() -> None:
    assert sessionmd.render_session([]) == "(no conversation in this session yet)"


# ---------------------------------------------------------------------------
# tool lines
# ---------------------------------------------------------------------------
def test_tool_one_liner_and_result_truncation() -> None:
    result = "\n".join(f"line {i}" for i in range(1, 11))
    events = [_p("run it"), _tool("Bash", {"command": "pytest -x tests/"}, result)]
    body = sessionmd.render_session(events)
    assert "⏺ Bash(pytest -x tests/)" in body
    assert "  ⎿ line 1" in body
    assert "    line 3 …" in body  # 3-line cap, trim marker on the last kept line
    assert "line 4" not in body


def test_tool_input_summary_fallbacks() -> None:
    assert "⏺ Edit(/tmp/x.py)" in sessionmd.render_session(
        [_tool("Edit", {"file_path": "/tmp/x.py", "old_string": "aaa"})]
    )
    assert "⏺ ExitPlanMode" in sessionmd.render_session([_tool("ExitPlanMode", {})])
    # No known key → compact JSON.
    body = sessionmd.render_session([_tool("Weird", {"foo": 1})])
    assert '⏺ Weird({"foo": 1})' in body
    # Long inputs are capped to one line with an ellipsis.
    long_cmd = "x" * 500
    body = sessionmd.render_session([_tool("Bash", {"command": long_cmd})])
    line = next(ln for ln in body.splitlines() if ln.startswith("⏺ Bash("))
    assert len(line) < 200 and "…" in line


def test_unpaired_tool_result_omitted() -> None:
    body = sessionmd.render_session([_p("go"), _tool("Bash", {"command": "sleep 1"})])
    assert "⏺ Bash(sleep 1)" in body
    assert "⎿" not in body


# ---------------------------------------------------------------------------
# caps (decision 3 + O5)
# ---------------------------------------------------------------------------
def test_cap_keeps_newest_whole_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessionmd, "SESSION_MAX_BYTES", 600)
    events: list[SessionEvent] = []
    for i in range(1, 6):
        events += [_p(f"ask number {i}"), _t(f"reply {i} " + "x" * 100)]
    body = sessionmd.render_session(events)
    assert body.startswith("_showing last ")
    assert " of 5 turns_" in body
    assert "## (5) you" in body  # newest kept, ORIGINAL numbering preserved
    assert "## (1) you" not in body  # oldest dropped whole
    assert len(body.encode("utf-8")) <= 600


def test_single_oversized_turn_tail_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessionmd, "SESSION_MAX_BYTES", 400)
    events = [_p("big ask"), _t("y" * 5000 + " THE-TAIL")]
    body = sessionmd.render_session(events)
    assert "[turn truncated" in body
    assert body.rstrip().endswith("THE-TAIL")  # trailing (newest) bytes kept
    assert len(body.encode("utf-8")) <= 400


def test_byte_stability() -> None:
    events = [_p("ask"), _tool("Bash", {"command": "ls"}, "out"), _t("done")]
    assert sessionmd.render_session(events) == sessionmd.render_session(events)


# ---------------------------------------------------------------------------
# mtime cache
# ---------------------------------------------------------------------------
def test_segments_for_path_mtime_cache(tmp_path: Path) -> None:
    path = tmp_path / "sid.jsonl"

    def _write(prompt: str) -> None:
        path.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n",
            encoding="utf-8",
        )

    _write("first version")
    first = sessionmd.segments_for_path(path)
    assert "first version" in "".join(t for t, _ in first)
    # Same mtime → cached segments (even though the bytes changed underneath).
    stat = path.stat()
    _write("second version")
    os.utime(path, (stat.st_atime, stat.st_mtime))
    assert sessionmd.segments_for_path(path) == first
    # A moved mtime re-renders.
    os.utime(path, (stat.st_atime + 5, stat.st_mtime + 5))
    assert "second version" in "".join(t for t, _ in sessionmd.segments_for_path(path))


def test_prompt_fences_escaped_assistant_fences_balanced() -> None:
    """A pasted terminal fence in a PROMPT must not swallow the rest of the file."""
    paste = "```% bash drill.sh\noutput line\nmain```; after i run"
    events = [
        _p(paste),
        _t("Reply with code:\n```python\nx = 1\n```\nok"),
        _t("```\ndangling"),
        _p("next ask"),
    ]
    body = sessionmd.render_session(events)
    assert "\\```% bash drill.sh" in body  # prompt fence neutralized (renders literal)
    assert "```python\nx = 1\n```" in body  # balanced assistant fences kept rendering
    assert "```\ndangling\n```" in body  # odd assistant fence gets self-closed
    assert "## (2) you\n\nnext ask" in body  # structure below survives
