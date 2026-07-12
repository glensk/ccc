"""Unit tests for ``ccc peek`` resolution (no GUI; AppKit is never imported)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from command_center import peek
from command_center.adapters import ClaudeAdapter
from command_center.store import Store


def _user(content: object, **extra: object) -> dict:
    record: dict = {"type": "user", "message": {"role": "user", "content": content}}
    record.update(extra)
    return record


def _transcript(home: Path, cwd: str, session_id: str, records: list[dict]) -> Path:
    project = home / "projects" / cwd.replace("/", "-")
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def test_uuid_tail() -> None:
    assert peek._uuid("w0t1p0:abc-123") == "ABC-123"
    assert peek._uuid("abc-123") == "ABC-123"
    assert peek._uuid("") is None
    assert peek._uuid(None) is None


def test_last_user_prompt_picks_last_human_turn(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    records = [
        _user("first prompt"),
        {"type": "assistant", "message": {"role": "assistant", "content": "hi"}},
        _user([{"type": "tool_result", "content": "x"}]),  # tool result → skipped
        _user("second prompt"),
        _user("meta noise", isMeta=True),  # meta → skipped
        _user([{"type": "tool_result", "content": "y"}]),  # tool result → skipped
    ]
    _transcript(tmp_path, "/Users/x/repo", "sid", records)
    assert adapter.last_user_prompt("/Users/x/repo", "sid") == "second prompt"


def test_last_user_prompt_strips_wrappers(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    text = "<command-name>/aim</command-name><command-args>x</command-args>real ask"
    _transcript(tmp_path, "/Users/x/repo", "sid", [_user(text)])
    assert adapter.last_user_prompt("/Users/x/repo", "sid") == "real ask"


def test_task_notifications_are_not_prompts(tmp_path: Path) -> None:
    # Background-task completion notices arrive as *user* records whose whole content
    # is a <task-notification> block — they are not human prompts and must be skipped.
    adapter = ClaudeAdapter(claude_home=tmp_path)
    lone = "<task-notification><task-id>abc</task-id><result>done</result></task-notification>"
    trailing = "please continue\n<task-notification><task-id>d</task-id></task-notification>"
    records = [
        _user("first prompt"),
        _user(lone),  # lone notification → dropped
        _user("second prompt"),
        _user(trailing),  # real ask with a notification tacked on → keep the ask only
    ]
    _transcript(tmp_path, "/Users/x/repo", "sid", records)
    assert adapter.all_user_prompts("/Users/x/repo", "sid") == [
        "first prompt",
        "second prompt",
        "please continue",
    ]


def test_last_user_prompt_text_block_list(tmp_path: Path) -> None:
    # A prompt with an attachment arrives as a block list; the text block is the ask.
    adapter = ClaudeAdapter(claude_home=tmp_path)
    content = [{"type": "image", "source": {}}, {"type": "text", "text": "describe this"}]
    _transcript(tmp_path, "/Users/x/repo", "sid", [_user(content)])
    assert adapter.last_user_prompt("/Users/x/repo", "sid") == "describe this"


def test_last_user_prompt_none_when_no_human_turn(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    _transcript(
        tmp_path, "/Users/x/repo", "sid", [_user([{"type": "tool_result", "content": "x"}])]
    )
    assert adapter.last_user_prompt("/Users/x/repo", "sid") is None
    assert adapter.last_user_prompt("/Users/x/none", "missing") is None  # no transcript


def test_session_for_uuid_most_recent_wins(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.ensure("old", cwd="/Users/x/repo")
    store.update_fields("old", iterm_session_id="w0t1p0:UUID-A", last_response_at=100)
    store.ensure("new", cwd="/Users/x/repo")
    store.update_fields("new", iterm_session_id="w9t9p9:uuid-a", last_response_at=200)
    store.ensure("other", cwd="/Users/x/o")
    store.update_fields("other", iterm_session_id="w0t1p0:UUID-B", last_response_at=999)

    chosen = peek._session_for_uuid(store, "UUID-A")
    assert chosen is not None and chosen.session_id == "new"  # case-insensitive, newest
    assert peek._session_for_uuid(store, "UUID-MISSING") is None
    store.close()


def test_resolve_prompt_via_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    store = Store(tmp_path / "state.db")
    store.ensure("sid", cwd="/Users/x/repo")
    store.update_fields("sid", iterm_session_id="w0t1p0:UUID-A", last_response_at=10)
    _transcript(tmp_path, "/Users/x/repo", "sid", [_user("hello there")])

    monkeypatch.setattr(peek, "frontmost_iterm_uuid", lambda: "UUID-A")
    text, _label = peek.resolve_prompt(adapter=adapter, store=store)
    assert text == "hello there"
    store.close()


def test_resolve_prompt_no_focus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(peek, "frontmost_iterm_uuid", lambda: None)
    text, label = peek.resolve_prompt()
    assert text is None
    assert "no focused" in label


def test_resolve_prompt_fallback_to_newest_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    store = Store(tmp_path / "state.db")  # empty: no UUID match → cwd fallback
    older = _transcript(tmp_path, "/Users/x/repo", "sidA", [_user("older")])
    newer = _transcript(tmp_path, "/Users/x/repo", "sidB", [_user("newest prompt")])
    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))

    monkeypatch.setattr(peek, "frontmost_iterm_uuid", lambda: "NO-MATCH")
    monkeypatch.setattr(peek, "frontmost_iterm_cwd", lambda: "/Users/x/repo")
    text, _label = peek.resolve_prompt(adapter=adapter, store=store)
    assert text == "newest prompt"
    store.close()


def test_all_user_prompts_returns_every_human_turn_in_order(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    records = [
        _user("first prompt"),
        {"type": "assistant", "message": {"role": "assistant", "content": "hi"}},
        _user([{"type": "tool_result", "content": "x"}]),  # tool result → skipped
        _user("second prompt"),
        _user("meta noise", isMeta=True),  # meta → skipped
        _user("third prompt"),
    ]
    _transcript(tmp_path, "/Users/x/repo", "sid", records)
    assert adapter.all_user_prompts("/Users/x/repo", "sid") == [
        "first prompt",
        "second prompt",
        "third prompt",
    ]
    assert adapter.all_user_prompts("/Users/x/none", "missing") == []  # no transcript


def test_resolve_peek_via_store_has_prompts_and_aim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    store = Store(tmp_path / "state.db")
    store.ensure("sid", cwd="/Users/x/repo")
    store.update_fields("sid", iterm_session_id="w0t1p0:UUID-A", last_response_at=10)
    store.set_aim("sid", "first aim")
    store.set_aim("sid", "second aim")  # → two AIM-history rows, oldest first
    _transcript(tmp_path, "/Users/x/repo", "sid", [_user("ask one"), _user("ask two")])

    monkeypatch.setattr(peek, "frontmost_iterm_uuid", lambda: "UUID-A")
    data = peek.resolve_peek(adapter=adapter, store=store)
    assert data.resolved is True
    assert data.prompts == ["ask one", "ask two"]
    assert [rev.aim for rev in data.aim_revisions] == ["first aim", "second aim"]
    store.close()


def test_resolve_peek_populates_header_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The header needs session_id, cwd, the tab badge, and the id-background colour.
    from command_center import tabsymbol

    adapter = ClaudeAdapter(claude_home=tmp_path)
    store = Store(tmp_path / "state.db")
    store.ensure("sid", cwd="/Users/x/repo")
    store.update_fields("sid", iterm_session_id="w0t1p0:UUID-A", last_response_at=10)
    _transcript(tmp_path, "/Users/x/repo", "sid", [_user("hi")])

    # Point both per-tab caches at tmp dirs and seed this tab's badge + colour.
    monkeypatch.setenv("CCC_TAB_SYMBOL_DIR", str(tmp_path / "sym"))
    monkeypatch.setenv("CCC_TAB_RGB_DIR", str(tmp_path / "rgb"))
    (tmp_path / "sym").mkdir()
    (tmp_path / "rgb").mkdir()
    (tmp_path / "sym" / "w0t1p0_UUID-A").write_text("💙", encoding="utf-8")
    (tmp_path / "rgb" / "w0t1p0_UUID-A").write_text("174;198;232\n", encoding="utf-8")

    monkeypatch.setattr(peek, "frontmost_iterm_uuid", lambda: "UUID-A")
    data = peek.resolve_peek(adapter=adapter, store=store)
    assert data.session_id == "sid"
    assert data.cwd == "/Users/x/repo"
    assert data.badge == "💙"
    assert data.id_rgb == (174, 198, 232)
    assert tabsymbol.read("w0t1p0:UUID-A") == "💙"  # sanity: reads the same cache
    store.close()


def test_tab_rgb_reads_cache_and_rejects_garbage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center import colors

    monkeypatch.setenv("CCC_TAB_RGB_DIR", str(tmp_path))
    (tmp_path / "w0t1p0_A").write_text("10;20;30\n", encoding="utf-8")
    (tmp_path / "w0t1p0_B").write_text("300;0;0", encoding="utf-8")  # out of range
    (tmp_path / "w0t1p0_C").write_text("not;a;color", encoding="utf-8")
    assert colors.tab_rgb("w0t1p0:A") == (10, 20, 30)  # ":" → "_" slug
    assert colors.tab_rgb("w0t1p0:B") is None
    assert colors.tab_rgb("w0t1p0:C") is None
    assert colors.tab_rgb("w0t1p0:missing") is None
    assert colors.tab_rgb(None) is None


def test_resolve_peek_fallback_has_prompts_but_no_aim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    store = Store(tmp_path / "state.db")  # empty: no UUID match → cwd fallback
    _transcript(tmp_path, "/Users/x/repo", "sidA", [_user("p1"), _user("p2")])
    monkeypatch.setattr(peek, "frontmost_iterm_uuid", lambda: "NO-MATCH")
    monkeypatch.setattr(peek, "frontmost_iterm_cwd", lambda: "/Users/x/repo")
    data = peek.resolve_peek(adapter=adapter, store=store)
    assert data.resolved is True
    assert data.prompts == ["p1", "p2"]
    assert data.aim_revisions == []  # an untracked tab has no AIM history
    store.close()


def test_resolve_peek_no_focus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(peek, "frontmost_iterm_uuid", lambda: None)
    data = peek.resolve_peek()
    assert data.resolved is False
    assert data.prompts == [] and data.aim_revisions == []
    assert "no focused" in data.label


def test_format_prompts_numbers_oldest_first() -> None:
    out = peek.format_prompts(["alpha", "beta", "gamma"])
    # Oldest is (1) and sits above the newest (3), so the panel opens scrolled to the end.
    assert out.index("(1) ─") < out.index("alpha") < out.index("(2) ─") < out.index("beta")
    assert out.index("beta") < out.index("(3) ─") < out.index("gamma")
    # Each prompt is headed by a `(N) ───…` rule followed by exactly ONE empty line.
    assert "(2) " + "─" * (peek._RULE_WIDTH - len("(2) ")) + "\n\nbeta" in out
    assert peek.format_prompts([]) == "(no prompts in this session yet)"


def test_prompt_segments_tags_rules_and_last_prompt() -> None:
    segments = peek.prompt_segments(["a", "b", "c"])
    # One header rule per prompt; only the NEWEST prompt body is tagged "last" (gold).
    assert [tag for _text, tag in segments].count("rule") == 3
    last = [text for text, tag in segments if tag == "last"]
    assert last == ["c"]
    # The plain text is exactly the concatenation of the segments (single source).
    assert "".join(text for text, _tag in segments) == peek.format_prompts(["a", "b", "c"])


def test_format_aim_marks_current_and_shows_short() -> None:
    from command_center.models import AimRevision

    revisions = [
        AimRevision("old aim", 40, 1_700_000_000_000, "old short"),
        AimRevision("new aim", 70, 1_700_000_100_000, None),
    ]
    out = peek.format_aim(revisions)
    assert "(1)" in out and "(2)" in out
    assert "← current" in out.split("(2)", 1)[1]  # the marker is on the last revision only
    assert "↳ short: old short" in out
    assert "40%" in out and "70%" in out
    assert peek.format_aim([]) == "(no AIM set for this session yet)"


# ---------------------------------------------------------------------------
# session tab + ccc-TUI-row fallback (PLAN_session-mirrors.md)
# ---------------------------------------------------------------------------
def test_resolve_peek_includes_session_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    store = Store(tmp_path / "state.db")
    store.ensure("sid", cwd="/Users/x/repo")
    store.update_fields("sid", iterm_session_id="w0t1p0:UUID-A", last_response_at=10)
    _transcript(
        tmp_path,
        "/Users/x/repo",
        "sid",
        [
            _user("hello there"),
            {"type": "assistant", "message": {"role": "assistant", "content": "hi back"}},
        ],
    )
    monkeypatch.setattr(peek, "frontmost_iterm_uuid", lambda: "UUID-A")
    data = peek.resolve_peek(adapter=adapter, store=store)
    body = peek.format_session(data.session_segments)
    assert "## (1) you\n\nhello there" in body
    assert "hi back" in body
    store.close()


def test_resolve_peek_prefers_tui_selected_row_over_stale_uuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O3: focused tab IS the ccc TUI → the selected row wins over a stale uuid map."""
    adapter = ClaudeAdapter(claude_home=tmp_path)
    store = Store(tmp_path / "state.db")
    # "stale" once ran in the tab the TUI now occupies (its uuid is recorded there).
    store.ensure("stale", cwd="/Users/x/repo")
    store.update_fields("stale", iterm_session_id="w0t1p0:UUID-TUI", last_response_at=10)
    store.ensure("selected", cwd="/Users/x/other")  # a PARKED row (no live tab)
    _transcript(tmp_path, "/Users/x/repo", "stale", [_user("stale prompt")])
    _transcript(tmp_path, "/Users/x/other", "selected", [_user("selected prompt")])

    monkeypatch.setattr(peek, "frontmost_iterm_uuid", lambda: "UUID-TUI")
    monkeypatch.setattr(peek, "_focused_tty", lambda: "/dev/ttys007")
    monkeypatch.setattr("command_center.jump.find_ccc_tty", lambda: "/dev/ttys007")
    monkeypatch.setattr("command_center.jumpstate.get_selected", lambda: "selected")
    data = peek.resolve_peek(adapter=adapter, store=store)
    assert data.resolved and data.session_id == "selected"
    assert data.prompts == ["selected prompt"]

    # A different focused tty (an ordinary session tab) → the uuid map wins again.
    monkeypatch.setattr(peek, "_focused_tty", lambda: "/dev/ttys002")
    data = peek.resolve_peek(adapter=adapter, store=store)
    assert data.session_id == "stale"
    store.close()


