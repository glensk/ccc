"""``ccc install-shell`` — the opt-in shell rc integration block.

Fresh zsh/bash install, markered idempotence, wrapper-name collision refusal, uninstall,
and dry-run. Everything runs against a temp rc file — the real ~/.zshrc is never touched.
The AIM wrapper is asserted to export exactly the env var the SessionStart hook reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import hooks, shell_install


@pytest.fixture(autouse=True)
def _no_path_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: no same-named binary on PATH (so a fresh 'c' wrapper installs cleanly)."""
    monkeypatch.setattr(shell_install.shutil, "which", lambda _name: None)


def _rc(tmp_path: Path) -> Path:
    return tmp_path / ".zshrc"


# --------------------------------------------------------------------------- #
# env-var contract: the wrapper must export what the SessionStart hook consumes
# --------------------------------------------------------------------------- #
def test_wrapper_exports_the_exact_hook_env_var() -> None:
    # The SessionStart hook reads os.environ["CLAUDE_SESSION_AIM"]; the wrapper must set it.
    assert shell_install.AIM_ENV_VAR == "CLAUDE_SESSION_AIM"
    assert 'os.environ.get("CLAUDE_SESSION_AIM")' in Path(hooks.__file__).read_text(
        encoding="utf-8"
    )
    block = shell_install.build_block("bash")
    assert 'CLAUDE_SESSION_AIM="$reply" command claude "$@"' in block
    # empty-input path BLANKS the var so a nested launch never inherits a parent AIM
    assert 'CLAUDE_SESSION_AIM= command claude "$@"' in block


# --------------------------------------------------------------------------- #
# fresh install (zsh + bash)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("shell", ["zsh", "bash"])
def test_fresh_install_writes_markered_block(tmp_path: Path, shell: str) -> None:
    rc = tmp_path / f".{shell}rc"
    rc.write_text("# my existing rc\nexport FOO=1\n", encoding="utf-8")
    rc_before = rc.read_text(encoding="utf-8")

    code = shell_install.install(rc_path=rc, shell=shell)
    assert code == 0
    text = rc.read_text(encoding="utf-8")
    assert shell_install.MARKER_START in text and shell_install.MARKER_END in text
    assert rc_before.rstrip("\n") in text  # existing content preserved
    # Both pieces present by default.
    assert "AIM of this session (empty to skip):" in text
    assert "_ccc_tab_badge" in text
    # Shell-appropriate hook registration.
    if shell == "zsh":
        assert "add-zsh-hook precmd _ccc_tab_badge" in text
    else:
        assert "PROMPT_COMMAND" in text
    # A timestamped backup of the pre-existing rc was written.
    assert list(tmp_path.glob(f".{shell}rc.ccc-backup-*"))


def test_no_rc_file_is_created_from_scratch(tmp_path: Path) -> None:
    rc = _rc(tmp_path)
    assert not rc.exists()
    assert shell_install.install(rc_path=rc, shell="zsh") == 0
    assert rc.exists()
    assert shell_install.MARKER_START in rc.read_text(encoding="utf-8")
    # Nothing to back up when the file did not exist.
    assert not list(tmp_path.glob(".zshrc.ccc-backup-*"))


# --------------------------------------------------------------------------- #
# idempotence: rerun replaces the block, exactly one block, foreign lines kept
# --------------------------------------------------------------------------- #
def test_rerun_is_idempotent_single_block(tmp_path: Path) -> None:
    rc = _rc(tmp_path)
    rc.write_text("alias ll='ls -la'\n", encoding="utf-8")
    shell_install.install(rc_path=rc, shell="zsh")
    first = rc.read_text(encoding="utf-8")
    shell_install.install(rc_path=rc, shell="zsh")
    second = rc.read_text(encoding="utf-8")
    assert second.count(shell_install.MARKER_START) == 1
    assert second.count(shell_install.MARKER_END) == 1
    assert "alias ll='ls -la'" in second
    # Content between the markers is identical on rerun (idempotent body).
    assert shell_install.find_block(first) is not None
    assert first.split(shell_install.MARKER_START)[1] == second.split(shell_install.MARKER_START)[1]


def test_rerun_with_badges_only_replaces_the_block(tmp_path: Path) -> None:
    rc = _rc(tmp_path)
    shell_install.install(rc_path=rc, shell="zsh")  # wrapper + badges
    assert "AIM of this session" in rc.read_text(encoding="utf-8")
    shell_install.install(rc_path=rc, shell="zsh", include_wrapper=False)  # badges only
    text = rc.read_text(encoding="utf-8")
    assert "AIM of this session" not in text  # old wrapper gone
    assert "_ccc_tab_badge" in text
    assert text.count(shell_install.MARKER_START) == 1


