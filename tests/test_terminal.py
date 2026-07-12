"""Unit tests for the terminal/iTerm helpers (AppleScript stubbed out)."""

from __future__ import annotations

import pytest

from command_center import terminal


def test_close_iterm_session_maps_osascript_output(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_osascript(script: str) -> str:
        captured["script"] = script
        return "tab\n"

    monkeypatch.setattr(terminal, "_osascript", fake_osascript)
    assert terminal.close_iterm_session("w0t1p0:ABC-123") == "tab"
    # The UUID after the colon is what the AppleScript matches on.
    assert "ABC-123" in captured["script"]


def test_close_iterm_session_session_vs_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminal, "_osascript", lambda _s: "session")
    assert terminal.close_iterm_session("w0t1p0:UUID") == "session"

    # Not located / unknown output -> "".
    monkeypatch.setattr(terminal, "_osascript", lambda _s: "")
    assert terminal.close_iterm_session("w0t1p0:UUID") == ""
    monkeypatch.setattr(terminal, "_osascript", lambda _s: "weird")
    assert terminal.close_iterm_session("w0t1p0:UUID") == ""


def test_close_iterm_session_no_osascript(monkeypatch: pytest.MonkeyPatch) -> None:
    # osascript missing / failed -> None -> "".
    monkeypatch.setattr(terminal, "_osascript", lambda _s: None)
    assert terminal.close_iterm_session("w0t1p0:UUID") == ""
    # No UUID at all -> "" without even invoking AppleScript.
    assert terminal.close_iterm_session(":") == ""
    assert terminal.close_iterm_session("") == ""


def test_set_session_titles_builds_script(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(terminal.shutil, "which", lambda _name: "/usr/bin/osascript")
    monkeypatch.setattr(terminal.subprocess, "Popen", lambda cmd, **_kw: calls.append(cmd))
    terminal.set_session_titles({"w0t1p0:ABC-123": '🔴 my"repo'})
    assert len(calls) == 1
    script = calls[0][-1]
    assert "ABC-123" in script  # keyed on the UUID after the colon
    assert '🔴 my\\"repo' in script  # title embedded with the quote escaped


def test_set_session_titles_skips_when_empty_or_no_osascript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(terminal.subprocess, "Popen", lambda cmd, **_kw: calls.append(cmd))

    # Nothing to set -> no subprocess, even if osascript exists.
    monkeypatch.setattr(terminal.shutil, "which", lambda _name: "/usr/bin/osascript")
    terminal.set_session_titles({})
    # osascript missing -> no subprocess, even with titles to set.
    monkeypatch.setattr(terminal.shutil, "which", lambda _name: None)
    terminal.set_session_titles({"w0t1p0:UUID": "🔴 repo"})
    assert calls == []


def test_set_session_titles_preserving_builds_marker_aware_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(terminal.shutil, "which", lambda _name: "/usr/bin/osascript")
    monkeypatch.setattr(terminal.subprocess, "Popen", lambda cmd, **_kw: calls.append(cmd))

    terminal.set_session_titles_preserving({"w0t1p0:ABC-123": "🟧 cscs-api"}, marker="🔴 ")
    assert len(calls) == 1
    script = calls[0][-1]
    assert "ABC-123" in script  # keyed on the UUID after the colon
    assert "🟧 cscs-api" in script  # the desired core is embedded
    # Marker preserved: it measures the marker length and slices the title past it,
    # so a "waiting" tab keeps its 🔴 while only the badge+leaf core is rewritten.
    assert 'set mlen to (count of "🔴 ")' in script
    assert "starts with" in script and "text (mlen + 1) thru -1 of n" in script


def test_set_session_titles_preserving_skips_when_empty_or_no_osascript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(terminal.subprocess, "Popen", lambda cmd, **_kw: calls.append(cmd))

    monkeypatch.setattr(terminal.shutil, "which", lambda _name: "/usr/bin/osascript")
    terminal.set_session_titles_preserving({})
    monkeypatch.setattr(terminal.shutil, "which", lambda _name: None)
    terminal.set_session_titles_preserving({"w0t1p0:UUID": "🟧 repo"})
    assert calls == []
