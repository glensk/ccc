"""Tests for the pure tmux-window locator :func:`terminal._tmux_pane_for_session`.

Only the matcher is unit-tested — the surrounding ``focus_tmux_window`` shells out to
``tmux``/``ps``/osascript and is environment-bound. Each case feeds a synthetic process
tree (mirroring ``ps -axo pid=,ppid=,command=``) plus a ``list-panes`` row set and asserts
which window target (if any) the walk resolves.
"""

from __future__ import annotations

from command_center import terminal


def _build_tree(
    procs: list[tuple[int, int, str]],
) -> tuple[dict[int, list[tuple[int, str]]], dict[int, str]]:
    """Turn ``(pid, ppid, command)`` rows into the (children, commands) maps the matcher takes.

    ``children`` maps ``ppid -> [(pid, command), ...]`` (traversal edges); ``commands`` maps
    ``pid -> command`` (each process's own argv, so the pane_pid's exec'd claude is visible).
    """
    children: dict[int, list[tuple[int, str]]] = {}
    commands: dict[int, str] = {}
    for pid, ppid, command in procs:
        commands[pid] = command
        children.setdefault(ppid, []).append((pid, command))
    return children, commands


def test_match_via_two_level_descendant_chain() -> None:
    children, commands = _build_tree(
        [
            (100, 1, "-zsh"),
            (200, 100, "zsh"),
            (300, 200, "claude --model x --session-id abc-123 prompt"),
        ]
    )
    panes = [("ccc:1", 100, "%0")]
    assert terminal._tmux_pane_for_session(panes, children, commands, "abc-123") == "ccc:1"


def test_match_on_pane_pid_own_command_no_children() -> None:
    # `ccc start-job` execs claude in place, so the claude argv is the pane_pid ITSELF and
    # there are no descendants to walk — the matcher must still resolve the window.
    children, commands = _build_tree([(75005, 1, "claude --model x --session-id 459c2ef3 prompt")])
    panes = [("ccc:2", 75005, "%3")]
    assert terminal._tmux_pane_for_session(panes, children, commands, "459c2ef3") == "ccc:2"


def test_no_match_returns_none() -> None:
    children, commands = _build_tree([(100, 1, "zsh"), (200, 100, "vim notes.md")])
    panes = [("ccc:1", 100, "%0")]
    assert terminal._tmux_pane_for_session(panes, children, commands, "abc-123") is None


def test_match_is_token_bounded_not_prefix() -> None:
    # "abc-123" must NOT match a longer id "abc-1234" sharing the prefix.
    children, commands = _build_tree(
        [(100, 1, "zsh"), (300, 100, "claude --session-id abc-1234 prompt")]
    )
    panes = [("ccc:1", 100, "%0")]
    assert terminal._tmux_pane_for_session(panes, children, commands, "abc-123") is None


def test_cycle_in_children_terminates_and_returns_none() -> None:
    # A self-referential ps snapshot (pid 100 -> 100) must not loop forever.
    children = {100: [(100, "sh")]}
    commands = {100: "sh"}
    panes = [("ccc:1", 100, "%0")]
    assert terminal._tmux_pane_for_session(panes, children, commands, "abc-123") is None
