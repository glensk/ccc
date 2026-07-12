"""Unit tests for ``ccc jump``: tty discovery, jumpstate, and the toggle logic.

No iTerm / AppleScript / lsappinfo is invoked — every OS boundary is monkeypatched.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from command_center import jump, jumpstate

# A realistic `ps -axo tty=,args=` dump: the bare TUI on a tty, the daemon and a
# one-shot subcommand on no tty (??), plus unrelated processes and a `ccc tui`.
_PS = """\
??       /opt/homebrew/.../Python /home/user/.local/bin/ccc daemon
??       /opt/homebrew/.../Python /home/user/.local/bin/ccc aim --session abc --format bar
ttys001  /opt/homebrew/.../Python /home/user/.local/bin/ccc
ttys009  -zsh
ttys016  /opt/homebrew/.../node /some/other/thing
"""


def _patch_ps(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(jump.subprocess, "run", fake_run)


def test_finds_bare_ccc_tui(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bare ``ccc`` process attached to a tty is the TUI."""
    _patch_ps(monkeypatch, _PS)
    assert jump.find_ccc_tty() == "/dev/ttys001"


def test_finds_ccc_tui_explicit_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ccc tui`` (explicit subcommand) on a tty also counts."""
    _patch_ps(monkeypatch, "ttys004  /opt/homebrew/.../Python /home/user/.local/bin/ccc tui\n")
    assert jump.find_ccc_tty() == "/dev/ttys004"


def test_ignores_daemon_and_subcommands(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ccc daemon`` / ``ccc aim …`` (no tty, extra args) are never matched."""
    no_tui = "\n".join(line for line in _PS.splitlines() if not line.startswith("ttys001")) + "\n"
    _patch_ps(monkeypatch, no_tui)
    assert jump.find_ccc_tty() is None


def test_no_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ps(monkeypatch, "")
    assert jump.find_ccc_tty() is None


def test_ps_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("ps not found")

    monkeypatch.setattr(jump.subprocess, "run", boom)
    assert jump.find_ccc_tty() is None


# --------------------------------------------------------------------------- #
# jumpstate — cross-process selected/request files
# --------------------------------------------------------------------------- #
@pytest.fixture
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(jumpstate.config, "app_home", lambda: tmp_path)
    return tmp_path


def test_selected_roundtrip(_home: Path) -> None:
    assert jumpstate.get_selected() is None
    jumpstate.set_selected("abc123")
    assert jumpstate.get_selected() == "abc123"
    jumpstate.set_selected(None)  # clearing removes the file
    assert jumpstate.get_selected() is None


def test_request_roundtrip(_home: Path) -> None:
    assert jumpstate.peek_request() is None
    jumpstate.request_select("sess-1")
    assert jumpstate.peek_request() == "sess-1"  # peek does NOT consume
    assert jumpstate.peek_request() == "sess-1"
    jumpstate.clear_request()
    assert jumpstate.peek_request() is None


# --------------------------------------------------------------------------- #
# run() — the context-aware toggle
# --------------------------------------------------------------------------- #
def _args(**kw: object) -> argparse.Namespace:
    return argparse.Namespace(**{"no_toggle": False, "no_launch": False, **kw})


def _wire(monkeypatch: pytest.MonkeyPatch, *, frontmost: bool, current: object) -> dict:
    """Stub every OS boundary `run()` touches; return a dict recording calls."""
    calls: dict = {"request": None, "focus_tty": None, "resume": 0}
    monkeypatch.setattr(jump, "find_ccc_tty", lambda: "/dev/ttys001")
    monkeypatch.setattr(jump.terminal, "is_iterm_frontmost", lambda: frontmost)
    monkeypatch.setattr(jump.terminal, "current_iterm_session", lambda: current)
    monkeypatch.setattr(
        jump.jumpstate, "request_select", lambda sid: calls.__setitem__("request", sid)
    )

    def _focus_tty(tty: str) -> bool:
        calls["focus_tty"] = tty
        return True

    monkeypatch.setattr(jump.terminal, "focus_tty", _focus_tty)

    def _resume() -> int:
        calls["resume"] += 1
        return 0

    monkeypatch.setattr(jump, "_resume_selected", _resume)
    return calls


def test_run_from_other_app_focuses_ccc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not in iTerm → no toggle: just bring ccc forward, no select request."""
    calls = _wire(monkeypatch, frontmost=False, current=None)
    assert jump.run(_args()) == 0
    assert calls["focus_tty"] == "/dev/ttys001"
    assert calls["request"] is None
    assert calls["resume"] == 0


def test_run_in_ccc_tab_resumes_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    """In the ccc tab (current tty == ccc tty) → act like `r`; do not re-focus ccc."""
    calls = _wire(monkeypatch, frontmost=True, current=("UUID-X", "/dev/ttys001"))
    assert jump.run(_args()) == 0
    assert calls["resume"] == 1
    assert calls["focus_tty"] is None


def test_run_in_session_tab_requests_then_focuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """In a session tab → request its row be selected, then focus ccc."""
    calls = _wire(monkeypatch, frontmost=True, current=("UUID-S", "/dev/ttys042"))
    monkeypatch.setattr(
        jump, "_session_for_uuid", lambda uuid: "sess-S" if uuid == "UUID-S" else None
    )
    assert jump.run(_args()) == 0
    assert calls["request"] == "sess-S"
    assert calls["focus_tty"] == "/dev/ttys001"
    assert calls["resume"] == 0


def test_no_toggle_skips_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-toggle → always just focus ccc, even in the ccc tab."""
    calls = _wire(monkeypatch, frontmost=True, current=("UUID-X", "/dev/ttys001"))
    assert jump.run(_args(no_toggle=True)) == 0
    assert calls["resume"] == 0
    assert calls["focus_tty"] == "/dev/ttys001"
