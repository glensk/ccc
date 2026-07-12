"""`ti` / `ccc toggle-idle` flip Claude Code's `agentPushNotifEnabled` safely.

Covers the read/toggle/force paths, byte-preserving surgical edits, the
absent-key JSON fallback, and — critically — that a stow-managed symlink at
``~/.claude/settings.json`` is written *through* (never replaced by a plain file).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from command_center import idlenotify


@pytest.fixture(autouse=True)
def _claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point CLAUDE_HOME at a tmp dir so settings.json is a throwaway file."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    return tmp_path


def _write_settings(home: Path, text: str) -> Path:
    path = home / "settings.json"
    path.write_text(text, encoding="utf-8")
    return path


def test_is_enabled_reads_the_flag(_claude_home: Path) -> None:
    _write_settings(_claude_home, json.dumps({"agentPushNotifEnabled": True}))
    assert idlenotify.is_enabled() is True
    _write_settings(_claude_home, json.dumps({"agentPushNotifEnabled": False}))
    assert idlenotify.is_enabled() is False


def test_missing_file_or_key_defaults_on(_claude_home: Path) -> None:
    # No file at all → treated as ON (so a first press mutes).
    assert idlenotify.is_enabled() is True
    # File present but key absent → also ON.
    _write_settings(_claude_home, json.dumps({"other": 1}))
    assert idlenotify.is_enabled() is True


def test_toggle_flips_and_returns_new_state(_claude_home: Path) -> None:
    _write_settings(_claude_home, json.dumps({"agentPushNotifEnabled": True}))
    assert idlenotify.toggle() is False
    assert idlenotify.is_enabled() is False
    assert idlenotify.toggle() is True
    assert idlenotify.is_enabled() is True


def test_set_enabled_forces_state(_claude_home: Path) -> None:
    _write_settings(_claude_home, json.dumps({"agentPushNotifEnabled": True}))
    idlenotify.set_enabled(False)
    assert idlenotify.is_enabled() is False
    idlenotify.set_enabled(True)
    assert idlenotify.is_enabled() is True


def test_surgical_edit_preserves_other_keys_and_formatting(_claude_home: Path) -> None:
    """Only the one boolean token changes; the rest of the file is byte-identical."""
    original = (
        "{\n"
        '  "agentPushNotifEnabled": true,\n'
        '  "hooks": {"Stop": ["keep me exactly as-is"]},\n'
        '  "trailingKey": 42\n'
        "}\n"
    )
    path = _write_settings(_claude_home, original)
    idlenotify.set_enabled(False)
    updated = path.read_text(encoding="utf-8")
    assert updated == original.replace(
        '"agentPushNotifEnabled": true', '"agentPushNotifEnabled": false'
    )
    # Still valid JSON with everything else intact.
    data = json.loads(updated)
    assert data["hooks"] == {"Stop": ["keep me exactly as-is"]}
    assert data["trailingKey"] == 42


def test_absent_key_inserted_via_json_fallback(_claude_home: Path) -> None:
    path = _write_settings(_claude_home, json.dumps({"other": 1}))
    idlenotify.set_enabled(False)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"other": 1, "agentPushNotifEnabled": False}


def test_write_through_symlink_keeps_the_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stow-style symlink must survive the atomic write (write the target, keep the link)."""
    home = tmp_path / "claude_home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))

    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir(parents=True)
    real.write_text(json.dumps({"agentPushNotifEnabled": True}) + "\n", encoding="utf-8")

    link = home / "settings.json"
    link.symlink_to(real)

    idlenotify.set_enabled(False)

    assert link.is_symlink(), "the symlink was replaced by a regular file"
    assert link.resolve() == real
    assert json.loads(real.read_text(encoding="utf-8"))["agentPushNotifEnabled"] is False
