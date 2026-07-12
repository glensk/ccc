"""Tests for the typed @tag registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import tags


def test_tag_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    tags._load.cache_clear()  # the registry is process-cached; reset for this temp home

    # Defaults are seeded on first use.
    type_map = tags.types()
    assert tags.tag_style("waiting") == type_map["status"]
    assert tags.tag_style("susi") == type_map["people"]
    assert tags.tag_style("galaxus") == type_map["place"]
    assert tags.tag_style("not_a_defined_tag") is None  # unknown -> caller uses UNKNOWN_STYLE
    assert "@home" in tags.known_tags()

    # Define a new type + tag and confirm they round-trip via the TOML file.
    tags.add_type("project", "magenta")
    tags.add_tag("@ccc", "project")
    assert tags.tag_style("ccc") == "magenta"
    assert "@ccc" in tags.known_tags()
