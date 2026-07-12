"""Score-backend runners + the fallback ladder (``command_center.llm``).

Every subprocess is mocked — NO network / LLM call runs in pytest. Covers each runner's
command construction and never-raise contract, plus the ladder's order, first-success-wins,
all-fail → ``None``, unknown-backend skip-with-warning, and timeout → next-rung behaviour.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest

from command_center import config, llm


class _Proc:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _which(*present: str) -> Callable[[str], str | None]:
    """A ``shutil.which`` stub that only "finds" the named tools."""
    return lambda name: f"/bin/{name}" if name in present else None


# --------------------------------------------------------------------------- #
# run_copilot (opencode)
# --------------------------------------------------------------------------- #
def test_run_copilot_builds_opencode_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm.shutil, "which", _which("opencode"))
    seen_cmd: list[object] = []
    seen_env: dict[str, str] = {}

    def fake_run(cmd, **kw):
        seen_cmd.append(cmd)
        seen_env.update(kw["env"])
        return _Proc(0, stdout='{"score":80}\n')

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    out = llm.run_copilot("PROMPT", "gpt-5.4")
    assert out == '{"score":80}'
    assert seen_cmd == [["opencode", "run", "-m", "github-copilot/gpt-5.4", "PROMPT"]]
    # the recursion / no-autocommit guards are set, like the claude/codex runners
    assert seen_env["CCC_INTERNAL"] == "1"
    assert seen_env["AI_NO_AUTOCOMMIT"] == "1"


def test_run_copilot_missing_binary_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm.shutil, "which", _which())
    assert llm.run_copilot("p", "gpt-5.4") is None


def test_run_copilot_empty_model_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm.shutil, "which", _which("opencode"))
    assert llm.run_copilot("p", "") is None


def test_run_copilot_nonzero_exit_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm.shutil, "which", _which("opencode"))
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: _Proc(1, stdout="boom"))
    assert llm.run_copilot("p", "gpt-5.4") is None


def test_run_copilot_timeout_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm.shutil, "which", _which("opencode"))

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="opencode", timeout=1)

    monkeypatch.setattr(llm.subprocess, "run", boom)
    assert llm.run_copilot("p", "gpt-5.4") is None


# --------------------------------------------------------------------------- #
# run_gemini
# --------------------------------------------------------------------------- #
def test_run_gemini_builds_command_with_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm.shutil, "which", _which("gemini"))
    seen_cmd: list[object] = []

    def fake_run(cmd, **kw):
        seen_cmd.append(cmd)
        return _Proc(0, stdout="RESP\n")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    assert llm.run_gemini("PROMPT", "gemini-2.5-pro") == "RESP"
    assert seen_cmd == [["gemini", "-p", "PROMPT", "-m", "gemini-2.5-pro"]]


def test_run_gemini_omits_model_flag_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm.shutil, "which", _which("gemini"))
    seen_cmd: list[object] = []

    def fake_run(cmd, **kw):
        seen_cmd.append(cmd)
        return _Proc(0, stdout="ok")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    assert llm.run_gemini("PROMPT") == "ok"
    assert seen_cmd == [["gemini", "-p", "PROMPT"]]


def test_run_gemini_nonzero_exit_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm.shutil, "which", _which("gemini"))
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: _Proc(1, stderr="Ineligible"))
    assert llm.run_gemini("p", "m") is None


def test_run_gemini_missing_binary_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm.shutil, "which", _which())
    assert llm.run_gemini("p") is None


# --------------------------------------------------------------------------- #
# run_custom (stdin/stdout contract)
# --------------------------------------------------------------------------- #
def test_run_custom_feeds_prompt_on_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["shell"] = kw["shell"]
        seen["input"] = kw["input"]
        return _Proc(0, stdout="MODEL RESPONSE\n")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    out = llm.run_custom("THE PROMPT", "cat")
    assert out == "MODEL RESPONSE"
    assert seen["shell"] is True
    assert seen["input"] == "THE PROMPT"  # prompt arrives on stdin
    assert seen["cmd"] == "cat"


def test_run_custom_empty_command_returns_none() -> None:
    assert llm.run_custom("p", "   ") is None


def test_run_custom_nonzero_exit_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: _Proc(3, stdout="partial"))
    assert llm.run_custom("p", "false") is None


# --------------------------------------------------------------------------- #
# run_ladder
# --------------------------------------------------------------------------- #
def _stub_runners(monkeypatch: pytest.MonkeyPatch, results: dict[str, str | None]) -> list[str]:
    """Replace every rung runner with one that records its name and returns a canned value."""
    order: list[str] = []

    def make(name: str) -> Callable[..., str | None]:
        def _f(*_a, **_k) -> str | None:
            order.append(name)
            return results.get(name)

        return _f

    monkeypatch.setattr(llm, "run_copilot", make("copilot"))
    monkeypatch.setattr(llm, "run_gemini", make("gemini"))
    monkeypatch.setattr(llm, "run_codex", make("codex"))
    monkeypatch.setattr(llm, "run_model", make("claude"))  # the "claude" rung
    monkeypatch.setattr(llm, "run_custom", make("custom"))
    return order


def test_ladder_tries_in_order_and_first_nonempty_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    order = _stub_runners(
        monkeypatch,
        {"copilot": None, "gemini": "   ", "codex": '{"score":1}', "claude": "unused"},
    )
    cfg = config.Config(score_backends=["copilot", "gemini", "codex", "claude"])
    assert llm.run_ladder("p", cfg) == ("codex", '{"score":1}')
    # copilot None → gemini whitespace-only (treated as failure) → codex serves; claude untried.
    assert order == ["copilot", "gemini", "codex"]


def test_ladder_all_fail_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    order = _stub_runners(monkeypatch, {})  # every rung returns None
    cfg = config.Config(score_backends=["copilot", "claude"])
    assert llm.run_ladder("p", cfg) is None
    assert order == ["copilot", "claude"]  # every configured rung was attempted


def test_ladder_timeout_falls_through_to_next_rung(monkeypatch: pytest.MonkeyPatch) -> None:
    # A rung that timed out already swallowed the TimeoutExpired and returned None; the ladder
    # must proceed to the next rung rather than give up.
    order = _stub_runners(monkeypatch, {"copilot": None, "codex": "SERVED"})
    cfg = config.Config(score_backends=["copilot", "codex"])
    assert llm.run_ladder("p", cfg) == ("codex", "SERVED")
    assert order == ["copilot", "codex"]


def test_ladder_skips_unknown_backend_with_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    order = _stub_runners(monkeypatch, {"claude": "ok"})
    cfg = config.Config(score_backends=["bogus", "claude"])
    assert llm.run_ladder("p", cfg) == ("claude", "ok")
    assert order == ["claude"]  # the unknown rung was never dispatched
    assert "unknown score backend" in capsys.readouterr().err


def test_ladder_backends_override_ignores_config(monkeypatch: pytest.MonkeyPatch) -> None:
    order = _stub_runners(monkeypatch, {"gemini": "G"})
    cfg = config.Config(score_backends=["claude"])  # config says claude…
    assert llm.run_ladder("p", cfg, backends=["gemini"]) == ("gemini", "G")  # …override wins
    assert order == ["gemini"]


def test_ladder_claude_rung_uses_score_model_over_llm_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    def fake_run_model(prompt: str, model: str, *, purpose: str = "", note: str = "") -> str:
        seen["model"] = model
        return "ok"

    monkeypatch.setattr(llm, "run_model", fake_run_model)
    cfg = config.Config(score_backends=["claude"], score_model="score-m", llm_model="llm-m")
    assert llm.run_ladder("p", cfg) == ("claude", "ok")
    assert seen["model"] == "score-m"  # score_model wins when set
