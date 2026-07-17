"""Packaging smoke test — the wheel must survive a non-editable install.

Builds the wheel, installs it into a scratch venv with a temp HOME/CLAUDE_HOME, and
asserts every console entry point runs and that the package data (``assets/README.md``)
ships and is reachable via ``importlib.resources``. Skipped when ``uv`` is unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_UV = shutil.which("uv")

pytestmark = pytest.mark.skipif(_UV is None, reason="uv not available to build/install the wheel")


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    """Run *cmd*, capturing output, never raising on a non-zero exit."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=300, **kwargs)


def test_wheel_ships_entrypoints_and_assets(tmp_path: Path) -> None:  # pylint: disable=too-many-locals
    assert _UV is not None
    dist = tmp_path / "dist"
    build = _run([_UV, "build", "--wheel", "-o", str(dist)], cwd=str(_ROOT))
    assert build.returncode == 0, build.stderr
    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1, wheels
    wheel = wheels[0]

    # The wheel (a zip) carries the package data + the moved-in modules.
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
    assert "command_center/assets/README.md" in names
    # The onboarding assets ccc init / install-commands / obsidian-setup seed must ship so
    # they survive a non-editable install (importlib.resources reads them at runtime).
    assert "command_center/assets/commands/aim.md" in names
    skill = "command_center/assets/codex/skills/codex-implement-task-and-claude-review/SKILL.md"
    assert skill in names
    # The core skill shipped by default (install-commands) must survive a non-editable install.
    assert "command_center/assets/skills/ccc-mark-done-and-close/SKILL.md" in names
    assert "command_center/assets/obsidian/future.md.tmpl" in names
    assert "command_center/assets/obsidian/plugins.json" in names
    assert "command_center/assets/karabiner/peek-s-p.json" in names
    assert "command_center/codex_in_claude.py" in names
    assert "command_center/session_continue.py" in names

    venv = tmp_path / "venv"
    assert _run([_UV, "venv", str(venv)]).returncode == 0
    py = venv / "bin" / "python"
    install = _run([_UV, "pip", "install", "--python", str(py), str(wheel)])
    assert install.returncode == 0, install.stderr

    home = tmp_path / "home"
    env = {**os.environ, "HOME": str(home), "CLAUDE_HOME": str(home / ".claude")}
    bindir = venv / "bin"
    for exe in ("ccc", "claude-session-continue", "codex-in-claude"):
        result = _run([str(bindir / exe), "--help"], env=env)
        assert result.returncode == 0, f"{exe} --help failed: {result.stderr}"

    # assets/README.md reachable via importlib.resources from the INSTALLED package.
    code = (
        "from importlib.resources import files\n"
        "text = (files('command_center') / 'assets' / 'README.md').read_text(encoding='utf-8')\n"
        "assert text.strip(), 'assets/README.md is empty'\n"
        "print('OK')\n"
    )
    probe = _run([str(py), "-c", code], env=env)
    assert probe.returncode == 0, probe.stderr
    assert "OK" in probe.stdout
