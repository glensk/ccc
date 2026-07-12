"""Tests for ``ccc demo`` — the self-contained fake-data command center."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from command_center import demo
from command_center.store import Store


def test_seed_creates_expected_rows(tmp_path: Path) -> None:
    """Seeding a demo home populates sessions, AIMs, sub-goals, drafts and live registry."""
    home = tmp_path / "demo"
    ids = demo.seed(home)

    # ~10 sessions across three categories, plus the two drafts.
    assert len(ids) >= 10

    with Store(home / "command-center" / "state.db") as store:
        sessions = store.list_sessions()
        by_id = {s.session_id: s for s in sessions}
        assert len(sessions) == len(ids)

        # Every non-draft session carries an AIM (recorded in history) and a score.
        for session in sessions:
            assert session.aim
            assert session.aim_score > 0

        # A working session has a sub-goal checklist that is partly (not fully) done.
        api = by_id[demo.sid_for("api-gateway")]
        checked, total = store.progress(api.session_id)
        assert 0 < checked < total

        # The done session reads 100% and carries the done flag.
        recipe = by_id[demo.sid_for("recipe-site")]
        assert recipe.done
        done_checked, done_total = store.progress(recipe.session_id)
        assert done_checked == done_total > 0

        # The aim-met session has the red-DONE verdict but is NOT human-done.
        textual = by_id[demo.sid_for("textual-ui")]
        assert textual.aim_met and not textual.done

        # The drift session carries a flagged (unacknowledged) drift verdict.
        cli_tool = by_id[demo.sid_for("cli-tool")]
        assert cli_tool.drift_severity in ("low", "medium", "high")

        # A manual progress override is set on one home session.
        backup = by_id[demo.sid_for("backup-script")]
        assert backup.manual_progress == 80

        # A FUTURE draft (codex) and a SCHEDULED draft (fixed start date) both exist.
        future = by_id[demo.sid_for("draft/new-linter")]
        assert future.draft and future.job_type == "codex" and not future.start_date
        scheduled = by_id[demo.sid_for("draft/q3-review")]
        assert scheduled.draft and scheduled.start_date

    # The live registry holds the three "live" sessions (working / waiting / halted).
    live_files = list((home / "sessions").glob("*.json"))
    assert len(live_files) == 3


def test_config_points_inside_home(tmp_path: Path) -> None:
    """The generated config keeps repo tree + vault roots inside the demo home."""
    home = tmp_path / "demo"
    demo.seed(home)
    text = (home / "command-center" / "config.toml").read_text(encoding="utf-8")
    assert str(home) in text
    assert 'folder_order = ["work", "home", "oss"]' in text


def test_demo_ls_renders_without_touching_real_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ccc demo --ls` renders (exit 0) and never writes into a stand-in real CLAUDE_HOME."""
    real_home = tmp_path / "real-claude-home"
    real_home.mkdir()
    (real_home / "settings.json").write_text('{"sentinel": true}', encoding="utf-8")
    before = {p: p.read_bytes() for p in real_home.rglob("*") if p.is_file()}
    monkeypatch.setenv("CLAUDE_HOME", str(real_home))

    demo_home = tmp_path / "demo"

    class _Args:
        ls = True
        dir = str(demo_home)
        clean = False

    rc = demo.run(_Args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "rate-limit middleware" in out  # a seeded short-AIM label rendered

    # The stand-in real home was created but demo redirected CLAUDE_HOME, so it is untouched.
    after = {p: p.read_bytes() for p in real_home.rglob("*") if p.is_file()}
    assert after == before
    assert not (real_home / "command-center").exists()


def test_seed_is_deterministic(tmp_path: Path) -> None:
    """Two seeds of separate homes mint identical session ids and store rows."""
    ids_a = demo.seed(tmp_path / "a")
    ids_b = demo.seed(tmp_path / "b")
    assert ids_a == ids_b

    def snapshot(home: Path) -> list[tuple[str, str | None, int]]:
        with Store(home / "command-center" / "state.db") as store:
            return sorted((s.session_id, s.aim, s.aim_score) for s in store.list_sessions())

    assert snapshot(tmp_path / "a") == snapshot(tmp_path / "b")


def test_live_registry_marks_sessions_live(tmp_path: Path) -> None:
    """The fake live registry uses this (alive) process pid, so the halt marker is on disk."""
    import os

    home = tmp_path / "demo"
    demo.seed(home)
    files = list((home / "sessions").glob("*.json"))
    pids = {json.loads(p.read_text(encoding="utf-8"))["pid"] for p in files}
    assert pids == {os.getpid()}

    # The halted session's transcript ends in a rate-limit API-error assistant record.
    halted_id = demo.sid_for("data-pipeline")
    proj = home / "projects"
    transcript = next(proj.rglob(f"{halted_id}.jsonl"))
    last = [json.loads(line) for line in transcript.read_text().splitlines() if line][-1]
    assert last["isApiErrorMessage"] is True
    assert "hit your usage limit" in last["message"]["content"][0]["text"]


def test_clean_removes_demo_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`ccc demo --clean` deletes the demo dir."""
    home = tmp_path / "demo"
    demo.seed(home)
    assert home.exists()

    class _Args:
        ls = False
        dir = str(home)
        clean = True

    rc = demo.run(_Args())
    assert rc == 0
    assert not home.exists()