def test_resolve_peek_session_id_bypasses_focus_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ccc peek --session <id>` (the TUI `sp` chord) hits that exact row — no focus probes."""
    adapter = ClaudeAdapter(claude_home=tmp_path)
    store = Store(tmp_path / "state.db")
    store.ensure("target", cwd="/Users/x/other")  # a PARKED row (no live tab)
    store.set_aim("target", "the target aim")
    _transcript(tmp_path, "/Users/x/other", "target", [_user("target prompt")])

    def _boom() -> str:
        raise AssertionError("no focus detection may run when a session id is given")

    monkeypatch.setattr(peek, "frontmost_iterm_uuid", _boom)
    monkeypatch.setattr(peek, "_focused_tty", _boom)
    monkeypatch.setattr("command_center.jump.find_ccc_tty", _boom)
    data = peek.resolve_peek(adapter=adapter, store=store, session_id="target")
    assert data.resolved and data.session_id == "target"
    assert data.prompts == ["target prompt"]
    assert [rev.aim for rev in data.aim_revisions] == ["the target aim"]

    # An unknown id resolves to nothing (labelled), still without any focus probe.
    missing = peek.resolve_peek(adapter=adapter, store=store, session_id="nope")
    assert missing.resolved is False and "nope" in missing.label
    store.close()


def test_ccc_fallback_inactive_without_tui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No ccc TUI process → no AppleScript probe, straight to the uuid map."""
    adapter = ClaudeAdapter(claude_home=tmp_path)
    store = Store(tmp_path / "state.db")
    store.ensure("sid", cwd="/Users/x/repo")
    store.update_fields("sid", iterm_session_id="w0t1p0:UUID-A", last_response_at=10)
    _transcript(tmp_path, "/Users/x/repo", "sid", [_user("hello")])

    def _boom() -> str:
        raise AssertionError("focused-tty probe must not run when no TUI is up")

    monkeypatch.setattr(peek, "frontmost_iterm_uuid", lambda: "UUID-A")
    monkeypatch.setattr(peek, "_focused_tty", _boom)
    monkeypatch.setattr("command_center.jump.find_ccc_tty", lambda: None)
    monkeypatch.setattr("command_center.jumpstate.get_selected", lambda: "sid")
    data = peek.resolve_peek(adapter=adapter, store=store)
    assert data.session_id == "sid"
    store.close()


# --------------------------------------------------------------------------- #
# non-macOS: peek auto-degrades to --print (no AppKit panel)
# --------------------------------------------------------------------------- #
def test_run_prints_and_skips_panel_off_macos(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(peek.sys, "platform", "linux")
    monkeypatch.setattr(peek, "frontmost_iterm_uuid", lambda: None)  # nothing focused

    def _no_panel(*_a: object, **_k: object) -> None:
        raise AssertionError("show_panel must not be called off macOS")

    monkeypatch.setattr(peek, "show_panel", _no_panel)
    code = peek.run(argparse.Namespace(session=None, print_only=False, timeout=0.0))
    assert code == 0
    assert capsys.readouterr().out.strip()  # something was printed (not a panel)


def test_run_shows_panel_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(peek.sys, "platform", "darwin")
    monkeypatch.setattr(peek, "frontmost_iterm_uuid", lambda: None)
    shown: list[bool] = []
    monkeypatch.setattr(peek, "show_panel", lambda *_a, **_k: shown.append(True))
    code = peek.run(argparse.Namespace(session=None, print_only=False, timeout=0.0))
    assert code == 0
    assert shown == [True]  # the AppKit panel path was taken on macOS
