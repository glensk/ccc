"""Tests for the shell-predicate runner (``run_exit0``)."""

from __future__ import annotations

from pathlib import Path

from command_center.checks import run_exit0


def test_run_exit0_true() -> None:
    assert run_exit0("exit 0") is True
    assert run_exit0("true") is True


def test_run_exit0_false() -> None:
    assert run_exit0("exit 1") is False
    assert run_exit0("false") is False


def test_run_exit0_bad_command() -> None:
    assert run_exit0("this-command-does-not-exist-xyzzy") is False


def test_run_exit0_timeout() -> None:
    assert run_exit0("sleep 5", timeout=1) is False  # killed at the timeout -> not satisfied


def test_run_exit0_cwd(tmp_path: Path) -> None:
    (tmp_path / "marker").write_text("x", encoding="utf-8")
    assert run_exit0("test -f marker", cwd=str(tmp_path)) is True
    assert run_exit0("test -f nope", cwd=str(tmp_path)) is False
