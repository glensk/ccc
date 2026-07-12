"""Headless smoke test for the Textual TUI (no real terminal needed)."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from rich.text import Text
from textual.widgets import Label, ListView

from command_center.store import Store

# Generic repo-tree root for the category-grouping fixtures (no personal anchors).
_BASE = "/repo-root"


@pytest.fixture(autouse=True)
def _color_textual_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run Textual TUI tests with color enabled and category grouping pointed at ``_BASE``."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("GIT_BASE", _BASE)


def _seed(home: Path) -> str:
    store = Store(home / "command-center" / "state.db")
    sid = "tui-test-session"
    store.ensure(sid, cwd="/Users/x/repo")
    store.update_fields(sid, aim="ship the thing", next_step="- do the next bit")
    store.set_subgoals(sid, ["a", "b"])
    subs = store.list_subgoals(sid)
    store.set_subgoal_checked(subs[0].id, True)
    store.close()
    return sid


def _seed_live_registry(home: Path, sid: str, pid: int) -> None:
    """Make *sid* look live by writing a registry entry for an alive *pid*."""
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{pid}.json").write_text(
        json.dumps({"pid": pid, "sessionId": sid, "cwd": "/Users/x/repo", "status": "idle"}),
        encoding="utf-8",
    )


def _styled_fragments(text: Text, style: str) -> list[str]:
    return [text.plain[span.start : span.end] for span in text.spans if str(span.style) == style]


def test_tui_mounts_lists_and_marks_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    sid = _seed(tmp_path)

    from command_center.views.tui import CommandCenterApp, SessionTable

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#sessions", SessionTable)
            assert table.row_count >= 1  # the seeded session shows up

            # Select it and confirm the detail pane + read-only fields render.
            app._current = sid
            app.update_detail()
            await pilot.pause()
            head = app.query_one("#detail-head")
            rendered = head.render()
            head_text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
            assert "1/2" in head_text  # one of two sub-goals checked (context block)
            # The editable fields live in their own static so edit mode can swap just
            # them in place; the AIM is rendered there, not in the context head.
            fview = app.query_one("#detail-fields-view")
            frendered = fview.render()
            fview_text = frendered.plain if hasattr(frendered, "plain") else str(frendered)
            assert "ship the thing" in fview_text  # aim shown in the read-only fields block

            # Mark done via the action and confirm it persisted.
            app.action_mark_done()
            await pilot.pause()

    asyncio.run(scenario())

    store = Store(tmp_path / "command-center" / "state.db")
    session = store.get(sid)
    store.close()
    assert session is not None and session.done is True


def test_table_row_paints_only_low_aim_score_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store(tmp_path / "command-center" / "state.db")
    sid = "low-aim-session"
    store.ensure(sid, cwd="/Users/x/repo")
    store.update_fields(sid, aim="improve things", aim_score=20)
    store.close()

    from command_center.views.tui import _AIM_COL, CommandCenterApp, SessionTable

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#sessions", SessionTable)
            aim_cell = table.get_row_at(table.get_row_index(sid))[_AIM_COL]
            assert isinstance(aim_cell, Text)
            assert "20% improve things" in aim_cell.plain
            assert _styled_fragments(aim_cell, "bold red") == ["20% "]

    asyncio.run(scenario())


def test_table_row_shows_codex_badge_and_waiting_reset_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    from command_center import usage

    sid = "codex-workflow-session"
    store = Store(tmp_path / "command-center" / "state.db")
    store.ensure(sid, cwd="/Users/x/repo")
    store.update_fields(sid, aim="ship with codex", job_type="codex", version="2.1.193")
    store.close()
    _seed_live_registry(tmp_path, sid, os.getpid())
    monkeypatch.setattr(
        usage,
        "read_codex_usage",
        lambda: usage.Usage(int(time.time()), usage.Window(100.0, int(time.time()) + 3600), None),
    )
    # The seeded live pid is this pytest process, whose descendants are unpredictable
    # across the full suite. The status-icon has a live has_subagent() override (a fresh
    # "working" ▶ cue) that would mask the derived status if a stray `claude` descendant
    # of pytest appears. Neutralize it so this test exercises purely the WAITING_CODEX
    # status->icon mapping — a real waiting-for-Codex session is idle with no `claude`
    # child, so has_subagent is False in production and 😴 shows unmasked.
    from command_center.adapters.claude import ClaudeAdapter

    monkeypatch.setattr(ClaudeAdapter, "has_subagent", lambda self, pid: False)

    from command_center.views.tui import _NEXT_COL, CommandCenterApp, SessionTable

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#sessions", SessionTable)
            cells = table.get_row_at(table.get_row_index(sid))
            icon = cells[0]
            ver = cells[2]
            nxt = cells[_NEXT_COL]
            assert isinstance(icon, Text)
            assert icon.plain == "😴"
            assert isinstance(ver, Text)
            assert ver.plain == "  OAI"
            assert "193" not in ver.plain
            assert _styled_fragments(ver, "bold black on white") == ["OAI"]
            assert isinstance(nxt, Text)
            assert "waiting for Codex 5h reset" in nxt.plain

    asyncio.run(scenario())


def test_detail_aim_line_paints_only_low_score_chip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    from command_center.views.tui import CommandCenterApp

    app = CommandCenterApp()
    app.cfg.aim_score_threshold = 50
    line = Text()
    app._append_aim_line(line, 1, "improve things", 20, "name a passing test")

    assert line.plain.startswith("/aim (1): 20% improve things")
    assert _styled_fragments(line, "bold red") == ["20% "]
    assert "improve things" not in "".join(_styled_fragments(line, "bold red"))

    unscored = Text()
    app._append_aim_line(unscored, 1, "some aim", -1, None)
    # Unscored (-1) shows no chip at all — the raw sentinel is never surfaced (a blank
    # chip degrades gracefully when scoring is disabled instead of a stuck "-1").
    assert unscored.plain == "/aim (1): some aim"
    assert "-1" not in unscored.plain
    assert _styled_fragments(unscored, "bold red") == []


def test_toggle_state_label_reports_live_toggle_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `t` menu annotates each toggle with its live state — ti's on/silent especially."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    (tmp_path / "settings.json").write_text('{"agentPushNotifEnabled": true}\n', encoding="utf-8")
    from command_center.views.tui import CommandCenterApp

    app = CommandCenterApp()

    # ti reflects Claude Code's settings.json live (on → silent after a mute).
    assert app._toggle_state_label("toggle_idle") == "on"
    (tmp_path / "settings.json").write_text('{"agentPushNotifEnabled": false}\n', encoding="utf-8")
    assert app._toggle_state_label("toggle_idle") == "silent"

    # td / tf reflect the view-local booleans; an unknown action has no state.
    app._show_finished = False
    app._show_future = True
    assert app._toggle_state_label("toggle_finished") == "hidden"
    assert app._toggle_state_label("toggle_future") == "shown"
    assert app._toggle_state_label("resume") is None


def test_mark_done_live_offers_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On a LIVE session, 'd' marks done then asks to close; confirming signals +
    closes the iTerm pane/tab."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    sid = _seed(tmp_path)
    # Live registry entry for this (alive) process => the row is "live" with a tab.
    # reconcile() will set last_seen_pid to this pid, so the SIGTERM targets it.
    _seed_live_registry(tmp_path, sid, os.getpid())
    store = Store(tmp_path / "command-center" / "state.db")
    store.update_fields(sid, iterm_session_id="w0t1p0:UUID-1")
    store.close()

    from command_center.views import tui as tui_mod
    from command_center.views.tui import CommandCenterApp, ConfirmScreen

    killed: list[int] = []
    closed: list[str] = []

    def fake_kill(pid: int, _sig: int) -> None:
        # Swallow (don't re-raise) — textual signals its own pid during teardown,
        # so delegating real os.kill here would terminate the test runner.
        killed.append(pid)

    def fake_close(iterm_session_id: str) -> str:
        closed.append(iterm_session_id)
        return "tab"

    monkeypatch.setattr(tui_mod.os, "kill", fake_kill)
    monkeypatch.setattr(tui_mod.terminal, "close_iterm_session", fake_close)

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._current = sid
            app.action_mark_done()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ConfirmScreen)  # confirmation popped
            # Single-button variant: only "Close" is offered (no "Keep open" to
            # preselect); Esc is how you decline.
            from textual.widgets import Button

            buttons = list(screen.query(Button))
            assert [b.id for b in buttons] == ["yes"]
            assert "Close" in str(buttons[0].label)
            screen.dismiss(True)  # confirm "close"
            await pilot.pause()

    asyncio.run(scenario())

    # reconcile set last_seen_pid to the live registry pid (this process), so the
    # close flow SIGTERMs that and closes the recorded iTerm session.
    assert os.getpid() in killed  # SIGTERM'd the session's process
    assert closed == ["w0t1p0:UUID-1"]  # closed its iTerm pane/tab

    store = Store(tmp_path / "command-center" / "state.db")
    session = store.get(sid)
    store.close()
    assert session is not None and session.done is True


def test_mark_done_parked_no_dialog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-live session marks done silently — no close dialog (no tab to close)."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    sid = _seed(tmp_path)  # no live registry entry => parked

    from command_center.views.tui import CommandCenterApp, ConfirmScreen

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._current = sid
            app.action_mark_done()
            await pilot.pause()
            assert not isinstance(app.screen, ConfirmScreen)  # no dialog

    asyncio.run(scenario())


def test_close_live_closes_tab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """'c' on a LIVE session SIGTERMs it, parks it, AND closes its iTerm pane/tab."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    sid = _seed(tmp_path)
    _seed_live_registry(tmp_path, sid, os.getpid())  # alive pid => row is "live"
    store = Store(tmp_path / "command-center" / "state.db")
    store.update_fields(sid, iterm_session_id="w0t1p0:UUID-1")
    store.close()

    from command_center.views import tui as tui_mod
    from command_center.views.tui import CommandCenterApp

    killed: list[int] = []
    closed: list[str] = []

    def fake_kill(pid: int, _sig: int) -> None:
        killed.append(pid)  # swallow — textual signals its own pid during teardown

    def fake_close(iterm_session_id: str) -> str:
        closed.append(iterm_session_id)
        return "tab"

    monkeypatch.setattr(tui_mod.os, "kill", fake_kill)
    monkeypatch.setattr(tui_mod.terminal, "close_iterm_session", fake_close)

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._current = sid
            app.action_close()
            await pilot.pause()

    asyncio.run(scenario())

    assert os.getpid() in killed  # SIGTERM'd the session's process
    assert closed == ["w0t1p0:UUID-1"]  # closed its iTerm pane/tab
    # (status isn't asserted here: the faked os.kill keeps this process alive, so the
    # post-close reconcile re-marks the live-registry session idle. The parked-status
    # path is covered by test_close_parked_no_tab_close, where the pid is truly gone.)