# --------------------------------------------------------------------------- #
# collision refusal
# --------------------------------------------------------------------------- #
def test_refuses_when_wrapper_name_alias_exists(tmp_path: Path, capsys) -> None:
    rc = _rc(tmp_path)
    rc.write_text("alias c='clear'\n", encoding="utf-8")
    code = shell_install.install(rc_path=rc, shell="zsh")
    assert code == 1
    assert shell_install.MARKER_START not in rc.read_text(encoding="utf-8")  # nothing written
    assert "refusing to install" in capsys.readouterr().err


def test_refuses_when_wrapper_name_function_exists(tmp_path: Path) -> None:
    rc = _rc(tmp_path)
    rc.write_text("c() { code . ; }\n", encoding="utf-8")
    assert shell_install.install(rc_path=rc, shell="zsh") == 1


def test_refuses_when_wrapper_name_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell_install.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert shell_install.install(rc_path=_rc(tmp_path), shell="zsh") == 1


def test_alternate_wrapper_name_sidesteps_collision(tmp_path: Path) -> None:
    rc = _rc(tmp_path)
    rc.write_text("alias c='clear'\n", encoding="utf-8")
    code = shell_install.install(rc_path=rc, shell="zsh", wrapper_name="cc")
    assert code == 0
    text = rc.read_text(encoding="utf-8")
    assert "cc() {" in text
    assert "alias c='clear'" in text  # the pre-existing c is untouched


def test_no_collision_check_when_wrapper_skipped(tmp_path: Path) -> None:
    rc = _rc(tmp_path)
    rc.write_text("alias c='clear'\n", encoding="utf-8")  # would collide, but --no-wrapper
    assert shell_install.install(rc_path=rc, shell="zsh", include_wrapper=False) == 0
    assert "_ccc_tab_badge" in rc.read_text(encoding="utf-8")


def test_own_block_is_not_a_self_collision(tmp_path: Path) -> None:
    """A rerun sees the wrapper the previous run wrote, but that is ccc's own block."""
    rc = _rc(tmp_path)
    shell_install.install(rc_path=rc, shell="zsh")
    # collision_reason strips ccc's own block first, so the 'c' our block defines is free.
    assert (
        shell_install.collision_reason("c", rc.read_text(encoding="utf-8"), check_path=False) == ""
    )
    assert shell_install.install(rc_path=rc, shell="zsh") == 0  # rerun succeeds


# --------------------------------------------------------------------------- #
# uninstall + dry-run
# --------------------------------------------------------------------------- #
def test_uninstall_removes_only_the_block(tmp_path: Path) -> None:
    rc = _rc(tmp_path)
    rc.write_text("export KEEP=1\n", encoding="utf-8")
    shell_install.install(rc_path=rc, shell="zsh")
    assert shell_install.install(rc_path=rc, shell="zsh", uninstall=True) == 0
    text = rc.read_text(encoding="utf-8")
    assert shell_install.MARKER_START not in text
    assert "export KEEP=1" in text  # foreign content survives


def test_uninstall_noop_when_absent(tmp_path: Path, capsys) -> None:
    rc = _rc(tmp_path)
    rc.write_text("export KEEP=1\n", encoding="utf-8")
    assert shell_install.install(rc_path=rc, shell="zsh", uninstall=True) == 0
    assert "no ccc shell integration block" in capsys.readouterr().out
    assert rc.read_text(encoding="utf-8") == "export KEEP=1\n"  # untouched


def test_dry_run_prints_block_and_writes_nothing(tmp_path: Path, capsys) -> None:
    rc = _rc(tmp_path)
    assert shell_install.install(rc_path=rc, shell="zsh", dry_run=True) == 0
    out = capsys.readouterr().out
    assert shell_install.MARKER_START in out
    assert str(rc) in out
    assert not rc.exists()  # dry-run never writes


# --------------------------------------------------------------------------- #
# shell / rc detection
# --------------------------------------------------------------------------- #
def test_detect_shell_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/usr/bin/zsh")
    assert shell_install.detect_shell() == "zsh"
    monkeypatch.setenv("SHELL", "/bin/bash")
    assert shell_install.detect_shell() == "bash"
    monkeypatch.setenv("SHELL", "/usr/bin/fish")  # unknown → bash-syntax default
    assert shell_install.detect_shell() == "bash"


def test_default_rc_per_shell() -> None:
    assert shell_install.default_rc("zsh").name == ".zshrc"
    assert shell_install.default_rc("bash").name == ".bashrc"


def test_both_pieces_skipped_is_an_error(tmp_path: Path, capsys) -> None:
    code = shell_install.install(
        rc_path=_rc(tmp_path), shell="zsh", include_wrapper=False, include_badges=False
    )
    assert code == 1
    assert "nothing to install" in capsys.readouterr().err
