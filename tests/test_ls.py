"""Render tests for the flat ``ccc ls`` view — AIM color keyed off the vagueness score."""

from __future__ import annotations

import time
from pathlib import Path

from command_center import accounts
from command_center.core import Row
from command_center.models import Session, Status
from command_center.views import ls as ls_view

_RED = "\x1b[38;5;196m"  # _SEVERITY_COLOR["red"] painted by _paint()
_OAI = "\x1b[1;38;5;16;48;5;15mOAI\x1b[0m"


def _row(aim: str, score: int) -> Row:
    session = Session(session_id="s1", cwd="/repo", aim=aim, aim_score=score)
    return Row(session, None, Status.PARKED, 0, 0)


def test_render_row_paints_only_low_aim_score_red() -> None:
    lines = ls_view._render_row(
        _row("improve things", 20), enabled=True, warn_days=2, aim_threshold=50
    )
    assert f"{_RED}20%\x1b[0m improve things" in lines[0]
    assert f"{_RED}improve things\x1b[0m" not in lines[0]


def test_render_row_specific_aim_not_red() -> None:
    lines = ls_view._render_row(
        _row("all tests pass", 85), enabled=True, warn_days=2, aim_threshold=50
    )
    assert "all tests pass" in lines[0]
    assert f"{_RED}all tests pass" not in lines[0]


def test_render_row_unscored_aim_not_red() -> None:
    lines = ls_view._render_row(_row("some aim", -1), enabled=True, warn_days=2, aim_threshold=50)
    assert f"{_RED}-1" not in lines[0]
    assert f"{_RED}some aim" not in lines[0]


_DEP = "abcd1234-1234-5678-9abc-def012345678"


def test_render_row_home_icon_marks_private_account() -> None:
    """Multi-account: a private-account row carries 🏠, a work-account row carries 💼."""
    dirs = {"private": Path("/home/u/.claude"), "work": Path("/home/u/.claude-work")}
    priv = Session(session_id="p", cwd="/repo", aim="x", config_dir="/home/u/.claude")
    work = Session(session_id="w", cwd="/repo", aim="x", config_dir="/home/u/.claude-work")
    line_priv = ls_view._render_row(Row(priv, None, Status.PARKED, 0, 0), True, 2, 50, dirs)[0]
    line_work = ls_view._render_row(Row(work, None, Status.PARKED, 0, 0), True, 2, 50, dirs)[0]
    assert accounts._HOME_GLYPH in line_priv and "💼" not in line_priv
    assert accounts._WORK_GLYPH in line_work and "🏠" not in line_work


def test_render_row_no_home_icon_in_single_account() -> None:
    """Single account: no marker at all (it would sit on every row and mean nothing)."""
    dirs = {"private": Path("/home/u/.claude")}
    priv = Session(session_id="p", cwd="/repo", aim="x", config_dir="/home/u/.claude")
    line = ls_view._render_row(Row(priv, None, Status.PARKED, 0, 0), True, 2, 50, dirs)[0]
    assert "🏠" not in line


def test_render_row_hoisted_marker_prefix() -> None:
    # A hoisted dependent (dep_depth > 0) leads line1 with the red |--> marker.
    session = Session(session_id="child", cwd="/repo", aim="x", depends_on=_DEP)
    row = Row(session, None, Status.PARKED, 0, 0)
    row.dep_depth = 1
    row.dep_state = "unmet"
    lines = ls_view._render_row(row, enabled=True, warn_days=2, aim_threshold=50)
    assert lines[0].startswith(f"{_RED}|--> \x1b[0m")


def test_render_row_depends_extras_states() -> None:
    # Any row with a dependency notes it on the ↳ line, with the state suffix.
    cases = [
        ("unmet", ""),
        ("satisfied", " (done)"),
        ("missing", " (missing)"),
        ("cancelled", " (cancelled)"),
    ]
    for state, suffix in cases:
        session = Session(session_id="s1", cwd="/repo", aim="x", depends_on=_DEP)
        row = Row(session, None, Status.PARKED, 0, 0)
        row.dep_state = state
        line2 = ls_view._render_row(row, enabled=False, warn_days=2, aim_threshold=50)[1]
        assert f"depends: abcd{suffix}" in line2


def test_render_row_no_depends_no_extra() -> None:
    session = Session(session_id="s1", cwd="/repo", aim="x")
    row = Row(session, None, Status.PARKED, 0, 0)
    line2 = ls_view._render_row(row, enabled=False, warn_days=2, aim_threshold=50)[1]
    assert "depends:" not in line2


