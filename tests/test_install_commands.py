"""``ccc install-commands`` — copy the ccc slash commands into $CLAUDE_HOME.

Covers a fresh install, the optional codex pair, idempotent reruns, overwrite-with-backup,
content-match uninstall (a user-edited command is left alone), and dry-run writing nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import install_commands


@pytest.fixture(autouse=True)
def _claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    return tmp_path


def _commands(home: Path) -> Path:
    return home / "commands"


def test_fresh_install_writes_seven_core_commands(_claude_home: Path) -> None:
    assert install_commands.run() == 0
    for stem in install_commands.CORE_COMMANDS:
        assert (_commands(_claude_home) / f"{stem}.md").is_file()
    # The codex pair is NOT installed without --codex.
    assert not (_commands(_claude_home) / "codex-implement-task-and-claude-review.md").exists()
    assert not (_claude_home / "skills").exists()


def test_codex_flag_adds_command_and_skill(_claude_home: Path) -> None:
    assert install_commands.run(codex=True) == 0
    assert (_commands(_claude_home) / "codex-implement-task-and-claude-review.md").is_file()
    skill = _claude_home / "skills" / "codex-implement-task-and-claude-review" / "SKILL.md"
    assert skill.is_file()


def test_vendored_commands_have_no_personal_paths(_claude_home: Path) -> None:
    install_commands.run(codex=True)
    forbidden = "/Users/" + "albert"
    for path in _claude_home.rglob("*.md"):
        assert forbidden not in path.read_text(encoding="utf-8")
    # The codex assets must reference the console entry point, not the .py shim.
    codex_cmd = (_commands(_claude_home) / "codex-implement-task-and-claude-review.md").read_text(
        encoding="utf-8"
    )
    assert "codex-in-claude.py" not in codex_cmd
    assert "codex-in-claude" in codex_cmd


def test_rerun_is_idempotent(_claude_home: Path) -> None:
    install_commands.run()
    aim = _commands(_claude_home) / "aim.md"
    before = aim.stat().st_mtime_ns
    install_commands.run()  # second run: byte-identical → skipped
    assert aim.stat().st_mtime_ns == before
    # No backup files created on an idempotent rerun.
    assert not list(_commands(_claude_home).glob("*.ccc-backup-*"))


def test_overwrite_backs_up_previous_content(_claude_home: Path) -> None:
    install_commands.run()
    aim = _commands(_claude_home) / "aim.md"
    aim.write_text("stale user content\n", encoding="utf-8")
    assert install_commands.run() == 0
    backups = list(_commands(_claude_home).glob("aim.md.ccc-backup-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "stale user content\n"
    # The live file is back to the shipped asset.
    assert "set-aim" in aim.read_text(encoding="utf-8")


def test_uninstall_removes_only_unmodified(_claude_home: Path) -> None:
    install_commands.run(codex=True)
    # Edit one command so it no longer matches the shipped asset.
    edited = _commands(_claude_home) / "done.md"
    edited.write_text("my own version\n", encoding="utf-8")
    assert install_commands.run(codex=True, uninstall=True) == 0
    # The edited file is preserved; the untouched ones are gone.
    assert edited.exists()
    assert not (_commands(_claude_home) / "aim.md").exists()
    skill = _claude_home / "skills" / "codex-implement-task-and-claude-review" / "SKILL.md"
    assert not skill.exists()


def test_dry_run_writes_nothing(_claude_home: Path) -> None:
    assert install_commands.run(dry_run=True) == 0
    assert not _commands(_claude_home).exists()


def test_build_plan_covers_expected_targets(_claude_home: Path) -> None:
    plan = install_commands.build_plan(_claude_home, codex=True)
    labels = {item.label for item in plan}
    assert "commands/aim.md" in labels
    assert "commands/codex-implement-task-and-claude-review.md" in labels
    assert "skills/codex-implement-task-and-claude-review/SKILL.md" in labels
    assert len(plan) == len(install_commands.CORE_COMMANDS) + 2
