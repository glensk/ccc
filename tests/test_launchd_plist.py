"""Tests for the generated WatchPaths future-sync launchd agent plist.

Unlike the periodic ``ccc daemon`` agent, the future-sync agent is WatchPaths-triggered.
It is now generated from config by :func:`command_center.launchd.future_sync_plist` (label,
watch path, log path and binary all resolved at generation time) rather than shipped as a
static hand-authored file. These tests pin the generated plist's shape and the keys the
launchd wiring depends on.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from command_center import config, launchd


def _cfg(tmp_path: Path) -> config.Config:
    vault = tmp_path / "vault"
    return config.Config(
        launchd_label="com.test.ccc",
        vault_root=str(vault),
        future_dir=str(vault / "01-llm-tasks" / "future"),
    )


@pytest.fixture
def plist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    xml = launchd.future_sync_plist(_cfg(tmp_path))
    return plistlib.loads(xml.encode("utf-8"))


def test_plist_parses(plist: dict) -> None:
    assert isinstance(plist, dict)


def test_label_derives_from_config(plist: dict) -> None:
    # The future-sync label is the configured launchd_label plus a "-future-sync" suffix.
    assert plist["Label"] == "com.test.ccc-future-sync"
    assert launchd.future_sync_label(_cfg(Path("/tmp"))) == "com.test.ccc-future-sync"


def test_program_arguments_run_sync_future(plist: dict) -> None:
    args = plist["ProgramArguments"]
    assert args[1] == "sync-future"
    assert args[0].endswith("ccc")


def test_watch_paths_covers_future_task_root(tmp_path: Path, plist: dict) -> None:
    # Watches the parent of future_dir — the vault's task-files root — so any
    # future/running/done edit triggers a sync.
    assert plist["WatchPaths"] == [str(tmp_path / "vault" / "01-llm-tasks")]


def test_throttle_interval_set(plist: dict) -> None:
    assert plist["ThrottleInterval"] == 10


def test_run_at_load_true(plist: dict) -> None:
    assert plist["RunAtLoad"] is True


def test_environment_guards_present(plist: dict) -> None:
    env = plist["EnvironmentVariables"]
    assert env["CCC_INTERNAL"] == "1"
    assert env["AI_NO_AUTOCOMMIT"] == "1"
    assert "PATH" in env and "HOME" in env


def test_log_paths_under_command_center_home(tmp_path: Path, plist: dict) -> None:
    app_home = str(tmp_path / "home" / "command-center")
    assert plist["StandardOutPath"].startswith(app_home)
    assert plist["StandardErrorPath"].startswith(app_home)
    assert plist["StandardOutPath"].endswith("future-sync.log")


def test_daemon_plist_label_derives_from_config(tmp_path: Path) -> None:
    # The periodic daemon agent's label is likewise config-driven.
    xml = launchd.plist_content("/x/ccc", 300, tmp_path, agent_label="com.test.ccc")
    data = plistlib.loads(xml.encode("utf-8"))
    assert data["Label"] == "com.test.ccc"
    assert data["ProgramArguments"] == ["/x/ccc", "daemon"]