def test_render_row_halted_shows_red_double_bar() -> None:
    session = Session(session_id="s1", cwd="/repo", aim="x")
    row = Row(session, None, Status.HALTED, 0, 0)
    lines = ls_view._render_row(row, enabled=True, warn_days=2, aim_threshold=50)
    assert f"{_RED}||\x1b[0m" in lines[0]  # red "||" rate-limit icon leads the row


def test_render_row_shows_per_repo_badge() -> None:
    """The deterministic per-repo badge appears before the folder cell (matches the TUI)."""
    from command_center import tabsymbol

    session = Session(session_id="s1", cwd="/repo", aim="x")
    row = Row(session, None, Status.PARKED, 0, 0)
    line = ls_view._render_row(row, enabled=False, warn_days=2, aim_threshold=50)[0]
    badge = tabsymbol.symbol_for_repo("/repo")
    assert f"{badge} " in line  # the emoji cell is rendered


def test_render_row_shows_claude_version() -> None:
    session = Session(session_id="s1", cwd="/repo", aim="x", version="2.1.193")
    line = ls_view._render_row(
        Row(session, None, Status.IDLE, 0, 0), enabled=False, warn_days=2, aim_threshold=50
    )[0]
    assert "193" in line  # patch part shown
    assert "2.1.193" not in line  # full version is not


def test_render_row_codex_workflow_badge_replaces_version() -> None:
    session = Session(session_id="s1", cwd="/repo", aim="x", version="2.1.193")
    row = Row(session, None, Status.IDLE, 0, 0, uses_codex_workflow=True)

    plain = ls_view._render_row(row, enabled=False, warn_days=2, aim_threshold=50)[0]
    assert "OAI" in plain
    assert "193" not in plain
    ansi = ls_view._render_row(row, enabled=True, warn_days=2, aim_threshold=50)[0]
    assert _OAI in ansi


def test_render_row_waiting_codex_shows_sleeping_face_and_reset_hint() -> None:
    session = Session(session_id="s1", cwd="/repo", aim="x")
    row = Row(
        session,
        None,
        Status.WAITING_CODEX,
        0,
        0,
        uses_codex_workflow=True,
        codex_reset_label="5h",
        codex_reset_at=int(time.time()) + 3600,
    )
    lines = ls_view._render_row(row, enabled=False, warn_days=2, aim_threshold=50)
    assert lines[0].startswith("😴")
    assert "waiting for Codex 5h reset" in lines[1]


def test_render_row_draft_shows_hash_linked_when_future_file_set() -> None:
    from command_center.future_files import display_hash, obsidian_uri
    from command_center.links import osc8_link

    sid = "3a8b7c12-1111-2222-3333-444444444444"
    session = Session(session_id=sid, cwd="/repo", aim="x", draft=True)
    row = Row(session, None, Status.PARKED, 0, 0)

    # No future_file yet: bare hash, no obsidian:// hyperlink (the folder cell may
    # still carry its own unrelated openterm:// OSC 8 link, hence the narrow check).
    line = ls_view._render_row(row, enabled=False, warn_days=2, aim_threshold=50)[0]
    assert display_hash(sid) in line
    assert "obsidian://" not in line

    # Synced to a future-job file: the hash is wrapped in an OSC 8 link to it.
    session.future_file = "01-llm-tasks/future/home/claude-command-center/3a8b-fix.md"
    linked_line = ls_view._render_row(row, enabled=False, warn_days=2, aim_threshold=50)[0]
    expected = osc8_link(obsidian_uri(session.future_file), display_hash(sid))
    assert expected in linked_line


def test_render_row_draft_shows_start_when_note() -> None:
    """A draft's free-text start_when note surfaces as a ``when:`` line-2 entry."""
    sid = "3a8b7c12-1111-2222-3333-444444444444"
    session = Session(
        session_id=sid, cwd="/repo", aim="x", draft=True, start_when="during holidays"
    )
    row = Row(session, None, Status.PARKED, 0, 0)
    line2 = ls_view._render_row(row, enabled=False, warn_days=2, aim_threshold=50)[1]
    assert "when: during holidays" in line2

    # No start_when → no when: entry.
    plain = Session(session_id=sid, cwd="/repo", aim="x", draft=True)
    plain_line2 = ls_view._render_row(
        Row(plain, None, Status.PARKED, 0, 0), enabled=False, warn_days=2, aim_threshold=50
    )[1]
    assert "when:" not in plain_line2