def test_close_parked_no_tab_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """'c' on a non-live session parks it but never tries to close an iTerm tab."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    sid = _seed(tmp_path)  # no live registry entry => parked
    store = Store(tmp_path / "command-center" / "state.db")
    store.update_fields(sid, iterm_session_id="w0t1p0:UUID-1")  # stale id, but not live
    store.close()

    from command_center.views import tui as tui_mod
    from command_center.views.tui import CommandCenterApp

    closed: list[str] = []

    def fake_close(iterm_session_id: str) -> str:
        closed.append(iterm_session_id)
        return "tab"

    monkeypatch.setattr(tui_mod.terminal, "close_iterm_session", fake_close)

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._current = sid
            app.action_close()
            await pilot.pause()

    asyncio.run(scenario())

    assert closed == []  # no tab-close attempted for a parked session

    store = Store(tmp_path / "command-center" / "state.db")
    session = store.get(sid)
    store.close()
    assert session is not None and session.status == "parked"


def test_close_done_session_stays_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """'c' on a done session never demotes it to PARKED — it stays DONE so the row
    sinks to the FINISHED section instead of reappearing in the active list."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    sid = _seed(tmp_path)  # no live registry entry
    store = Store(tmp_path / "command-center" / "state.db")
    store.update_fields(sid, done=True, status="done", done_at=1)
    store.close()

    from command_center.views.tui import CommandCenterApp

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._current = sid
            app.action_close()
            await pilot.pause()

    asyncio.run(scenario())

    store = Store(tmp_path / "command-center" / "state.db")
    session = store.get(sid)
    store.close()
    assert session is not None and session.done is True
    assert session.status == "done"  # not clobbered to "parked"


def test_row_shows_per_tab_badge_before_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A LIVE session whose iTerm tab has a claimed badge shows that emoji before the repo.

    The badge is gated on liveness: a parked/finished row's badge no longer maps to any
    open tab, so only a live row renders it (see ``test_parked_row_hides_badge``).
    """
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("CCC_TAB_SYMBOL_DIR", str(tmp_path / "iterm-tab-symbol"))
    from command_center import tabsymbol

    base = _BASE
    cwd = f"{base}/home/repo-a"
    store = Store(tmp_path / "command-center" / "state.db")
    store.ensure("a", cwd=cwd)
    store.update_fields("a", status="idle", aim="ship it", iterm_session_id="w0t0p0:UUID-A")
    store.close()
    # Make it live (alive pid + matching cwd) so the badge is shown.
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{os.getpid()}.json").write_text(
        json.dumps({"pid": os.getpid(), "sessionId": "a", "cwd": cwd, "status": "idle"}),
        encoding="utf-8",
    )
    badge = tabsymbol.assign("w0t0p0:UUID-A")

    from command_center.views.tui import CommandCenterApp, SessionTable

    folder_col: list[str] = []

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#sessions", SessionTable)
            for i in range(table.row_count):
                cell = table.get_row_at(i)[3]  # folder column (after icon, !, ver)
                folder_col.append(cell.plain if hasattr(cell, "plain") else str(cell))

    asyncio.run(scenario())
    assert any(f"{badge} repo-a" in cell for cell in folder_col)


def test_parked_row_shows_deterministic_badge_not_live_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parked (non-live) row shows its DETERMINISTIC per-repo badge, not the live iTerm one.

    The live iTerm-tab cache badge is gated on liveness (the tab is gone, its
    $ITERM_SESSION_ID may be recycled); the deterministic symbol_for_repo(cwd) fallback
    still renders so a screenshot / list always shows a per-repo symbol.
    """
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("CCC_TAB_SYMBOL_DIR", str(tmp_path / "iterm-tab-symbol"))
    from command_center import tabsymbol

    base = _BASE
    cwd = f"{base}/home/repo-a"
    store = Store(tmp_path / "command-center" / "state.db")
    store.ensure("a", cwd=cwd)
    # No live registry entry → reconcile() parks it; the live cache badge must NOT render.
    store.update_fields("a", status="idle", aim="ship it", iterm_session_id="w0t0p0:UUID-A")
    store.close()
    # Force the live cache badge to differ from the deterministic one so the assertions
    # are unambiguous (assign takes the palette head; the deterministic one is a hash).
    live_badge = tabsymbol.assign("w0t0p0:UUID-A")
    deterministic = tabsymbol.symbol_for_repo(cwd)
    assert live_badge is not None and deterministic and live_badge != deterministic

    from command_center.views.tui import CommandCenterApp, SessionTable

    folder_col: list[str] = []

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#sessions", SessionTable)
            for i in range(table.row_count):
                cell = table.get_row_at(i)[3]  # folder column (after icon, !, ver)
                folder_col.append(cell.plain if hasattr(cell, "plain") else str(cell))

    asyncio.run(scenario())
    assert any(f"{deterministic} repo-a" in cell for cell in folder_col)  # deterministic shown
    assert all(live_badge not in cell for cell in folder_col)  # live cache badge NOT shown


