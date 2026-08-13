"""Live tab-colour dedupe: colliding OPEN tabs get distinct, cached, sticky colours."""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import colors, tabcolor
from command_center.models import Session


@pytest.fixture(autouse=True)
def _tmp_rgb_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the per-tab colour cache (reader AND writer) at a temp dir."""
    directory = tmp_path / "iterm-tab-rgb"
    monkeypatch.setenv("CCC_TAB_RGB_DIR", str(directory))
    return directory


def _session(sid: str, iterm: str | None, cwd: str = "/repo/a", created_at: int = 0) -> Session:
    return Session(session_id=sid, cwd=cwd, iterm_session_id=iterm, created_at=created_at)


def test_same_folder_colour_recolours_all_but_the_oldest(
    _tmp_rgb_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two open tabs on one repo colour: the oldest keeps it, the other takes a palette slot."""
    monkeypatch.setattr(colors, "folder_rgb", lambda _cwd: (7, 8, 9))  # no per-tab cache yet
    first = _session("aaaa", "w0t0p0:A", created_at=1000)
    second = _session("bbbb", "w0t1p0:B", created_at=2000)

    recoloured = tabcolor.dedupe_live([first, second])

    assert recoloured == ["w0t1p0_B"]  # the later-created tab moved, not the anchor
    assert not (_tmp_rgb_dir / "w0t0p0_A").exists()  # the anchor was never written
    assigned = colors.tab_rgb("w0t1p0:B")
    assert assigned in tabcolor.PALETTE and assigned != (7, 8, 9)
    # The `.manual` marker is what makes the status line follow the cache and repaint the
    # real tab to it, so the row, the tab and the status line agree.
    assert (_tmp_rgb_dir / "w0t1p0_B.manual").exists()


def test_second_pass_is_a_no_op(_tmp_rgb_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Idempotent: the recoloured tab now resolves distinct, so nothing is rewritten."""
    monkeypatch.setattr(colors, "folder_rgb", lambda _cwd: (7, 8, 9))
    sessions = [
        _session("aaaa", "w0t0p0:A", created_at=1000),
        _session("bbbb", "w0t1p0:B", created_at=2000),
    ]
    assert tabcolor.dedupe_live(sessions) == ["w0t1p0_B"]
    before = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in _tmp_rgb_dir.iterdir()
    }

    assert tabcolor.dedupe_live(sessions) == []
    after = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in _tmp_rgb_dir.iterdir()
    }
    assert after == before  # no rewrite (bytes AND mtimes untouched)


def test_distinct_colours_are_left_alone(_tmp_rgb_dir: Path) -> None:
    """No collision, no writes — the cache is only ever touched to break a tie."""
    _tmp_rgb_dir.mkdir(parents=True)
    (_tmp_rgb_dir / "w0t0p0_A").write_text("10;20;30\n", encoding="utf-8")
    (_tmp_rgb_dir / "w0t1p0_B").write_text("40;50;60\n", encoding="utf-8")
    sessions = [
        _session("aaaa", "w0t0p0:A", created_at=1000),
        _session("bbbb", "w0t1p0:B", created_at=2000),
    ]

    assert tabcolor.dedupe_live(sessions) == []
    assert colors.tab_rgb("w0t0p0:A") == (10, 20, 30)
    assert colors.tab_rgb("w0t1p0:B") == (40, 50, 60)
    assert not (_tmp_rgb_dir / "w0t0p0_A.manual").exists()


def test_sessions_without_a_tab_or_colour_are_skipped(
    _tmp_rgb_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``iterm_session_id`` (nothing to key the cache on) → never touched, never counted."""
    monkeypatch.setattr(colors, "folder_rgb", lambda _cwd: (7, 8, 9))
    assert tabcolor.dedupe_live([_session("aaaa", None), _session("bbbb", "")]) == []
    assert not _tmp_rgb_dir.exists()  # not even created

    # An unmapped folder with no cached colour resolves to nothing to collide with.
    monkeypatch.setattr(colors, "folder_rgb", lambda _cwd: None)
    assert tabcolor.dedupe_live([_session("aaaa", "w0t0p0:A"), _session("bbbb", "w0t1p0:B")]) == []
    assert not _tmp_rgb_dir.exists()


def test_two_sessions_in_one_tab_never_collide_with_themselves(
    _tmp_rgb_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One tab holding two sessions is ONE colour — otherwise it would churn every pass."""
    monkeypatch.setattr(colors, "folder_rgb", lambda _cwd: (7, 8, 9))
    same_tab = [
        _session("aaaa", "w0t0p0:A", created_at=1000),
        _session("bbbb", "w0t0p0:A", created_at=2000),  # nested claude / resumed in-place
    ]

    assert tabcolor.dedupe_live(same_tab) == []
    assert not _tmp_rgb_dir.exists()


def test_palette_exhaustion_leaves_the_remainder_alone(
    _tmp_rgb_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """More colliding tabs than palette slots: the surplus keeps the shared colour."""
    monkeypatch.setattr(colors, "folder_rgb", lambda _cwd: (7, 8, 9))
    count = len(tabcolor.PALETTE) + 3
    sessions = [_session(f"s{i:03d}", f"w0t{i}p0:T{i}", created_at=1000 + i) for i in range(count)]

    recoloured = tabcolor.dedupe_live(sessions)

    assert len(recoloured) == len(tabcolor.PALETTE)  # every slot claimed, nothing beyond
    assigned = [colors.tab_rgb(f"w0t{i}p0:T{i}") for i in range(count)]
    assert set(assigned[1:]) - {None} == set(tabcolor.PALETTE)  # each slot used exactly once
    # Never written (→ still the shared repo colour): the oldest tab, plus the 2 surplus ones
    # left over once the palette ran out (19 tabs = 1 anchor + 16 slots + 2).
    assert [rgb for rgb in assigned if rgb is None] == [None] * 3


def test_palette_is_distinct_and_ample() -> None:
    assert len(set(tabcolor.PALETTE)) == len(tabcolor.PALETTE)  # no duplicate colours
    assert len(tabcolor.PALETTE) >= 12  # enough slots for a busy screen of same-repo tabs
    assert all(len(rgb) == 3 and all(0 <= c <= 255 for c in rgb) for rgb in tabcolor.PALETTE)