def test_render_row_draft_shows_models_readout() -> None:
    """A draft's model column (line 1) carries the configured pair; line 2 no longer does."""
    sid = "3a8b7c12-1111-2222-3333-444444444444"
    session = Session(session_id=sid, cwd="/repo", aim="x", draft=True)
    row = Row(session, None, Status.PARKED, 0, 0)
    lines = ls_view._render_row(row, enabled=False, warn_days=2, aim_threshold=50)
    # Equal overseer/executor (the fable-5 default) compacts to a single name in the model slot.
    assert "fable-5" in lines[0]
    assert "▸" not in lines[0]  # no redundant "fable-5 ▸ fable-5"
    assert "fable-5" not in lines[1]  # the pair moved off the secondary line

    session.llm_overseer = "fable-5"
    session.llm_exec = "sonnet-5"
    mixed = ls_view._render_row(row, enabled=False, warn_days=2, aim_threshold=50)
    assert "fable-5 ▸ sonnet-5" in mixed[0]  # differing pair shown in full, in the model column
    assert "▸" not in mixed[1]


def test_render_row_shows_blue_drift_dot() -> None:
    flagged = Session(session_id="s1", cwd="/repo", aim="x", drift_severity="high", drift_at=1)
    line = ls_view._render_row(
        Row(flagged, None, Status.IDLE, 0, 0), enabled=True, warn_days=2, aim_threshold=50
    )[0]
    assert "\x1b[38;5;39m ●\x1b[0m" in line  # blue unresolved-drift dot

    acked = Session(
        session_id="s2", cwd="/r", aim="x", drift_severity="high", drift_at=1, drift_ack_at=2
    )
    acked_line = ls_view._render_row(
        Row(acked, None, Status.IDLE, 0, 0), enabled=True, warn_days=2, aim_threshold=50
    )[0]
    assert "\x1b[38;5;39m ●\x1b[0m" not in acked_line  # acknowledged -> no blue drift dot
    # (a bare ● now also appears as the green idle icon, so match the blue drift escape precisely)


def test_render_row_shows_red_done_when_aim_met() -> None:
    session = Session(session_id="s1", cwd="/repo", aim="ship it", aim_score=80, aim_met=True)
    line = ls_view._render_row(
        Row(session, None, Status.IDLE, 3, 4), enabled=True, warn_days=2, aim_threshold=50
    )[0]
    # 3/4 → the bar under DONE is all filled: every letter is red on the SAME palette entry
    # (48;5;84) the solid █ glyphs use as foreground — letter cells and bar cells render
    # pixel-identically, no seam.
    on_fill = "".join(f"\x1b[1;38;5;196;48;5;84m{ch}\x1b[0m" for ch in "DONE")
    assert on_fill in line
    assert "75%" in line  # the exact sub-goal progress is still shown alongside
    assert "█" in line and "▓" not in line  # DONE bar fill is solid, not the dotted shade

    # 2/4 → fill 5 of 10 cells: "DO" sits on filled cells (bg = the fill palette entry),
    # "NE" on empty cells (25 % tint of 84 → rgb 24,64,34 — the ░ track's average).
    half = ls_view._render_row(
        Row(session, None, Status.IDLE, 2, 4), enabled=True, warn_days=2, aim_threshold=50
    )[0]
    mixed = "".join(f"\x1b[1;38;5;196;48;5;84m{ch}\x1b[0m" for ch in "DO") + "".join(
        f"\x1b[1;38;5;196;48;2;24;64;34m{ch}\x1b[0m" for ch in "NE"
    )
    assert mixed in half


def test_render_row_no_done_when_not_met() -> None:
    session = Session(session_id="s1", cwd="/repo", aim="ship it", aim_score=80, aim_met=False)
    line = ls_view._render_row(
        Row(session, None, Status.IDLE, 3, 4), enabled=True, warn_days=2, aim_threshold=50
    )[0]
    assert "DONE" not in line


def test_render_row_no_done_for_human_done_row() -> None:
    # A human-done row (✓ / FINISHED) never also shows the soft red DONE overlay.
    session = Session(session_id="s1", cwd="/repo", aim="ship it", aim_score=80, aim_met=True)
    line = ls_view._render_row(
        Row(session, None, Status.DONE, 4, 4), enabled=True, warn_days=2, aim_threshold=50
    )[0]
    assert "DONE" not in line