def test_list_groups_by_category_with_nested_repos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each repo category shown once as a header with repos nested; never split.

    sdsc holds both an AIM and a no-AIM session — the bug was that AIM-bucketing
    split such a category into two headers. It must now render as a single ``sdsc``
    block (AIM repo first), preceded by the earlier ``home`` category.
    """
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    base = _BASE
    store = Store(tmp_path / "command-center" / "state.db")
    store.ensure("a", cwd=f"{base}/sdsc/repo-a")
    store.update_fields("a", status="idle", aim="ship the thing")  # sdsc + AIM
    store.ensure("b", cwd=f"{base}/home/repo-b")
    store.update_fields("b", status="parked")  # home, no AIM
    store.ensure("c", cwd=f"{base}/sdsc/repo-c")
    store.update_fields("c", status="parked")  # sdsc, no AIM — must stay under the sdsc header
    store.close()

    from command_center.views.tui import CommandCenterApp, SessionTable

    folder_col: list[str] = []

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#sessions", SessionTable)
            for i in range(table.row_count):
                cell = table.get_row_at(i)[3]  # folder column (after icon, importance, version)
                folder_col.append(cell.plain if hasattr(cell, "plain") else str(cell))

    asyncio.run(scenario())

    # home header → its repo, then ONE sdsc header → both repos nested (AIM-first).
    # Each repo row shows the DETERMINISTIC per-repo badge before the name (no iTerm cache
    # here, so cell_for falls back to symbol_for_repo(cwd)). Category headers now carry a
    # trailing full-width divider ("home ───…") and a FUTURE separator line is always
    # appended; normalise both out so this test stays focused on the grouping (one header
    # per category, repos nested, AIM-first) rather than divider styling.
    from command_center import tabsymbol

    def _badge(cwd: str) -> str:
        return tabsymbol.cell_for(None, cwd, live=False)

    cleaned = [c.split(" ─", 1)[0].rstrip() for c in folder_col if "FUTURE" not in c]
    # AIM session (repo-a) sorts first within sdsc; the no-AIM repo-c stays under the SAME
    # sdsc header. Each repo row leads with its deterministic per-repo badge cell.
    assert cleaned == [
        "home",
        f"  {_badge(f'{base}/home/repo-b')}repo-b",
        "sdsc",
        f"  {_badge(f'{base}/sdsc/repo-a')}repo-a",
        f"  {_badge(f'{base}/sdsc/repo-c')}repo-c",
    ]


def test_column_cursor_navigates_columns_and_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """←/→ step a cell cursor across editable columns; Enter edits; ↓ returns to row mode."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    sid = _seed(tmp_path)

    from command_center.views.tui import (
        _AIM_COL,
        _NEXT_COL,
        _PROGRESS_COL,
        CommandCenterApp,
        InputScreen,
        SessionTable,
    )

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#sessions", SessionTable)
            table.move_cursor(row=table.get_row_index(sid))
            table.focus()
            await pilot.pause()
            assert app._current == sid

            # → enters column mode on /aim; → again steps to /next-step, then progress.
            await pilot.press("right")
            await pilot.pause()
            assert table._col_mode is True
            assert table.cursor_type == "cell"
            assert table.cursor_column == _AIM_COL
            await pilot.press("right")
            await pilot.pause()
            assert table.cursor_column == _NEXT_COL
            await pilot.press("right")
            await pilot.pause()
            assert table.cursor_column == _PROGRESS_COL

            # ← steps back to /next-step, /aim, then /aim ← wraps to progress.
            await pilot.press("left")
            await pilot.pause()
            assert table.cursor_column == _NEXT_COL
            await pilot.press("left")
            await pilot.pause()
            assert table.cursor_column == _AIM_COL
            await pilot.press("left")
            await pilot.pause()
            assert table.cursor_column == _PROGRESS_COL  # wrapped to the last editable column

            # ↓ leaves column mode (whole-row selection again).
            await pilot.press("down")
            await pilot.pause()
            assert table._col_mode is False
            assert table.cursor_type == "row"

            # Re-anchor on the session (↓ wraps and may land on a category header),
            # then re-enter column mode; Enter opens the field editor for that column.
            table.move_cursor(row=table.get_row_index(sid))
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, InputScreen)

    asyncio.run(scenario())


def test_multiline_field_editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`a`/`n` open a large multiline TextArea so a long value is fully visible.

    Enter inserts a newline; Ctrl+S submits the whole (multi-line) text, Esc
    cancels without writing, and Tab completes the current ``@tag`` in place.
    """
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    sid = _seed(tmp_path)

    from textual.widgets import TextArea

    from command_center.views.tui import CommandCenterApp, InputScreen, SessionTable

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#sessions", SessionTable)
            table.move_cursor(row=table.get_row_index(sid))
            table.focus()
            await pilot.pause()
            assert app._current == sid

            # `a` (bare, after the chord window) opens the AIM editor in MULTILINE mode.
            await pilot.press("a")
            await asyncio.sleep(0.5)  # > the 0.35s chord window → falls back to edit_aim
            await pilot.pause()
            assert isinstance(app.screen, InputScreen)
            assert app.screen._multiline is True
            aim_field = app.screen.query_one("#field")
            assert isinstance(aim_field, TextArea)
            assert aim_field.text == "ship the thing"  # pre-filled with the current AIM
            # Esc cancels — no write, no score spawn.
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, InputScreen)

            # `n` opens the next-step editor (multiline + @tag Tab-completion).
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, InputScreen)
            ta = app.screen.query_one("#field", TextArea)
            ta.load_text("first line\n@wai")
            ta.move_cursor(ta.document.end)
            await pilot.pause()
            # Tab completes the half-typed "@wai" → "@waiting" at the cursor.
            await pilot.press("tab")
            await pilot.pause()
            assert ta.text == "first line\n@waiting"
            # Ctrl+S submits the full multi-line value.
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert not isinstance(app.screen, InputScreen)

    asyncio.run(scenario())

    store = Store(tmp_path / "command-center" / "state.db")
    saved = store.get(sid)
    store.close()
    assert saved is not None and saved.next_step == "first line\n@waiting"


def test_fast_row_jump_via_word_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The word-back/forward keys jump 3 rows: ctrl+left = down, ctrl+right = up.

    iTerm sends Option+←/→ as Esc-b / Esc-f, which Textual delivers as
    ctrl+left / ctrl+right — the keys the table binds to the fast row jump.
    """
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    base = _BASE
    store = Store(tmp_path / "command-center" / "state.db")
    for i in range(6):
        store.ensure(f"s{i}", cwd=f"{base}/sdsc/repo-{i}")
        store.update_fields(f"s{i}", status="idle", aim=f"aim {i}")
    store.close()

    from command_center.views.tui import CommandCenterApp, SessionTable

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#sessions", SessionTable)
            table.move_cursor(row=1)  # row 0 is the "sdsc" category header
            table.focus()
            await pilot.pause()
            start = table.cursor_row
            await pilot.press("ctrl+left")  # word-back → 3 rows down
            await pilot.pause()
            assert table.cursor_row == start + 3
            assert table._col_mode is False
            await pilot.press("ctrl+right")  # word-forward → 3 rows up
            await pilot.pause()
            assert table.cursor_row == start

            # Wrap: jumping down past the last row circles back to the top section.
            last = table.row_count - 1
            table.move_cursor(row=last)
            await pilot.pause()
            await pilot.press("ctrl+left")  # down 3 from the bottom → wraps
            await pilot.pause()
            assert table.cursor_row == (last + 3) % table.row_count
            assert table.cursor_row < last  # actually circled upward, not stuck

    asyncio.run(scenario())


