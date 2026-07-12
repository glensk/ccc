"""Desktop notifications — ``auto`` platform resolution + the notify-send (Linux) path.

All external commands (osascript / notify-send) are mocked; nothing real is fired.
"""

from __future__ import annotations

import pytest

from command_center import notify


def test_resolve_auto_to_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    assert notify.resolve_channels(["auto"]) == ["macos"]
    assert notify.resolve_channels(["auto", "slack"]) == ["macos", "slack"]


def test_resolve_auto_to_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify.sys, "platform", "linux")
    assert notify.resolve_channels(["auto"]) == ["linux"]


def test_explicit_channels_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify.sys, "platform", "linux")
    assert notify.resolve_channels(["macos", "slack"]) == ["macos", "slack"]


def test_auto_fires_notify_send_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(notify.sys, "platform", "linux")
    monkeypatch.setattr(notify.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(cmd, **_kw):
        calls.append(cmd)

    monkeypatch.setattr(notify.subprocess, "run", fake_run)
    notify.notify("Title", "Body", ["auto"])
    assert len(calls) == 1
    assert calls[0][0] == "notify-send"
    assert "Title" in calls[0] and "Body" in calls[0]


def test_notify_send_missing_is_silent_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify.sys, "platform", "linux")
    monkeypatch.setattr(notify.shutil, "which", lambda _name: None)  # no notify-send
    ran = False

    def fake_run(*_a, **_k):
        nonlocal ran
        ran = True

    monkeypatch.setattr(notify.subprocess, "run", fake_run)
    notify.notify("Title", "Body", ["auto"])  # must not raise
    assert ran is False  # no subprocess launched


def test_auto_fires_osascript_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    monkeypatch.setattr(notify.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(notify.subprocess, "run", lambda cmd, **_kw: calls.append(cmd))
    notify.notify("Title", "Body", ["auto"])
    assert calls and calls[0][0] == "osascript"


def test_notify_never_raises_on_subprocess_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify.sys, "platform", "linux")
    monkeypatch.setattr(notify.shutil, "which", lambda name: f"/usr/bin/{name}")

    def boom(*_a, **_k):
        raise OSError("nope")

    monkeypatch.setattr(notify.subprocess, "run", boom)
    notify.notify("Title", "Body", ["auto"])  # swallowed, no raise
