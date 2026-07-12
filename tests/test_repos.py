"""Tests for repo-tree category/repo discovery (future-job repo picker)."""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import config, repos


@pytest.fixture
def fake_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repo tree with two categories and a few repos, plus tooling dirs."""
    base = tmp_path / "tree"
    for rel in ("infra/network-apis", "sdsc/runai-cscs", "sdsc/zoho-api"):
        (base / rel).mkdir(parents=True)
    (base / ".git").mkdir(parents=True)  # tooling dir — must be skipped
    (base / "sdsc" / ".mypy_cache").mkdir(parents=True)  # tooling dir — must be skipped
    monkeypatch.setenv("GIT_BASE", str(base))
    return base


def test_git_base_honours_env(fake_tree: Path) -> None:
    assert repos.git_base() == fake_tree


def test_repo_root_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # config.repo_root wins over $GIT_BASE; $GIT_BASE is the fallback; neither ⇒ "".
    monkeypatch.delenv("GIT_BASE", raising=False)
    assert repos.repo_root(config.Config(repo_root="")) == ""

    monkeypatch.setenv("GIT_BASE", str(tmp_path / "envtree"))
    assert repos.repo_root(config.Config(repo_root="")) == str(tmp_path / "envtree")
    # explicit config value overrides the env
    assert repos.repo_root(config.Config(repo_root=str(tmp_path / "cfgtree"))) == str(
        tmp_path / "cfgtree"
    )


def test_categories_skips_tooling_dirs(fake_tree: Path) -> None:
    assert repos.categories() == ["infra", "sdsc"]  # sorted; .git excluded


def test_categories_empty_without_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    # No repo_root and no $GIT_BASE ⇒ no category tree, no crash.
    monkeypatch.delenv("GIT_BASE", raising=False)
    monkeypatch.setattr(config, "load_config", lambda: config.Config(repo_root=""))
    assert repos.repo_root() == ""
    assert repos.categories() == []


def test_repos_in_category(fake_tree: Path) -> None:
    assert repos.repos_in("sdsc") == ["runai-cscs", "zoho-api"]  # .mypy_cache excluded


def test_repo_path(fake_tree: Path) -> None:
    assert repos.repo_path("sdsc", "zoho-api") == fake_tree / "sdsc" / "zoho-api"


def test_category_of_with_root(fake_tree: Path) -> None:
    root = str(fake_tree)
    # (category, leaf) under the tree; deeper sub-folders keep their tail.
    assert repos.category_of(str(fake_tree / "sdsc" / "zoho-api"), root) == ("sdsc", "zoho-api")
    assert repos.category_of(str(fake_tree / "sdsc" / "runai-cscs" / "tickets"), root) == (
        "sdsc",
        "runai-cscs/tickets",
    )
    # a bare category dir → (category, category); outside the tree / the root itself → None.
    assert repos.category_of(str(fake_tree / "sdsc"), root) == ("sdsc", "sdsc")
    assert repos.category_of("/tmp/elsewhere", root) is None
    assert repos.category_of(root, root) is None


def test_category_of_empty_root_is_none(fake_tree: Path) -> None:
    # With no configured tree every path is outside → the "others" bucket.
    assert repos.category_of(str(fake_tree / "sdsc" / "zoho-api"), "") is None
    assert repos.category_of("", str(fake_tree)) is None


def test_parse_repo_path_from_create_args(fake_tree: Path) -> None:
    # `<category> <name> [flags]` → <repo_root>/<category>/<name>
    assert repos.parse_repo_path("sdsc zoho-api --private -w github") == (
        fake_tree / "sdsc" / "zoho-api"
    )
    assert repos.parse_repo_path("--private only-flags") is None  # <2 positionals


def test_create_repo_disabled_without_command() -> None:
    ok, msg = repos.create_repo("sdsc zoho-api", config.Config(create_repo_command=""))
    assert ok is False
    assert "create_repo_command" in msg


def test_create_repo_runs_template(tmp_path: Path) -> None:
    marker = tmp_path / "created.txt"
    cfg = config.Config(create_repo_command=f"printf '%s %s' {{category}} {{name}} > {marker}")
    ok, _out = repos.create_repo("sdsc zoho-api", cfg)
    assert ok is True
    assert marker.read_text(encoding="utf-8") == "sdsc zoho-api"
