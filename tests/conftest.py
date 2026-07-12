"""Shared test fixtures — hermetic isolation from the real machine.

The per-tab badge system (:mod:`command_center.tabsymbol`) is filesystem-backed:
``assign``/``read`` persist one small file per iTerm tab under
``~/.cache/iterm-tab-symbol`` (overridable via ``CCC_TAB_SYMBOL_DIR``), and the
hook / daemon / TUI paths push ``"<badge> <leaf>"`` to the *live* iTerm tab whose
``$ITERM_SESSION_ID`` they read from the environment.

Without isolation a test run leaks into the developer's real session:

* ``tabsymbol.assign`` writes into the real cache, claiming a real palette slot.
  With the palette near capacity this forces a reclaim of a real tab's badge,
  reshuffling the cache so live tab **titles** (set on the last ``cd``/seed) no
  longer match the row the command center shows — the exact "tab symbols disagree"
  bug.
* the SessionStart hook reads the *tester's own* ``$ITERM_SESSION_ID`` and pushes
  a ``"<badge> repo"`` title onto the real tab the suite is running in.

This autouse fixture redirects the cache to a throwaway dir and unsets
``ITERM_SESSION_ID`` for every test, so the suite can neither pollute the real
badge cache nor drive real iTerm tabs. Tests that exercise the badge code on
purpose (``test_tabsymbol``, ``test_tui``) still point the cache at their own
``tmp_path`` — that simply re-overrides this default, which is fine.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_tab_symbol_side_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off the real badge cache and the real iTerm session."""
    monkeypatch.setenv("CCC_TAB_SYMBOL_DIR", str(tmp_path / "iterm-tab-symbol"))
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)


@pytest.fixture(autouse=True)
def _isolate_peek_focus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off the real iTerm focus probe (peek's ccc-TUI-row fallback).

    ``peek.resolve_peek`` now checks whether the focused tab is the live ccc TUI
    (an AppleScript tty probe) BEFORE the uuid map. On a dev machine with a real
    TUI running that would fire osascript per test; pin the probe to ``None`` —
    tests that exercise the fallback re-patch ``peek._focused_tty`` themselves.
    """
    from command_center import peek as _peek

    monkeypatch.setattr(_peek, "_focused_tty", lambda: None)


@pytest.fixture(autouse=True)
def _pin_single_claude_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off the developer's REAL Claude accounts.

    21 test files never set ``CLAUDE_HOME``, so an un-pinned ``load_config()`` would
    read the developer's real ``config.toml`` and could pick up their real
    ``claude_accounts`` — routing usage writes at the actual ``~/.claude`` tree. Pin
    ``config.claude_config_dirs`` to the single default account (``{"private":
    claude_home()}``, which honours each test's tmp ``CLAUDE_HOME``) and clear
    ``CLAUDE_CONFIG_DIR`` so ``_account_from_env`` deterministically falls back to the
    sole account. Also clear ``CCC_HOME`` so ``app_home()`` (now routed through
    ``ccc_home()``) still resolves under each test's tmp ``CLAUDE_HOME`` rather than a
    stray value in the developer's shell. Tests needing multiple accounts override this
    fixture explicitly.
    """
    from command_center import config as _config

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CCC_HOME", raising=False)
    monkeypatch.setattr(_config, "claude_config_dirs", lambda: {"private": _config.claude_home()})


@pytest.fixture(autouse=True)
def _isolate_vault_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off the real Obsidian vault.

    The future-job files and the running/done mirrors default to real paths under
    ``~/obsidian/01-llm-tasks/``. A test that reaches ``config.load_config()``
    indirectly (e.g. ``daemon.run_once``, CLI handlers) would otherwise export
    its fixture sessions into the developer's actual vault — which happened once:
    daemon tests isolated the store via ``CLAUDE_HOME`` but ``run_once``'s
    internal ``load_config()`` still pointed the new mirror pass at the real
    dirs, replacing the real mirrors with fixture jobs until the daemon healed
    them. This wrapper rewrites the five vault-path keys on every loaded config;
    tests that build ``Config(...)`` explicitly keep their own values (and the
    resolver-side guard in :mod:`command_center.config` fails loudly for any
    future leak vector this wrapper cannot see).

    ``repo_root`` is blanked for the same reason: it outranks ``$GIT_BASE`` in
    :func:`command_center.repos.repo_root`, so on a machine whose real config sets
    it, every fixture that points ``$GIT_BASE`` at a fake tree (``test_repos``,
    ``test_core``) would silently resolve against the developer's actual repo tree.
    Blanking it makes the ``$GIT_BASE`` monkeypatch the effective knob under test.
    """
    from command_center import config as _config

    real_load = _config.load_config

    def _tmp_vaulted() -> _config.Config:
        cfg = real_load()
        vault = tmp_path / "vault"
        cfg.vault_root = str(vault)
        cfg.future_dir = str(vault / "01-llm-tasks" / "future")
        cfg.delete_dir = str(vault / "01-llm-tasks" / "delete")
        cfg.future_pad = str(vault / "01-llm-tasks" / "new-prompt.md")
        cfg.running_dir = str(vault / "01-llm-tasks" / "running")
        cfg.done_dir = str(vault / "01-llm-tasks" / "done")
        cfg.sessions_dir = str(vault / "01-llm-tasks" / "sessions")
        cfg.repo_root = ""  # let each test's $GIT_BASE monkeypatch govern the repo tree
        return cfg

    monkeypatch.setattr(_config, "load_config", _tmp_vaulted)
