"""Unit tests for short-AIM label generation + sanitation."""

from __future__ import annotations

import pytest

from command_center import short_aim


def test_sanitize_strips_quotes_bullets_and_period() -> None:
    assert short_aim._sanitize('  "Implement short aim."  ') == "Implement short aim"
    assert short_aim._sanitize("- implement x") == "implement x"
    assert short_aim._sanitize("`maria: ws reconnect`") == "maria: ws reconnect"


def test_sanitize_takes_first_nonempty_line() -> None:
    assert short_aim._sanitize("\n\nimplement x\nsome chatter after") == "implement x"


def test_sanitize_caps_length() -> None:
    out = short_aim._sanitize("implement " + "x" * 200)
    assert out is not None and len(out) <= short_aim._MAX_CHARS


def test_sanitize_unwraps_fence_with_language_tag() -> None:
    """A reply wrapped in ```json … ``` yields the label, not the literal 'json'."""
    assert short_aim._sanitize("```json\nfix the login bug\n```") == "fix the login bug"


def test_sanitize_unwraps_fence_without_language_tag() -> None:
    """A bare ``` fence unwraps too (used to sanitize down to None)."""
    assert short_aim._sanitize("```\nfix the login bug\n```") == "fix the login bug"


def test_sanitize_plain_reply_unchanged() -> None:
    """An unfenced reply passes through the fence stripper untouched."""
    assert short_aim._sanitize("fix the login bug") == "fix the login bug"


def test_sanitize_content_starting_with_backtick_is_not_a_fence() -> None:
    """Inline-backtick CONTENT survives the fence stripper (only pure ``` lines count)."""
    assert short_aim._sanitize("`fix ccc jump toggle`\nchatter after") == "fix ccc jump toggle"


def test_sanitize_none_on_empty() -> None:
    assert short_aim._sanitize(None) is None
    assert short_aim._sanitize("   ") is None
    assert short_aim._sanitize('""') is None


def test_generate_none_on_empty_aim() -> None:
    assert short_aim.generate(None) is None
    assert short_aim.generate("  ") is None


def test_generate_codex_backend_routes_to_run_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import llm

    seen: dict[str, str] = {}

    def fake_codex(prompt: str, model: str = "") -> str:
        seen["prompt"] = prompt
        seen["model"] = model
        return '"implement short aim"\n'

    monkeypatch.setattr(llm, "run_codex", fake_codex)
    monkeypatch.setattr(llm, "run_model", lambda *_a, **_k: "WRONG — claude path used")
    out = short_aim.generate("show a short aim in ccc", backend="codex", model="m1")
    assert out == "implement short aim"  # sanitized
    assert seen["model"] == "m1"
    assert "show a short aim in ccc" in seen["prompt"]  # the AIM is in the prompt


def test_generate_claude_backend_routes_to_run_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import llm

    monkeypatch.setattr(llm, "run_model", lambda *_a, **_k: "fix login bug")
    monkeypatch.setattr(llm, "run_codex", lambda *_a, **_k: "WRONG — codex path used")
    assert short_aim.generate("the login bug is fixed", backend="claude") == "fix login bug"


def test_generate_claude_backend_passes_short_aim_purpose(monkeypatch: pytest.MonkeyPatch) -> None:
    """The claude backend labels its ai.py call ``short-aim`` and notes the original AIM."""
    from command_center import llm

    seen: dict[str, object] = {}

    def _capture(_prompt: str, _model: str, *, purpose: str = "", note: str = "") -> str:
        seen["purpose"] = purpose
        seen["note"] = note
        return "fix login bug"

    monkeypatch.setattr(llm, "run_model", _capture)
    monkeypatch.setattr(llm, "run_codex", lambda *_a, **_k: "WRONG — codex path used")
    out = short_aim.generate(
        "the login bug in the auth flow is fixed", original="fix login", backend="claude"
    )
    assert out == "fix login bug"
    assert seen["purpose"] == "short-aim"
    assert seen["note"] == "fix login"  # concise first AIM, log context only


def test_generate_none_on_backend_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import llm

    monkeypatch.setattr(llm, "run_codex", lambda *_a, **_k: None)
    assert short_aim.generate("ship it", backend="codex") is None


def test_original_hint_included_only_when_distinct() -> None:
    assert short_aim._original_hint("same aim", "same aim") == ""
    assert short_aim._original_hint("a", None) == ""
    hint = short_aim._original_hint("a long sharpened aim", "short original")
    assert "short original" in hint
