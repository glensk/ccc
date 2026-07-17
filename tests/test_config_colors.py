"""Tests for config save/load round-trip and folder shortening."""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import colors, config

# The autouse conftest fixture pins ``config.claude_config_dirs``; capture the real
# implementation at import (before any fixture runs) so these tests can exercise it.
_real_claude_config_dirs = config.claude_config_dirs


def test_config_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    cfg = config.load_config()
    cfg.idle_timeout_min = 30
    cfg.nag_every_n_turns = 0
    cfg.daemon_interval_sec = 600
    cfg.reap = False
    config.save_config(cfg)

    reloaded = config.load_config()
    assert reloaded.idle_timeout_min == 30
    assert reloaded.nag_every_n_turns == 0
    assert reloaded.daemon_interval_sec == 600
    assert reloaded.reap is False


def test_usage_refresh_config_keys_defaults_and_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The usage-card cadence keys exist in BOTH DEFAULTS and the dataclass (two-place
    # pattern), carry the documented defaults, and survive a save/load round-trip.
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    for key in (
        "usage_refresh_sec",
        "copilot_usage_refresh_sec",
        "copilot_usage_refresh_active_sec",
    ):
        assert key in config.DEFAULTS
        assert hasattr(config.Config(), key)
    fresh = config.load_config()
    assert fresh.usage_refresh_sec == 5.0
    assert fresh.copilot_usage_refresh_sec == 900
    assert fresh.copilot_usage_refresh_active_sec == 300

    fresh.usage_refresh_sec = 3.0
    fresh.copilot_usage_refresh_active_sec = 120
    config.save_config(fresh)
    reloaded = config.load_config()
    assert reloaded.usage_refresh_sec == 3.0
    assert reloaded.copilot_usage_refresh_active_sec == 120
    assert reloaded.copilot_usage_refresh_sec == 900


def test_multi_account_config_keys_defaults_and_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The multi-account keys exist in BOTH DEFAULTS and the dataclass, and round-trip.

    ``claude_accounts`` is list[str] precisely so ``save_config`` (which serializes
    bool/int/float/list[str]/str) round-trips it.
    """
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    for key in (
        "claude_accounts",
        "usage_card_private",
        "usage_card_work",
        "usage_card_codex",
        "usage_card_copilot",
        "nixos_overseer_dir",
        "card_nixos_overseer_supervised",
        "card_nixos_overseer_tier_a",
        "llm_account",
    ):
        assert key in config.DEFAULTS
        assert hasattr(config.Config(), key)
    fresh = config.load_config()
    assert fresh.claude_accounts == []
    assert fresh.usage_card_private is True
    assert fresh.usage_card_work is True
    assert fresh.usage_card_codex is True
    assert fresh.usage_card_copilot is True
    # The nixos-overseer cards: dir defaults empty (feature off), supervised shown,
    # tier_a hidden — and all three round-trip through save/load.
    assert fresh.nixos_overseer_dir == ""
    assert fresh.card_nixos_overseer_supervised is True
    assert fresh.card_nixos_overseer_tier_a is False
    assert fresh.llm_account == "private"

    fresh.claude_accounts = ["private=~/.claude", "work=~/.claude-work"]
    fresh.usage_card_work = False
    fresh.usage_card_copilot = False
    fresh.nixos_overseer_dir = "~/overseer"
    fresh.card_nixos_overseer_supervised = False
    fresh.card_nixos_overseer_tier_a = True
    fresh.llm_account = "work"
    config.save_config(fresh)
    reloaded = config.load_config()
    assert reloaded.claude_accounts == ["private=~/.claude", "work=~/.claude-work"]
    assert reloaded.usage_card_work is False
    assert reloaded.usage_card_copilot is False
    assert reloaded.usage_card_private is True
    assert reloaded.nixos_overseer_dir == "~/overseer"
    assert reloaded.card_nixos_overseer_supervised is False
    assert reloaded.card_nixos_overseer_tier_a is True
    assert reloaded.llm_account == "work"


def test_claude_config_dirs_parses_validates_and_skips_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty → single private default; valid entries parse+resolve; bad ones are skipped."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))

    def _cfg_with(accounts: list[str]) -> config.Config:
        cfg = config.Config()
        cfg.claude_accounts = accounts
        return cfg

    # Empty → the single-account default.
    monkeypatch.setattr(config, "load_config", lambda: _cfg_with([]))
    assert _real_claude_config_dirs() == {"private": config.claude_home()}

    # Valid entries are parsed, expanduser()-ed and resolve()-d.
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: _cfg_with([f"private={tmp_path / 'priv'}", f"work={tmp_path / 'work'}"]),
    )
    dirs = _real_claude_config_dirs()
    assert set(dirs) == {"private", "work"}
    assert dirs["work"] == (tmp_path / "work").resolve()

    # Malformed entries are skipped without crashing: no '=', an uppercase label, a
    # label carrying a path separator, and an empty path — only the good one survives.
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: _cfg_with(
            [
                f"good={tmp_path / 'g'}",
                "noequals",
                f"Bad={tmp_path / 'x'}",
                f"pa/th={tmp_path / 'y'}",
                "empty=",
            ]
        ),
    )
    dirs = _real_claude_config_dirs()
    assert set(dirs) == {"good"}

    # If every entry is malformed, fall back to the single private default.
    monkeypatch.setattr(config, "load_config", lambda: _cfg_with(["nope", "Bad=/x"]))
    assert _real_claude_config_dirs() == {"private": config.claude_home()}


def test_short_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/tester")
    root = "/home/tester/repo-root"
    assert colors.short_folder(f"{root}/infra/network-apis", root) == "infra/network-apis"
    assert colors.short_folder("/home/tester", root) == "~"
    assert colors.short_folder("/home/tester/scratch", root) == "~/scratch"
    assert colors.short_folder("/etc/hosts", root) == "/etc/hosts"
    assert colors.short_folder("", root) == "?"


def test_folder_split(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/tester")
    base = "/home/tester/repo-root"
    # Under the repo tree: first segment is the category, the tail is the leaf.
    assert colors.folder_split(f"{base}/sdsc/runai-cscs", base) == ("sdsc", "runai-cscs")
    assert colors.folder_split(f"{base}/sdsc/runai-cscs/tickets", base) == (
        "sdsc",
        "runai-cscs/tickets",
    )
    # Outside the tree: one "others" category, leaf is the full home-relative path.
    assert colors.folder_split("/home/tester", base) == ("others", "~")
    assert colors.folder_split("/home/tester/scratch", base) == ("others", "~/scratch")
    assert colors.folder_split("/home/tester/a/b/c", base) == ("others", "~/a/b/c")
    assert colors.folder_split("/tmp/elsewhere", base) == ("others", "/tmp/elsewhere")
    # No configured tree ⇒ everything is "others" (leaf collapses $HOME to ~).
    assert colors.folder_split(f"{base}/sdsc/runai-cscs", "") == (
        "others",
        "~/repo-root/sdsc/runai-cscs",
    )


def test_category_color_palette_is_deterministic_and_overridable() -> None:
    # "others" (and the empty category) never get a colour.
    assert colors.category_color("others") is None
    assert colors.category_color("") is None
    # An explicit config override wins.
    cfg = config.Config(category_colors={"sdsc": "#123456"})
    assert colors.category_color("sdsc", cfg) == "#123456"
    # Unknown categories fall back to a stable, palette-bound colour (hash of the name),
    # identical across calls and drawn from the published palette.
    first = colors.category_color("brand-new-cat")
    assert first == colors.category_color("brand-new-cat")
    assert first in colors._CATEGORY_PALETTE


def test_folder_style_falls_back_to_category_palette() -> None:
    # With no tab-colour cache hit, a real category still gets a stable colour (not grey);
    # only an "others" folder falls through to grey70.
    base = "/home/tester/repo-root"
    styled = colors.folder_style(f"{base}/sdsc/repo-x", None, base)
    assert styled == colors.category_color("sdsc")
    assert colors.folder_style("/tmp/elsewhere", None, base) == "grey70"


def test_with_tags_preserves_text() -> None:
    from command_center.views.tui import _with_tags  # pylint: disable=import-outside-toplevel

    rendered = _with_tags("ping @susi re @waiting", "white")
    assert rendered.plain == "ping @susi re @waiting"
    # the @-tags carry a non-default style (a span exists for each)
    assert len(rendered.spans) >= 2


def test_parse_claude_accounts_is_pure_and_shares_semantics_with_claude_config_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`parse_claude_accounts` is the file-read-free half of `claude_config_dirs`.

    The TUI calls it on every 5 s render tick from an already-loaded Config, so it must
    not touch disk and must agree with `claude_config_dirs` on the same entries.
    """
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    entries = [f"private={tmp_path}/p", f"work={tmp_path}/w"]
    parsed = config.parse_claude_accounts(entries)
    assert set(parsed) == {"private", "work"}

    # Malformed entries are skipped, not fatal; an all-malformed list falls back.
    assert set(config.parse_claude_accounts(["nosep", "BAD=/x", "=/y", "ok="])) == {"private"}
    assert config.parse_claude_accounts([]) == {"private": config.claude_home()}
