"""`claude-session-continue` — the ported reset-wait / resume engine.

Exercises the CLI surface ccc's auto-resume relies on (positional id + ``now``,
``-w/--wait-only`` + ``--signal-file``, the claude-missing exit code) and the pure
time / limit-message parsers. Never invokes a real ``claude``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from command_center import session_continue as sc


# ------------------------------ CLI surface / arg parsing ------------------------------ #
def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        sc.parse_args(["--help"])
    assert exc.value.code == 0


def test_positional_id_and_time() -> None:
    args = sc.parse_args(["0199-uuid", "now"])
    assert args.session_id == "0199-uuid"
    assert args.time == "now"
    assert not args.wait_only


def test_wait_only_signal_file_contract() -> None:
    # The exact form resume.py spawns: `<script> auto --wait-only --signal-file <f>`.
    args = sc.parse_args(["auto", "--wait-only", "--signal-file", "/tmp/sig"])
    assert args.session_id == "auto"
    assert args.wait_only is True
    assert args.signal_file == "/tmp/sig"


def test_short_flags_present() -> None:
    args = sc.parse_args(["id", "-w", "-s", "/tmp/x", "-n", "-d"])
    assert args.wait_only and args.signal_file == "/tmp/x"
    assert args.no_prompt and args.dry_run


# ------------------------------ build_command ------------------------------ #
def test_build_command_resume_with_defaults() -> None:
    args = sc.parse_args(["myid"])
    assert sc.build_command(args, "myid") == [
        "claude",
        "--resume",
        "myid",
        "--dangerously-skip-permissions",
        sc.DEFAULT_PROMPT,
    ]


def test_build_command_no_prompt_no_skip() -> None:
    args = sc.parse_args(["myid", "--no-prompt", "--no-skip-permissions"])
    assert sc.build_command(args, "myid") == ["claude", "--resume", "myid"]


def test_build_command_last_uses_continue() -> None:
    args = sc.parse_args(["last"])
    assert sc.build_command(args, "last")[:2] == ["claude", "--continue"]


# ------------------------------ run_wait_only ------------------------------ #
def test_wait_only_now_writes_signal(tmp_path: Path) -> None:
    sig = tmp_path / "reset.signal"
    args = sc.parse_args(["now", "--wait-only", "--signal-file", str(sig)])
    assert sc.run_wait_only(args) == 0
    assert sig.exists() and sig.read_text().strip()  # a timestamp was written


def test_wait_only_dry_run_writes_nothing(tmp_path: Path) -> None:
    sig = tmp_path / "reset.signal"
    args = sc.parse_args(["now", "--wait-only", "--signal-file", str(sig), "--dry-run"])
    assert sc.run_wait_only(args) == 0
    assert not sig.exists()


# ------------------------------ main guardrails (no real claude) ------------------------------ #
def test_main_without_claude_returns_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sc.shutil, "which", lambda name: None)
    assert sc.main(["myid", "now"]) == 1


# ------------------------------ CLAUDE_HOME override ------------------------------ #
def test_claude_home_honours_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "custom"))
    assert sc._claude_home() == str(tmp_path / "custom")
    path = sc.session_file_path("abc")
    assert str(tmp_path / "custom") in path and path.endswith("abc.jsonl")


# ------------------------------ pure parsers ------------------------------ #
def test_parse_clock_time_variants() -> None:
    assert sc.parse_clock_time("1:10am") == (1, 10)
    assert sc.parse_clock_time("13:45") == (13, 45)
    assert sc.parse_clock_time("1am") == (1, 0)
    assert sc.parse_clock_time("nonsense") is None


def test_compute_start_rolls_to_tomorrow() -> None:
    now = datetime(2026, 7, 8, 12, 0, 0)
    # A time already past today lands tomorrow (+1 min margin).
    target = sc.compute_start(now, 1, 10)
    assert target.day == 9 and (target.hour, target.minute) == (1, 11)


def test_parse_limit_message_epoch_and_relative() -> None:
    now = datetime(2026, 7, 8, 12, 0, 0)
    epoch = sc.parse_limit_message("Claude AI usage limit reached|1749600600", now)
    assert epoch is not None
    rel = sc.parse_limit_message("resets in 2h 7m", now)
    assert rel is not None and rel.hour == 14 and rel.minute == 8  # +2h07m +1m margin
