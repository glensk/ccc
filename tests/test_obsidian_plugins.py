"""``ccc obsidian-setup --install-plugins`` — pinned manifest + mocked download path.

The committed ``assets/obsidian/plugins.json`` is validated for shape + real sha256 hex +
the Meta Bind 1.4.1 pin. The download/verify/write path is exercised with a fully mocked
downloader (no network): a matching run writes the plugin files and enables them, a sha256
mismatch aborts and leaves the vault untouched, and ``--dry-run`` downloads nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from command_center import config, obsidian

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


# ------------------------------ committed manifest ------------------------------ #
def test_real_manifest_schema_and_pins() -> None:
    manifest = obsidian.load_plugins_manifest()
    plugins = {p["id"]: p for p in manifest["plugins"]}
    assert set(obsidian.REQUIRED_PLUGINS) <= set(plugins)
    # Meta Bind is pinned to 1.4.1: 1.5.1 requires Obsidian >= 1.13.1 (see the entry's "note"),
    # but the current release line is 1.12.x, where 1.5.1 never registers.
    assert plugins["obsidian-meta-bind-plugin"]["version"] == "1.4.1"
    # An extra descriptive "note" key must be tolerated by the loader (it ignores unknown keys).
    assert plugins["obsidian-meta-bind-plugin"]["note"]
    for plugin in manifest["plugins"]:
        assert plugin["version"] and plugin["repo"]
        names = {f["name"] for f in plugin["files"]}
        assert "main.js" in names and "manifest.json" in names
        for spec in plugin["files"]:
            assert spec["url"].startswith("https://github.com/")
            assert _HEX64.match(spec["sha256"]), spec
            assert isinstance(spec["bytes"], int) and spec["bytes"] > 0


# ------------------------------ mocked download ------------------------------ #
def _fake_manifest() -> tuple[dict, dict[str, bytes]]:
    """A 2-plugin manifest with real sha256s + the url→bytes the fake downloader serves."""
    contents = {
        "plug-a": {"main.js": b"AAA-main", "manifest.json": b'{"id":"plug-a"}'},
        "plug-b": {"main.js": b"BBB-main", "styles.css": b".x{}"},
    }
    plugins = []
    url_bytes: dict[str, bytes] = {}
    for pid, files in contents.items():
        specs = []
        for name, data in files.items():
            url = f"https://github.com/o/{pid}/releases/download/1.0.0/{name}"
            url_bytes[url] = data
            specs.append(
                {
                    "name": name,
                    "url": url,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                }
            )
        plugins.append(
            {"id": pid, "version": "1.0.0", "repo": f"o/{pid}", "reason": "t", "files": specs}
        )
    return {"schema": 1, "plugins": plugins}, url_bytes


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / ".obsidian").mkdir(parents=True)
    return v


def test_install_plugins_writes_files_and_enables(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, url_bytes = _fake_manifest()
    monkeypatch.setattr(obsidian, "load_plugins_manifest", lambda: manifest)
    rc = obsidian.run_install_plugins(
        config.load_config(), vault, yes=True, downloader=lambda url: url_bytes[url]
    )
    assert rc == 0
    assert (vault / ".obsidian/plugins/plug-a/main.js").read_bytes() == b"AAA-main"
    assert (vault / ".obsidian/plugins/plug-b/styles.css").read_bytes() == b".x{}"
    enabled = json.loads((vault / ".obsidian/community-plugins.json").read_text(encoding="utf-8"))
    assert "plug-a" in enabled and "plug-b" in enabled


def test_install_plugins_tolerates_extra_note_key(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An extra descriptive "note" key on a plugin entry (like the Meta Bind pin rationale in
    # the committed manifest) must be ignored by the loader, not break download/verify/write.
    manifest, url_bytes = _fake_manifest()
    for plugin in manifest["plugins"]:
        plugin["note"] = "pinned for a reason"
    monkeypatch.setattr(obsidian, "load_plugins_manifest", lambda: manifest)
    rc = obsidian.run_install_plugins(
        config.load_config(), vault, yes=True, downloader=lambda url: url_bytes[url]
    )
    assert rc == 0
    assert (vault / ".obsidian/plugins/plug-a/main.js").read_bytes() == b"AAA-main"


def test_install_plugins_sha256_mismatch_rolls_back(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, url_bytes = _fake_manifest()
    monkeypatch.setattr(obsidian, "load_plugins_manifest", lambda: manifest)
    # Pre-existing community-plugins.json must survive a failed install untouched.
    community = vault / ".obsidian/community-plugins.json"
    community.write_text('["already-here"]', encoding="utf-8")

    def _bad(url: str) -> bytes:
        return (
            b"TAMPERED"
            if url.endswith("plug-b/releases/download/1.0.0/main.js")
            else url_bytes[url]
        )

    rc = obsidian.run_install_plugins(config.load_config(), vault, yes=True, downloader=_bad)
    assert rc == 1
    # Verify-before-write: nothing was written for either plugin.
    assert not (vault / ".obsidian/plugins/plug-a").exists()
    assert not (vault / ".obsidian/plugins/plug-b").exists()
    assert community.read_text(encoding="utf-8") == '["already-here"]'


def test_install_plugins_dry_run_downloads_nothing(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _ = _fake_manifest()
    monkeypatch.setattr(obsidian, "load_plugins_manifest", lambda: manifest)

    def _explode(_url: str) -> bytes:
        raise AssertionError("downloader must not be called in --dry-run")

    rc = obsidian.run_install_plugins(
        config.load_config(), vault, yes=True, dry_run=True, downloader=_explode
    )
    assert rc == 0
    assert not (vault / ".obsidian/plugins").exists()


def test_install_plugins_non_interactive_without_yes_exits_3(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, url_bytes = _fake_manifest()
    monkeypatch.setattr(obsidian, "load_plugins_manifest", lambda: manifest)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = obsidian.run_install_plugins(
        config.load_config(), vault, yes=False, downloader=lambda url: url_bytes[url]
    )
    assert rc == 3
