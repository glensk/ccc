"""Tests for the codex-in-claude engine and the future-job codex launch wiring.

The engine now lives inside the package as ``command_center.codex_in_claude`` (the
``codex-in-claude`` console entry point; the repo-root ``codex-in-claude.py`` is a thin
PATH-compat shim). Tests avoid any live Codex call: subprocess and the model catalog are
monkeypatched.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from command_center.models import job_launch_prefix
from command_center.store import Store

_FAKE_CATALOG = [
    {"slug": "gpt-5.6-sol", "visibility": "list", "default_reasoning_level": "medium"},
    {"slug": "gpt-5.5", "visibility": "list", "default_reasoning_level": "xhigh"},
    {"slug": "gpt-5.4", "visibility": "list", "default_reasoning_level": "medium"},
    {"slug": "codex-auto-review", "visibility": "hide", "default_reasoning_level": "medium"},
]


def _load_engine() -> ModuleType:
    import command_center.codex_in_claude as engine

    return engine


@pytest.fixture()
def cic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The engine module, with config + Claude home pointed at tmp and the catalog faked.

    ``CLAUDE_CONFIG_DIR`` matters: ``set-model``/``set-effort`` re-stamp the model marker
    into the codex skill/command descriptions, and tests must never touch the real ones.
    """
    mod = _load_engine()
    monkeypatch.setenv("CODEX_IN_CLAUDE_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(mod, "list_models", lambda **_: list(_FAKE_CATALOG))
    return mod


# --------------------------- config / model resolution --------------------------- #
def test_resolve_model_defaults_to_newest(cic: ModuleType) -> None:
    assert cic.DEFAULT_MODEL == "gpt-5.6-sol"
    assert cic.resolve_model(None) == cic.DEFAULT_MODEL
    assert cic.resolve_model("delegate-review") == cic.DEFAULT_MODEL


def test_resolve_model_precedence(cic: ModuleType) -> None:
    cic.save_config({"default": "gpt-5.4", "delegate-review": "gpt-5.5", "debate": None})
    assert cic.resolve_model("delegate-review") == "gpt-5.5"  # per-command wins
    assert cic.resolve_model("debate") == "gpt-5.4"  # falls back to default
    assert cic.resolve_model(None) == "gpt-5.4"


def test_config_roundtrip_and_corrupt(cic: ModuleType, tmp_path: Path) -> None:
    path = cic.save_config({"default": "gpt-5.4", "delegate-review": None, "debate": "gpt-5.5"})
    assert Path(path).exists()
    assert cic.load_config()["debate"] == "gpt-5.5"
    Path(path).write_text("{not json", encoding="utf-8")  # corrupt → defaults, no crash
    assert cic.load_config()["default"] == cic.DEFAULT_MODEL


def test_set_model_for_all_clears_per_command_pins(cic: ModuleType) -> None:
    """--for all is a real reset: a stale per-command pin must not shadow the new default."""
    cic.save_config({"default": "gpt-5.4", "delegate-review": "gpt-5.5", "debate": "gpt-5.5"})
    assert cic.cmd_set_model(argparse.Namespace(slug="gpt-5.6-sol", for_command="all")) == cic.EX_OK
    assert cic.resolve_model("delegate-review") == "gpt-5.6-sol"
    assert cic.resolve_model("debate") == "gpt-5.6-sol"
    # a bare set-model (no --for) still only moves the default, leaving pins alone
    cic.save_config({"default": "gpt-5.4", "delegate-review": "gpt-5.5", "debate": None})
    assert cic.cmd_set_model(argparse.Namespace(slug="gpt-5.6-sol", for_command=None)) == cic.EX_OK
    assert cic.resolve_model("delegate-review") == "gpt-5.5"


def test_parse_models_list_and_dict(cic: ModuleType) -> None:
    assert cic._parse_models('[{"slug": "a"}]') == [{"slug": "a"}]
    assert cic._parse_models('{"models": [{"slug": "b"}]}') == [{"slug": "b"}]


def test_valid_slug_and_effort(cic: ModuleType) -> None:
    assert cic.valid_slug("gpt-5.5") is True
    assert cic.valid_slug("nope") is False
    assert cic.effort_of("gpt-5.5") == "xhigh"


# --------------------------- delegate prompt contract --------------------------- #
def test_patch_contract_demands_diff(cic: ModuleType) -> None:
    prompt = cic._build_delegate_prompt("add x", write=False, feedback=None, round_no=1)
    assert "READ-ONLY" in prompt and "### DIFF" in prompt and "add x" in prompt


def test_write_contract_allows_edits(cic: ModuleType) -> None:
    prompt = cic._build_delegate_prompt("add x", write=True, feedback="tests failed", round_no=2)
    assert "edit files" in prompt and "tests failed" in prompt and "REVISION (round 2)" in prompt


# --------------------------- delegate run (no live codex) --------------------------- #
def _ns(**kw: object) -> argparse.Namespace:
    base = dict(
        prompt="do x",
        write=False,
        scout=False,
        cwd=None,
        round=1,
        feedback=None,
        model=None,
        effort=None,
        timeout=600,
        # 0 disables the flock concurrency gate (limit <= 0 short-circuits in
        # _concurrency_slot), so these unit tests never touch the real slot dir.
        max_concurrent=0,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_delegate_prints_model_first_and_assembles_cmd(
    cic: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        out_path = cmd[cmd.index("-o") + 1]  # codex writes its final message here
        Path(out_path).write_text("### SELF-CHECK\nok\n### DIFF\n```diff\n```\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cic.subprocess, "run", fake_run)
    rc = cic.cmd_delegate(_ns())
    out = capsys.readouterr().out
    assert rc == cic.EX_OK
    assert out.splitlines()[0] == "model: gpt-5.6-sol (effort xhigh)"  # guaranteed 1st line
    assert captured["cmd"][:3] == ["codex", "exec", "-s"]
    assert "read-only" in captured["cmd"] and "-m" in captured["cmd"]
    assert "gpt-5.6-sol" in captured["cmd"]


def test_delegate_write_mode_uses_workspace_write(
    cic: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        Path(cmd[cmd.index("-o") + 1]).write_text("done", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cic.subprocess, "run", fake_run)
    monkeypatch.setattr(cic, "_git_status", lambda _cwd: [])
    assert cic.cmd_delegate(_ns(write=True)) == cic.EX_OK
    assert "workspace-write" in captured["cmd"]


def test_delegate_scout_is_readonly_plan(cic: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """--scout forces read-only (even with --write) and uses the PLAN contract, no diff."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        Path(cmd[cmd.index("-o") + 1]).write_text("### PLAN\n1. ...", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cic.subprocess, "run", fake_run)
    # scout wins over write → read-only sandbox, and the prompt is the scout contract
    assert cic.cmd_delegate(_ns(scout=True, write=True)) == cic.EX_OK
    assert "read-only" in captured["cmd"] and "workspace-write" not in captured["cmd"]
    prompt = captured["cmd"][-1]
    assert "SCOUTING" in prompt and "### PLAN" in prompt and "NOT write" in prompt


def test_delegate_effort(
    cic: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        Path(cmd[cmd.index("-o") + 1]).write_text("ok", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cic.subprocess, "run", fake_run)
    # no -e flag -> the config-default effort (xhigh) resolves, so the -c override IS
    # passed and the first line reflects it (shown_effort comes from config, not catalog).
    assert cic.cmd_delegate(_ns()) == cic.EX_OK
    assert "model_reasoning_effort=xhigh" in " ".join(captured["cmd"])
    assert capsys.readouterr().out.splitlines()[0] == "model: gpt-5.6-sol (effort xhigh)"
    # explicit -e high -> passed through and reflected in the first line
    assert cic.cmd_delegate(_ns(effort="high")) == cic.EX_OK
    assert "model_reasoning_effort=high" in " ".join(captured["cmd"])
    assert capsys.readouterr().out.splitlines()[0] == "model: gpt-5.6-sol (effort high)"


def test_set_get_effort(cic: ModuleType) -> None:
    """set-effort persists a valid level; a fresh config resolves to the xhigh base default.

    'default' clears the explicit key by writing an explicit ``"effort": null`` that
    overrides the xhigh base default in load_config's ``base.update(data)``, so
    resolve_effort then returns None (not xhigh).
    """
    assert cic.resolve_effort() == "xhigh"
    cic.cmd_set_effort(argparse.Namespace(level="high"))
    assert cic.resolve_effort() == "high"
    cic.cmd_set_effort(argparse.Namespace(level="default"))
    assert cic.resolve_effort() is None


def test_delegate_exit_codes(cic: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_notfound(*_: object, **__: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(cic.subprocess, "run", raise_notfound)
    assert cic.cmd_delegate(_ns()) == cic.EX_NO_CODEX

    def raise_timeout(*_: object, **__: object) -> None:
        raise subprocess.TimeoutExpired("codex", 1)

    monkeypatch.setattr(cic.subprocess, "run", raise_timeout)
    assert cic.cmd_delegate(_ns()) == cic.EX_TIMEOUT

    assert cic.cmd_delegate(_ns(model="bogus")) == cic.EX_INVALID_MODEL
    assert cic.cmd_delegate(_ns(prompt="   ")) == cic.EX_USAGE


# --------------------------- skill/command description markers --------------------------- #
_BLOCK_SKILL = """---
name: codex-debate
description: >-
  Run a bounded debate. Use in mode=plan to stress-test a plan (hooks: a PLAN*.md
  write) and in mode=diagnose otherwise.
---

# body
"""

_INLINE_CMD = """---
description: Delegate a task to Codex (Codex implements; Claude reviews) — saves tokens.
argument-hint: "[--write] <task>"
---

body
"""


def _surfaces(cic: ModuleType, tmp_path: Path) -> tuple[Path, Path]:
    """Create fake skill + command surfaces under the tmp CLAUDE_CONFIG_DIR."""
    home = cic.claude_home()
    skill = home / "skills" / "codex-debate" / "SKILL.md"
    cmd = home / "commands" / "codex-implement-task-and-claude-review.md"
    for path, text in ((skill, _BLOCK_SKILL), (cmd, _INLINE_CMD)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return skill, cmd


def test_marker_prefixes_block_and_inline_descriptions(cic: ModuleType, tmp_path: Path) -> None:
    """Both YAML shapes get the marker as a *prefix*; the inline one is quoted (YAML-safe)."""
    skill, cmd = _surfaces(cic, tmp_path)
    cic.save_config({"default": "gpt-5.6-sol", "effort": "xhigh"})
    rows = cic.sync_markers()
    assert {status for status, _, _ in rows if status != "missing"} == {"updated"}
    assert "  [codex gpt-5.6-sol effort=xhigh] Run a bounded debate." in skill.read_text()
    # inline value must be double-quoted, else a leading '[' reads as a YAML flow sequence
    line = next(ln for ln in cmd.read_text().splitlines() if ln.startswith("description:"))
    assert line == (
        'description: "[codex gpt-5.6-sol effort=xhigh] Delegate a task to Codex '
        '(Codex implements; Claude reviews) — saves tokens."'
    )


def test_marker_is_idempotent_and_replaces_stale(cic: ModuleType, tmp_path: Path) -> None:
    """Re-syncing never stacks markers; a model change rewrites the existing one in place."""
    skill, cmd = _surfaces(cic, tmp_path)
    cic.save_config({"default": "gpt-5.6-sol", "effort": "xhigh"})
    cic.sync_markers()
    first = (skill.read_text(), cmd.read_text())
    assert {s for s, _, _ in cic.sync_markers() if s != "missing"} == {"ok"}  # no-op re-run
    assert (skill.read_text(), cmd.read_text()) == first

    cic.save_config({"default": "gpt-5.4", "effort": "low"})
    assert {s for s, _, _ in cic.sync_markers() if s != "missing"} == {"updated"}
    for text in (skill.read_text(), cmd.read_text()):
        assert text.count("[codex ") == 1
        assert "[codex gpt-5.4 effort=low]" in text
        assert "gpt-5.6-sol" not in text


def test_set_model_syncs_descriptions(cic: ModuleType, tmp_path: Path) -> None:
    """The whole point: changing the model updates what /codex… help will show."""
    skill, _cmd = _surfaces(cic, tmp_path)
    assert cic.cmd_set_model(argparse.Namespace(slug="gpt-5.4", for_command="all")) == cic.EX_OK
    assert "[codex gpt-5.4 effort=xhigh]" in skill.read_text()
    assert cic.cmd_set_effort(argparse.Namespace(level="medium")) == cic.EX_OK
    assert "[codex gpt-5.4 effort=medium]" in skill.read_text()


def test_sync_check_reports_drift_without_writing(cic: ModuleType, tmp_path: Path) -> None:
    skill, _cmd = _surfaces(cic, tmp_path)
    before = skill.read_text()
    assert cic.cmd_sync_skills(argparse.Namespace(check=True)) == 1  # stale
    assert skill.read_text() == before  # --check never writes
    assert cic.cmd_sync_skills(argparse.Namespace(check=False)) == cic.EX_OK
    assert cic.cmd_sync_skills(argparse.Namespace(check=True)) == cic.EX_OK  # now current


def test_sync_tolerates_missing_and_unparsable_surfaces(cic: ModuleType, tmp_path: Path) -> None:
    """A file with no frontmatter is an error row, not a traceback; absent files are 'missing'."""
    skill, _cmd = _surfaces(cic, tmp_path)
    skill.write_text("no frontmatter here\n", encoding="utf-8")
    rows = dict((path, status) for status, path, _ in cic.sync_markers())
    assert rows[skill] == "error"
    assert cic.cmd_sync_skills(argparse.Namespace(check=True)) == cic.EX_USAGE
    missing = cic.claude_home() / "commands" / "codex-debate.md"
    assert rows[missing] == "missing"


def test_marker_falls_back_when_effort_unknown(cic: ModuleType) -> None:
    """An unknown slug has no catalog effort — the marker drops the effort= part, no '?'."""
    cic.save_config({"default": "mystery-model", "effort": None})
    assert cic.marker_for("debate") == "[codex mystery-model]"


def test_pick_refuses_without_a_tty(cic: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cic.sys.stdin, "isatty", lambda: False, raising=False)
    ns = argparse.Namespace(for_command=None, refresh=False)
    assert cic.cmd_pick(ns) == cic.EX_USAGE


def test_pick_sets_the_chosen_model(cic: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cic.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *_: "3")  # gpt-5.4 in _FAKE_CATALOG order
    assert cic.cmd_pick(argparse.Namespace(for_command="debate", refresh=False)) == cic.EX_OK
    assert cic.resolve_model("debate") == "gpt-5.4"
    # empty input keeps the current model
    monkeypatch.setattr("builtins.input", lambda *_: "")
    assert cic.cmd_pick(argparse.Namespace(for_command="debate", refresh=False)) == cic.EX_OK
    assert cic.resolve_model("debate") == "gpt-5.4"
    # out-of-range is a usage error, not a crash
    monkeypatch.setattr("builtins.input", lambda *_: "99")
    assert cic.cmd_pick(argparse.Namespace(for_command="debate", refresh=False)) == cic.EX_USAGE


# --------------------------- future-job launch wiring --------------------------- #
def test_job_launch_prefix() -> None:
    """A codex job prefixes its launch prompt with the slash command (claude job = no prefix)."""
    assert job_launch_prefix("claude") == ""
    assert job_launch_prefix("codex") == "/codex-implement-task-and-claude-review "
    assert job_launch_prefix("codex-write") == "/codex-implement-task-and-claude-review --write "


def test_create_draft_job_type_roundtrip_and_coercion(tmp_path: Path) -> None:
    """A draft stores its job_type, defaults to 'claude', and coerces unknown values."""
    store = Store(tmp_path / "s.db")
    assert store.create_draft("a", "/r", "aim", job_type="codex").job_type == "codex"
    assert store.create_draft("b", "/r", "aim").job_type == "claude"  # default
    assert store.create_draft("c", "/r", "aim", job_type="garbage").job_type == "claude"  # coerced
    got = store.get("a")
    assert got is not None and got.job_type == "codex"  # persisted across read