def test_arrow_keys_wrap_top_and_bottom(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain ↑/↓ circle: ↑ at the top row jumps to the bottom, ↓ at the bottom to the top."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    base = _BASE
    store = Store(tmp_path / "command-center" / "state.db")
    for i in range(4):
        store.ensure(f"s{i}", cwd=f"{base}/sdsc/repo-{i}")
        store.update_fields(f"s{i}", status="idle", aim=f"aim {i}")
    store.close()

    from command_center.views.tui import CommandCenterApp, SessionTable

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#sessions", SessionTable)
            table.focus()
            last = table.row_count - 1

            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("up")  # ↑ at the top wraps to the bottom
            await pilot.pause()
            assert table.cursor_row == last

            await pilot.press("down")  # ↓ at the bottom wraps back to the top
            await pilot.pause()
            assert table.cursor_row == 0

    asyncio.run(scenario())


def _row_text(list_item) -> str:
    rendered = list_item.query_one(Label).render()
    return rendered.plain if hasattr(rendered, "plain") else str(rendered)


def test_help_scrolls_and_explains_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """'?' opens help; the reference scrolls; 'Commands & keys' explains each shortcut."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    _seed(tmp_path)

    from textual.containers import VerticalScroll

    from command_center.views.tui import (
        CommandCenterApp,
        HelpScreen,
        KeysScreen,
        TopicScreen,
        _HelpTopics,
    )

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)

            topics = app.screen.query_one("#topics", _HelpTopics)
            assert topics.has_focus
            assert _row_text(topics.children[0]).startswith("Commands & keys")

            # The reference pane scrolls with End/Home even though the list has focus.
            pane = app.screen.query_one("#help", VerticalScroll)
            await pilot.press("end")
            await pilot.pause()
            assert pane.scroll_offset.y > 0
            await pilot.press("home")
            await pilot.pause()
            assert pane.scroll_offset.y == 0

            # Open the Commands & keys explorer (first topic).
            topics.index = 0
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, KeysScreen)

            keys = app.screen.query_one("#keys", ListView)
            columns_item = next(li for li in keys.children if li.id and "columns" in _row_text(li))
            keys.index = list(keys.children).index(columns_item)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, TopicScreen)

            body = " ".join(
                _row_text_static(s) for s in app.screen.query_one("#topic").query("Static")
            )
            assert "editable columns" in body  # the ←/→ column-cursor explanation

    asyncio.run(scenario())


def test_help_lists_wrap_around(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Help topics and the keys list wrap with ↑/↓; the keys list skips disabled headers."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    _seed(tmp_path)

    from command_center.views.tui import CommandCenterApp, KeysScreen, _HelpTopics

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            topics = app.screen.query_one("#topics", _HelpTopics)
            topics.focus()
            topics.index = 0
            await pilot.pause()
            await pilot.press("up")  # ↑ at the first topic wraps to the last
            await pilot.pause()
            assert topics.index == len(topics.children) - 1
            await pilot.press("down")  # ↓ at the last wraps back to the first
            await pilot.pause()
            assert topics.index == 0

            # Into the Commands & keys list (interleaved disabled section headers).
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, KeysScreen)
            keys = app.screen.query_one("#keys", ListView)
            enabled = [i for i, item in enumerate(keys.children) if not item.disabled]
            keys.index = enabled[0]
            await pilot.pause()
            await pilot.press("up")  # wraps past the top, skipping the disabled headers
            await pilot.pause()
            assert keys.index == enabled[-1]
            assert not keys.children[keys.index].disabled  # landed on a selectable row

            # Word-back key (ctrl+left) jumps 3 selectable items, wrapping + skipping headers.
            keys.index = enabled[0]
            await pilot.pause()
            await pilot.press("ctrl+left")
            await pilot.pause()
            assert keys.index == enabled[3 % len(enabled)]
            assert not keys.children[keys.index].disabled

    asyncio.run(scenario())


def _row_text_static(static_widget) -> str:
    rendered = static_widget.render()
    return rendered.plain if hasattr(rendered, "plain") else str(rendered)


def test_help_key_renames_are_gold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Commands & keys rows show resume / close / Refresh-now with the right gold letter."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))

    from command_center.views import commands
    from command_center.views.tui import _GOLD, _key_row

    def gold_char(word: str, key: str) -> str:
        text = _key_row(key, word, "gloss")
        for span in text.spans:
            if _GOLD in str(span.style):
                return text.plain[span.start : span.end]
        return ""

    # Words/keys come from the registry, so the rendered gold letter cannot drift.
    resume = commands.by_action("resume")
    close = commands.by_action("close")
    refresh = commands.by_action("refresh_data")
    assert resume.word == "resume"  # not "/resume" (resume is not a slash command)
    assert gold_char(resume.word, resume.key) == "r"
    assert gold_char(close.word, close.key) == "c"  # close shows c in gold (no "(x)" suffix)
    assert "(x)" not in _key_row(close.key, close.word, "g").plain
    assert refresh.word == "Refresh-now"
    assert gold_char(refresh.word, refresh.key) == "R"


def test_td_chord_toggles_done_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`td` hides/shows DONE sessions; `s` alone opens Settings."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store(tmp_path / "command-center" / "state.db")
    store.ensure("idle-1", cwd="/Users/x/repo")
    store.update_fields("idle-1", status="idle")
    store.ensure("done-1", cwd="/Users/x/repo2")
    store.update_fields("done-1", status="done", done=1)
    store.close()

    from command_center.views.tui import CommandCenterApp, SettingsScreen

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._show_finished is False
            assert "done-1" not in app._rows  # finished hidden by default

            await pilot.press("t")
            await pilot.press("d")
            await pilot.pause()
            assert app._show_finished is True
            assert "done-1" in app._rows  # shown after td

            await pilot.press("t")
            await pilot.press("d")
            await pilot.pause()
            assert app._show_finished is False
            assert "done-1" not in app._rows  # hidden again

            # `s` is a chord leader again (sh = subgoal-history); a non-`h` follower
            # falls back to its standalone action, Settings.
            await pilot.press("s")
            await pilot.press("z")  # non-follower → fires the `s` fallback
            await pilot.pause()
            assert isinstance(app.screen, SettingsScreen)

    asyncio.run(scenario())


def test_tf_chord_toggles_future_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`tf` hides/shows FUTURE (draft) jobs, which are shown by default."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft("job-1", "/Users/x/repo", "Migrate tickets", start_when="during holidays")
    store.close()

    from command_center.views.tui import CommandCenterApp

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._show_future is True
            assert "job-1" in app._rows  # future jobs shown by default

            await pilot.press("t")
            await pilot.press("f")
            await pilot.pause()
            assert app._show_future is False
            assert "job-1" not in app._rows  # hidden after tf

            await pilot.press("t")
            await pilot.press("f")
            await pilot.pause()
            assert app._show_future is True
            assert "job-1" in app._rows  # shown again

    asyncio.run(scenario())


def test_ah_chord_shows_aim_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`ah` opens the AIM-history view (full progression); a bare `a` still edits the AIM."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store(tmp_path / "command-center" / "state.db")
    store.ensure("s1", cwd="/Users/x/repo")
    store.set_aim("s1", "first vague aim")
    store.set_aim("s1", "second sharper aim, pytest -q green")  # a real change → history grows
    store.close()

    from command_center.views.tui import CommandCenterApp, InputScreen, TopicScreen

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._current == "s1"  # the only session is selected

            # `ah` chord → the AIM-history modal listing every revision.
            await pilot.press("a")
            await pilot.press("h")
            await pilot.pause()
            assert isinstance(app.screen, TopicScreen)
            rendered = [s.render() for s in app.screen.query_one("#topic").query("Static")]
            body = " ".join(r.plain if hasattr(r, "plain") else str(r) for r in rendered)
            assert "first vague aim" in body
            assert "second sharper aim" in body
            assert "current" in body  # the latest revision is marked
            history_body = next(
                content
                for s in app.screen.query_one("#topic").query("Static")
                if isinstance((content := getattr(s, "content", None)), Text)
                and "first vague" in content.plain
            )
            assert _styled_fragments(history_body, "bold red") == ["35%"]

            await pilot.press("escape")
            await pilot.pause()

            # A bare `a` (leader times out with no `h`) falls back to editing the AIM.
            await pilot.press("a")
            await asyncio.sleep(0.5)  # > the 0.35s chord window
            await pilot.pause()
            assert isinstance(app.screen, InputScreen)

    asyncio.run(scenario())


def test_sh_chord_shows_subgoal_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`sh` opens the sub-goal-history view (versions + drift); a bare `s` still opens Settings."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store(tmp_path / "command-center" / "state.db")
    store.ensure("s1", cwd="/Users/x/repo")
    store.set_aim("s1", "first concrete aim: pytest -q green")
    store.set_subgoals("s1", ["write test_x", "open pr"], source="agent")
    store.set_aim("s1", "second concrete aim: deploy smoke passes")
    store.set_subgoals("s1", ["write test_x", "deploy"], source="agent", merge=True)
    store.close()

    from command_center.views.tui import CommandCenterApp, TopicScreen

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._current == "s1"

            await pilot.press("s")
            await pilot.press("h")
            await pilot.pause()
            assert isinstance(app.screen, TopicScreen)
            body = " ".join(
                _row_text_static(s) for s in app.screen.query_one("#topic").query("Static")
            )
            assert "write test_x" in body and "deploy" in body
            assert "current" in body  # latest version marked

    asyncio.run(scenario())


def test_draft_id_cell_bare_hash() -> None:
    """The draft id cell is always the bare 4-hex hash; no link without a file."""
    from command_center.models import Session
    from command_center.views.tui import _DRAFT_BLUE, _draft_id_cell

    sid = "3a8b7c12-1111-2222-3333-444444444444"
    # start_when is set, but the id cell no longer carries it (it rides the next-step column).
    session = Session(session_id=sid, cwd="/repo", draft=True, start_when="tomorrow evening")

    cell = _draft_id_cell(session)
    assert cell.plain == "  3a8b"
    assert cell.style == _DRAFT_BLUE
    assert cell.spans == []  # no future_file yet → no link span

    # No start_when at all → still just the bare hash.
    bare = _draft_id_cell(Session(session_id=sid, cwd="/repo", draft=True))
    assert bare.plain == "  3a8b"

    # A long start_when never widens the bare-hash id cell.
    long_session = Session(session_id=sid, cwd="/repo", draft=True, start_when="x" * 40)
    assert _draft_id_cell(long_session).plain == "  3a8b"


def test_draft_id_cell_links_hash_when_future_file_set() -> None:
    """Once synced to a future-job file, the 4-hex hash span carries a Rich link style."""
    from command_center import future_files
    from command_center.models import Session
    from command_center.views.tui import _draft_id_cell

    sid = "3a8b7c12-1111-2222-3333-444444444444"
    relpath = "01-llm-tasks/future/home/claude-command-center/3a8b-fix-x.md"
    session = Session(
        session_id=sid,
        cwd="/repo",
        draft=True,
        start_when="tomorrow evening",
        future_file=relpath,
    )

    cell = _draft_id_cell(session)
    assert cell.plain == "  3a8b"
    assert len(cell.spans) == 1
    span = cell.spans[0]
    assert (span.start, span.end) == (2, 6)  # "3a8b" right after the 2-space prefix
    assert getattr(span.style, "link", None) == future_files.obsidian_uri(relpath)


def test_draft_next_cell_tags_and_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A draft's next-step cell renders ``@tags · <start_when note>`` (tags/notes column)."""
    from command_center import tags
    from command_center.models import Session
    from command_center.views.tui import _DRAFT_BLUE, _draft_next_cell

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    tags._load.cache_clear()  # the registry is process-cached; reset for this temp home

    sid = "3a8b7c12-1111-2222-3333-444444444444"

    # Both present: @tag coloured by its type, dot separator grey50, note in draft blue.
    both = Session(
        session_id=sid,
        cwd="/repo",
        draft=True,
        next_step="@home",
        start_when="any time — non-urgent",
    )
    cell = _draft_next_cell(both, "grey50")
    assert cell.plain == "  @home · any time — non-urgent"
    styles = {str(span.style) for span in cell.spans}
    assert tags.tag_style("home") in styles  # @home carries the tags-registry style
    assert _DRAFT_BLUE in styles  # the start_when note is draft blue
    assert "grey50" in styles  # the " · " separator

    # Only start_when → the blue note alone.
    only_when = Session(session_id=sid, cwd="/repo", draft=True, start_when="any time — non-urgent")
    when_cell = _draft_next_cell(only_when, "grey50")
    assert when_cell.plain == "  any time — non-urgent"
    assert when_cell.style == _DRAFT_BLUE

    # Neither → an em dash in the base style.
    none_cell = _draft_next_cell(Session(session_id=sid, cwd="/repo", draft=True), "grey50")
    assert none_cell.plain == "  —"

    # Both present and long → prefix-stripped length capped at 48.
    long_cell = _draft_next_cell(
        Session(
            session_id=sid,
            cwd="/repo",
            draft=True,
            next_step="@home short note",
            start_when="w" * 80,
        ),
        "grey50",
    )
    assert len(long_cell.plain) - 2 <= 48  # minus the 2-space prefix

    # A long next_step with a note is head-truncated too — the cap holds from both sides.
    long_head = _draft_next_cell(
        Session(
            session_id=sid,
            cwd="/repo",
            draft=True,
            next_step="n" * 60,
            start_when="any time",
        ),
        "grey50",
    )
    assert len(long_head.plain) - 2 <= 48


def test_oo_chord_opens_future_file_in_obsidian(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`oo` shells out to macOS `open` on the selected draft's future-job file."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    relpath = "01-llm-tasks/future/home/repo/j-migrate-tickets.md"
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft("job-1", "/Users/x/repo", "Migrate tickets", start_when="during holidays")
    store.update_fields("job-1", future_file=relpath)
    store.close()

    from command_center import future_files
    from command_center.views import tui as tui_mod
    from command_center.views.tui import CommandCenterApp

    calls: list[list[str]] = []

    class FakePopen:  # pylint: disable=too-few-public-methods
        def __init__(self, args: list[str], **_kwargs: object) -> None:
            calls.append(args)

    # NB: this patches the shared `subprocess` module (tui_mod.subprocess IS
    # subprocess), so unrelated Popen calls the app makes on mount (e.g. a
    # detached `ccc copilot-usage` refresh) are captured too — assert
    # membership, not an exact call list.
    monkeypatch.setattr(tui_mod.subprocess, "Popen", FakePopen)

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._current == "job-1"

            await pilot.press("o")
            await pilot.press("o")
            await pilot.pause()

    asyncio.run(scenario())

    assert ["open", future_files.obsidian_uri(relpath)] in calls


def test_oo_chord_notifies_when_draft_has_no_future_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A draft that hasn't synced to a file yet has nothing to open — `oo` just notifies."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft("job-1", "/Users/x/repo", "Migrate tickets", start_when="during holidays")
    store.close()

    from command_center.views.tui import CommandCenterApp

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            notes: list[str] = []
            monkeypatch.setattr(app, "notify", lambda msg, **_kw: notes.append(msg))

            await pilot.press("o")
            await pilot.press("o")
            await pilot.pause()
            assert notes and "No Obsidian file" in notes[-1]

    asyncio.run(scenario())


def test_t_alone_shows_toggle_menu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pressing `t` without a follower toasts the menu of available toggle chords."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store(tmp_path / "command-center" / "state.db")
    store.ensure("s1", cwd="/Users/x/repo")
    store.update_fields("s1", status="idle")
    store.close()

    from command_center.views.tui import CommandCenterApp

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            notes: list[str] = []
            monkeypatch.setattr(app, "notify", lambda msg, **_kw: notes.append(msg))
            await pilot.press("t")  # leader with no follower
            await asyncio.sleep(0.9)  # > the 0.7s pure-leader chord window → timeout fires the menu
            await pilot.pause()
            assert notes, "pressing t alone should toast a menu"
            assert "td" in notes[-1] and "tf" in notes[-1]  # both t… toggles listed
            assert "done" in notes[-1] and "future" in notes[-1]

    asyncio.run(scenario())


def test_aim_column_stretches_so_progress_is_flush_right(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The /aim column soaks up leftover width so the row fills the table and the
    trailing progress column sits flush against the right edge."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store(tmp_path / "command-center" / "state.db")
    sid = "tui-fit-session"
    store.ensure(sid, cwd="/Users/x/repo")
    store.update_fields(sid, aim="x" * 180)  # long aim, but the column crops it
    store.close()

    from command_center.views.tui import _AIM_COL, CommandCenterApp, SessionTable

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            table = app.query_one("#sessions", SessionTable)
            table.fit_aim_column()  # deterministic: run the stretch now
            await pilot.pause()
            cols = list(table.columns.values())
            avail = table.scrollable_content_region.width
            total = sum(c.get_render_width(table) for c in cols)
            assert total == avail  # row spans the full width → progress is flush right
            assert cols[_AIM_COL].width > 38  # /aim got the slack on a wide terminal

    asyncio.run(scenario())


def test_e_opens_inline_edit_form_and_esc_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`e` opens an inline detail-pane form; Esc saves changed fields."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    sid = _seed(tmp_path)

    from textual.widgets import Input, TextArea

    from command_center.views.tui import CommandCenterApp, SessionTable

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#sessions", SessionTable)
            table.move_cursor(row=table.get_row_index(sid))
            table.focus()
            await pilot.pause()
            app.cfg.aim_score_on_set = False
            assert app._current == sid

            await pilot.press("e")
            await pilot.pause()
            assert app._editing is True
            assert app._edit_sid == sid
            app.cfg.drift_check = False  # keep the sub-goal edit from spawning check-drift
            # AIM is a multi-line TextArea (its own box); the rest are inline Inputs.
            assert app.query_one("#edit-aim", TextArea).text == "ship the thing"
            assert app.query_one("#edit-next", Input).value == "- do the next bit"
            # Progress override starts blank (auto); sub-goals show one item per line.
            assert app.query_one("#edit-progress", Input).value == ""
            assert app.query_one("#edit-subgoals", TextArea).text == "a\nb"

            # Tab leaves the AIM box and moves through the inline field lines.
            await pilot.press("tab")
            await pilot.pause()
            assert isinstance(app.focused, Input) and (app.focused.id or "").startswith("edit-")

            app.query_one("#edit-aim", TextArea).text = "ship the better thing"
            app.query_one("#edit-next", Input).value = "call @waiting"
            app.query_one("#edit-deadline", Input).value = "2026-07-01"
            app.query_one("#edit-progress", Input).value = "40%"
            app.query_one("#edit-block", Input).value = "@susi"
            # Delete "b", add "c" — the tick on "a" must carry over (merge by text).
            app.query_one("#edit-subgoals", TextArea).text = "a\nc"

            await pilot.press("escape")
            await pilot.pause()
            assert app._editing is False
            assert app._edit_sid is None

    asyncio.run(scenario())

    store = Store(tmp_path / "command-center" / "state.db")
    saved = store.get(sid)
    subs = store.list_subgoals(sid)
    store.close()
    assert saved is not None
    assert saved.aim == "ship the better thing"
    assert saved.next_step == "call @waiting"
    assert saved.next_step_source == "user"
    assert saved.deadline == "2026-07-01"
    assert saved.manual_progress == 40  # "40%" parsed; overrides the sub-goal bar
    assert saved.blocked_on == "@susi"
    assert [s.text for s in subs] == ["a", "c"]
    assert [s.checked for s in subs] == [True, False]  # a's tick carried over
    assert {s.source for s in subs} == {"user"}  # manual edit → provenance "manual"


def test_e_warns_before_saving_blank_aim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Clearing the AIM and exiting pops a confirm; declining keeps the AIM."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    sid = _seed(tmp_path)

    from textual.widgets import TextArea

    from command_center.views.tui import CommandCenterApp, ConfirmScreen, SessionTable

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.cfg.aim_score_on_set = False
            table = app.query_one("#sessions", SessionTable)
            table.move_cursor(row=table.get_row_index(sid))
            table.focus()
            await pilot.pause()

            await pilot.press("e")
            await pilot.pause()
            app.query_one("#edit-aim", TextArea).text = ""  # blank the AIM
            await pilot.press("escape")
            await pilot.pause()
            # The blank-AIM guard must warn (only in this case).
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("n")  # decline → keep editing, AIM untouched
            await pilot.pause()
            assert app._editing is True

    asyncio.run(scenario())

    store = Store(tmp_path / "command-center" / "state.db")
    saved = store.get(sid)
    store.close()
    assert saved is not None
    assert saved.aim == "ship the thing"  # never blanked


def test_inline_edit_prompt_visibility_and_subgoals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inline edit exposes a draft's prompt (drafts only) and Esc-from-TextArea exits."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    normal_sid = _seed(tmp_path)
    draft_sid = "draft-inline-edit"
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft(draft_sid, "/Users/x/repo", "Prepare draft", prompt="run the thing")
    store.close()

    from textual.widgets import TextArea

    from command_center.views.tui import CommandCenterApp, SessionTable

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.cfg.aim_score_on_set = False
            table = app.query_one("#sessions", SessionTable)
            table.move_cursor(row=table.get_row_index(draft_sid))
            table.focus()
            await pilot.pause()

            await pilot.press("e")
            await pilot.pause()
            # Draft → the prompt line is shown and editable.
            assert app.query_one("#edit-prompt-row").styles.display == "block"
            assert app.query_one("#edit-prompt", TextArea).text == "run the thing"

            prompt = app.query_one("#edit-prompt", TextArea)
            prompt.text = "run the better thing"
            prompt.focus()
            await pilot.press("escape")  # Esc from a focused TextArea must exit edit mode
            await pilot.pause()
            assert app._editing is False

            table = app.query_one("#sessions", SessionTable)
            table.move_cursor(row=table.get_row_index(normal_sid))
            table.focus()
            await pilot.pause()

            await pilot.press("e")
            await pilot.pause()
            assert app.query_one("#edit-prompt-row").styles.display == "none"  # non-draft hides it
            await pilot.press("escape")
            await pilot.pause()

    asyncio.run(scenario())

    store = Store(tmp_path / "command-center" / "state.db")
    draft = store.get(draft_sid)
    store.close()
    assert draft is not None
    assert draft.prompt == "run the better thing"


def test_draft_next_step_cell_shows_models_readout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A draft row's ``model`` cell is the colour-coded overseer/executor pair.

    Equal models compact to a single colour-coded name; a differing pair shows
    ``overseer ▸ executor``. The /next-step cell is the ordinary next-step ("—" here).
    """
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft("job-models", "/Users/x/repo", "Migrate tickets")  # both default fable-5
    mixed = "job-mixed"
    store.create_draft(mixed, "/Users/x/repo", "Ship it")
    store.update_fields(mixed, llm_overseer="opus-4.8", llm_exec="sonnet-5")
    store.close()

    from command_center.views.tui import _MODEL_COL, _NEXT_COL, CommandCenterApp, SessionTable

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#sessions", SessionTable)

            models_row = table.get_row_at(table.get_row_index("job-models"))
            cell = models_row[_MODEL_COL]
            assert isinstance(cell, Text)
            # Equal overseer/executor: single orange (#ff9f43) name, no arrow.
            assert "▸" not in cell.plain
            assert _styled_fragments(cell, "#ff9f43") == ["fable-5"]
            # The pair moved OUT of the /next-step cell (draft has no next-step → "—").
            next_cell = models_row[_NEXT_COL]
            assert isinstance(next_cell, Text)
            assert "▸" not in next_cell.plain

            mixed_cell = table.get_row_at(table.get_row_index(mixed))[_MODEL_COL]
            assert isinstance(mixed_cell, Text)
            assert "opus-4.8 ▸ sonnet-5" in mixed_cell.plain
            assert _styled_fragments(mixed_cell, "#2ecc71") == ["opus-4.8"]  # overseer green
            assert _styled_fragments(mixed_cell, "#5fafff") == ["sonnet-5"]  # executor blue

    asyncio.run(scenario())


def test_inline_edit_models_selects_save_through_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The /overseer & /executor rows are clickable Selects that save via _commit_edit."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    draft_sid = "draft-models-edit"
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft(draft_sid, "/Users/x/repo", "Prepare draft")  # both default fable-5
    store.close()

    from textual.widgets import Select

    from command_center.views.tui import CommandCenterApp, SessionTable

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.cfg.aim_score_on_set = False
            table = app.query_one("#sessions", SessionTable)
            table.move_cursor(row=table.get_row_index(draft_sid))
            table.focus()
            await pilot.pause()

            await pilot.press("e")
            await pilot.pause()
            # Draft → both model rows are shown (non-drafts hide them); Selects seed the value.
            assert app.query_one("#edit-overseer-row").styles.display == "block"
            assert app.query_one("#edit-executor-row").styles.display == "block"
            overseer = app.query_one("#edit-overseer", Select)
            executor = app.query_one("#edit-executor", Select)
            assert overseer.value == "fable-5"

            # Picking new choices from the dropdowns (invalid input is impossible).
            overseer.value = "opus-4.8"
            executor.value = "sonnet-5"
            await pilot.pause()
            app.action_exit_edit()  # aim unchanged/non-empty → commits without a confirm dialog
            await pilot.pause()

    asyncio.run(scenario())

    store = Store(tmp_path / "command-center" / "state.db")
    saved = store.get(draft_sid)
    store.close()
    assert saved is not None
    assert saved.llm_overseer == "opus-4.8"
    assert saved.llm_exec == "sonnet-5"


def test_inline_edit_account_select_saves_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In multi-account mode the draft `/account` Select commits the picked account's config_dir."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    work = tmp_path / "work"
    work.mkdir()
    from command_center import config as _config

    dirs = {"private": tmp_path, "work": work}
    monkeypatch.setattr(_config, "claude_config_dirs", lambda: dict(dirs))

    draft_sid = "draft-account-edit"
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft(
        draft_sid, "/Users/x/repo", "Prepare draft"
    )  # config_dir = default (private)
    store.close()

    from textual.widgets import Select

    from command_center.views.tui import CommandCenterApp, SessionTable

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.cfg.aim_score_on_set = False
            table = app.query_one("#sessions", SessionTable)
            table.move_cursor(row=table.get_row_index(draft_sid))
            table.focus()
            await pilot.pause()

            await pilot.press("e")
            await pilot.pause()
            # Draft + 2 accounts → the account row shows, seeded with the default label.
            assert app.query_one("#edit-account-row").styles.display == "block"
            account = app.query_one("#edit-account", Select)
            assert account.value == "private"
            account.value = "work"
            await pilot.pause()
            app.action_exit_edit()  # aim unchanged/non-empty → commits without a confirm dialog
            await pilot.pause()

    asyncio.run(scenario())

    from command_center import accounts

    store = Store(tmp_path / "command-center" / "state.db")
    saved = store.get(draft_sid)
    store.close()
    assert saved is not None
    assert accounts.same_config_dir(saved.config_dir, str(work))
    # The guard _commit_edit relies on: an unknown label maps to "" → no write.
    assert accounts.account_config_dir("work") == str(work)
    assert accounts.account_config_dir("nosuch") == ""


def test_account_row_hidden_in_single_account_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With one account configured, the draft `/account` row stays hidden (no reflow)."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    draft_sid = "draft-single-account"
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft(draft_sid, "/Users/x/repo", "Prepare draft")
    store.close()

    from command_center.views.tui import CommandCenterApp, SessionTable

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.cfg.aim_score_on_set = False
            table = app.query_one("#sessions", SessionTable)
            table.move_cursor(row=table.get_row_index(draft_sid))
            table.focus()
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            assert app.query_one("#edit-account-row").styles.display == "none"

    asyncio.run(scenario())


def test_click_draft_next_step_cell_opens_model_editor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clicking a DRAFT row's /next-step cell opens the editor with the overseer Select focused."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    draft_sid = "draft-click-models"
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft(draft_sid, "/Users/x/repo", "Prepare draft")
    store.close()

    from textual.widgets import Select

    from command_center.views.tui import CommandCenterApp, SessionTable

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.cfg.aim_score_on_set = False
            table = app.query_one("#sessions", SessionTable)
            table.move_cursor(row=table.get_row_index(draft_sid))
            table.focus()
            await pilot.pause()
            # Simulate the table posting the click message (bypasses pixel hit-testing).
            table.post_message(SessionTable.DraftModelsClicked(draft_sid))
            await pilot.pause()
            assert app._editing is True
            assert app._edit_sid == draft_sid
            assert (getattr(app.focused, "id", "") or "") == "edit-overseer"
            assert isinstance(app.focused, Select)
            app.action_exit_edit()
            await pilot.pause()

    asyncio.run(scenario())


def test_enter_on_row_resumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Enter on a (non-future) session row triggers resume / switch-to-tab — like `r`."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    sid = _seed(tmp_path)

    from command_center.views.tui import CommandCenterApp, SessionTable

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            calls: list[bool] = []
            monkeypatch.setattr(app, "action_resume", lambda: calls.append(True))
            table = app.query_one("#sessions", SessionTable)
            table.move_cursor(row=table.get_row_index(sid))
            table.focus()
            await pilot.pause()
            assert app._current == sid

            await pilot.press("enter")  # whole-row mode → RowSelected → action_resume
            await pilot.pause()
            assert calls == [True]

    asyncio.run(scenario())


def test_draft_id_cell_scheduled_shows_bare_hash() -> None:
    """A dated (SCHEDULED) draft keeps the id cell narrow: bare hash, no ·suffix —
    its compact date spans the importance + ver cells at the row's start instead."""
    from datetime import date

    from command_center.models import Session, short_date_label
    from command_center.views.tui import _draft_id_cell

    sid = "3a8b7c12-1234-5678-9abc-def012345678"
    session = Session(
        session_id=sid,
        cwd="/repo",
        draft=True,
        start_when="return from Slovenia",
        start_date="2026-08-11",
    )
    assert _draft_id_cell(session).plain == "  3a8b"  # start_when suppressed too
    # The compact D.M.YY label that spans the !!! and head: columns; its head slice
    # (first 3 chars) fits the importance column, the tail (≤5) fits the ver column.
    label = short_date_label(date(2026, 8, 11))
    assert label == "11.8.26"
    assert len(label[:3]) <= 3 and len(label[3:]) <= 5
    longest = short_date_label(date(2026, 10, 24))  # widest possible shape: DD.MM.YY
    assert longest == "24.10.26"
    assert len(longest[3:]) <= 5


def test_action_open_session_obsidian_opens_exact_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `os` chord opens the session mirror's obsidian:// URI (and notifies when absent)."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    sid = _seed(tmp_path)
    # A session-mirror file in the isolated vault (conftest points load_config there).
    sdir = tmp_path / "vault" / "01-llm-tasks" / "sessions" / "other" / "repo"
    sdir.mkdir(parents=True)
    mirror = sdir / "ship-the-thing-abcd.md"
    mirror.write_text(
        f'---\nccc_mirror: "session"\nsession_id: "{sid}"\n---\n\nbody\n', encoding="utf-8"
    )

    from command_center.views import tui as tui_mod
    from command_center.views.tui import CommandCenterApp

    opened: list[list[str]] = []

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._current = sid
            monkeypatch.setattr(
                tui_mod.subprocess, "Popen", lambda argv, **_kw: opened.append(list(argv))
            )
            app.action_open_session_obsidian()
            await pilot.pause()
            # Missing mirror → notify, no open.
            mirror.unlink()
            app.action_open_session_obsidian()
            await pilot.pause()

    asyncio.run(scenario())
    # conftest points vault_root at tmp_path/"vault", so the derived vault name is "vault".
    expected = (
        "obsidian://open?vault=vault&file=01-llm-tasks/sessions/other/repo/ship-the-thing-abcd.md"
    )
    assert opened == [["open", expected]]


def test_action_peek_spawns_peek_for_selected_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `sp` chord spawns `ccc peek --session <highlighted-row-id>` (no row → no spawn)."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    sid = _seed(tmp_path)

    from command_center import spawn
    from command_center.views.tui import CommandCenterApp

    spawned: list[list[str]] = []

    def _fake_spawn(args: list[str]) -> bool:
        spawned.append(list(args))
        return True

    monkeypatch.setattr(spawn, "spawn_ccc", _fake_spawn)

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._current = sid
            app.action_peek()
            await pilot.pause()
            # No selected row → nothing spawns.
            app._current = None
            app.action_peek()
            await pilot.pause()

    asyncio.run(scenario())
    # The TUI also spawns copilot-usage on mount; isolate the peek spawns.
    assert [a for a in spawned if a and a[0] == "peek"] == [["peek", "--session", sid]]


def test_card_toggles_flip_persist_and_t4_flips_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`t1`…`t4` flip each usage card's render gate AND persist it; `t4` flips both keys.

    Each toggle is reload-modify-save: a fresh ``load_config`` is flipped and saved, so
    a stale ``self.cfg`` snapshot can never clobber Settings-screen edits.
    """
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    from command_center import config
    from command_center.views.tui import CommandCenterApp

    # `t2` is a no-op without a configured `work` account (there would be nothing to
    # show), so give this machine one. Also exercises the claude_accounts round-trip.
    (tmp_path / "command-center").mkdir(parents=True, exist_ok=True)
    (tmp_path / "command-center" / "config.toml").write_text(
        f'claude_accounts = ["private={tmp_path}/priv", "work={tmp_path}/work"]\n'
        "copilot_usage = true\n",
        encoding="utf-8",
    )

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            # All four cards start shown (defaults).
            assert app.cfg.usage_card_private is True
            assert app.cfg.usage_card_work is True
            assert app.cfg.usage_card_codex is True
            assert app.cfg.usage_card_copilot is True
            assert app.cfg.copilot_usage is True

            for digit, key in (
                ("1", "usage_card_private"),
                ("2", "usage_card_work"),
                ("3", "usage_card_codex"),
            ):
                await pilot.press("t")
                await pilot.press(digit)
                await pilot.pause()
                assert getattr(app.cfg, key) is False  # in-memory flipped
                assert getattr(config.load_config(), key) is False  # persisted to disk

            # t4 flips BOTH the render gate and the network-fetch gate.
            await pilot.press("t")
            await pilot.press("4")
            await pilot.pause()
            assert app.cfg.usage_card_copilot is False
            assert app.cfg.copilot_usage is False
            reloaded = config.load_config()
            assert reloaded.usage_card_copilot is False
            assert reloaded.copilot_usage is False
            # The earlier toggles remain persisted (no stale-snapshot clobber).
            assert reloaded.usage_card_private is False
            assert reloaded.usage_card_work is False
            assert reloaded.usage_card_codex is False

    asyncio.run(scenario())


def test_work_card_hidden_and_t2_inert_without_a_work_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `work` account ⇒ the work card is hidden and `t2` explains instead of no-oping.

    The statusline SKIPS a usage write whose CLAUDE_CONFIG_DIR matches no configured
    account, so a shown-but-empty work card would be a permanently dead box on every
    single-account machine.
    """
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    from command_center import config
    from command_center.views.tui import CommandCenterApp

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._has_work_account() is False  # no claude_accounts configured
            assert app.query_one("#usage-work").display is False  # hidden, not empty
            assert app.cfg.usage_card_work is True  # the flag itself is untouched

            await pilot.press("t")
            await pilot.press("2")
            await pilot.pause()
            # Inert: nothing flipped, nothing persisted.
            assert app.cfg.usage_card_work is True
            assert config.load_config().usage_card_work is True
            assert app.query_one("#usage-work").display is False

    asyncio.run(scenario())


def test_marked_codex_ver_cell_width_over_all_status_icons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A marked (unsatisfied-dep) codex row's ver cell measures <= 5 cells for EVERY icon.

    The 2-cell status icons (||, 😴, 💤) drop the OAI badge so no column ever widens.
    """
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    from command_center.core import Row
    from command_center.models import Session, Status
    from command_center.views.tui import CommandCenterApp, SessionTable

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.update_detail = lambda: None  # type: ignore[method-assign]  # skip the highlight→detail side-effect
            table = app.query_one("#sessions", SessionTable)
            for status in Status:
                table.clear()
                sid = f"marked-{status.value}"
                session = Session(session_id=sid, cwd="/repo", aim="x")
                row = Row(session, None, status, 0, 0, uses_codex_workflow=True, dep_state="unmet")
                app._add_session_row(table, row)
                cells = table.get_row_at(table.get_row_index(sid))
                icon, imp, ver = cells[0], cells[1], cells[2]
                assert isinstance(ver, Text)
                assert ver.cell_len <= 5, f"{status}: {ver.plain!r} = {ver.cell_len} cells"
                if status is Status.DONE:
                    # A done row never wears the marker — it no longer waits on anything.
                    assert icon.plain != "|"
                    continue
                # The |--> marker starts at column 0 (| + -- + >...).
                assert icon.plain == "|"
                assert imp.plain == "--"
                assert ver.plain.startswith(">")

    asyncio.run(scenario())


def test_edit_depends_row_visible_and_commit_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The /depends-on row shows for a non-draft session; a picked value commits + clears."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    parent_id = "11111111-1111-1111-1111-111111111111"
    store = Store(tmp_path / "command-center" / "state.db")
    store.ensure(parent_id, cwd="/Users/x/repo")
    store.update_fields(parent_id, aim="parent job", status="parked")
    store.ensure("edited", cwd="/Users/x/repo")  # a NON-draft session
    store.update_fields("edited", aim="child job", status="parked")
    store.close()

    from command_center.views.tui import CommandCenterApp, SessionTable

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.cfg.aim_score_on_set = False
            table = app.query_one("#sessions", SessionTable)
            table.move_cursor(row=table.get_row_index("edited"))
            table.focus()
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            # Visible for a non-draft session (not draft-gated).
            assert app.query_one("#edit-depends-row").styles.display == "block"
            # Simulate the picker selecting the parent, then commit via exit.
            app._edit_depends_pending = parent_id
            app.action_exit_edit()
            await pilot.pause()
            assert app.store is not None
            assert app.store.get("edited").depends_on == parent_id  # type: ignore[union-attr]
            # Re-edit and clear it back to none.
            app._current = "edited"
            table.move_cursor(row=table.get_row_index("edited"))
            await pilot.press("e")
            await pilot.pause()
            app._edit_depends_pending = ""
            app.action_exit_edit()
            await pilot.pause()
            assert app.store.get("edited").depends_on is None  # type: ignore[union-attr]

    asyncio.run(scenario())


def test_hoisted_draft_renders_without_future_separator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A draft hoisted under an active parent renders adjacent to it — no FUTURE separator."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store(tmp_path / "command-center" / "state.db")
    store.ensure("parent", cwd=f"{_BASE}/infra/r")
    store.update_fields("parent", status="parked", aim="the parent")
    store.create_draft("child", f"{_BASE}/infra/r", "needs parent", depends_on="parent")
    store.close()

    from command_center.views.tui import CommandCenterApp, SessionTable

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#sessions", SessionTable)
            # Directly under the parent: no separator row was inserted between them.
            assert table.get_row_index("child") == table.get_row_index("parent") + 1

    asyncio.run(scenario())
