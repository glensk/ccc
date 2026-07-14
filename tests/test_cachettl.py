"""Unit tests for the prompt-cache TTL countdown helper (:mod:`command_center.cachettl`).

Covers the colour boundaries (1200 green / 600 orange), the ``❄ cold`` expiry, the
``M:SS`` formatting, a missing transcript (empty cell), and the ``CC_CACHE_TTL_S``
override incl. junk-value fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import cachettl
from command_center.models import Session


def _cd(remaining: int, ttl: int = 3600) -> tuple[str, str]:
    """``countdown`` for an exact number of *remaining* seconds under *ttl* (mtime=0)."""
    return cachettl.countdown(mtime=0.0, now=float(ttl - remaining), ttl=ttl)


# --------------------------------------------------------------------------- boundaries
def test_countdown_green_at_and_above_1200() -> None:
    text, level = _cd(1200)
    assert level == "green"
    assert text == "♨ 20:00"
    assert _cd(3599)[1] == "green"


def test_countdown_orange_between_600_and_1200() -> None:
    assert _cd(1199) == ("♨ 19:59", "orange")
    assert _cd(600) == ("♨ 10:00", "orange")


def test_countdown_red_below_600_while_warm() -> None:
    assert _cd(599) == ("♨ 9:59", "red")
    assert _cd(1) == ("♨ 0:01", "red")


def test_countdown_cold_at_zero_and_negative() -> None:
    assert _cd(0) == ("❄ cold", "red")
    assert _cd(-5) == ("❄ cold", "red")


# --------------------------------------------------------------------------- formatting
def test_countdown_mmss_formatting() -> None:
    # Whole minutes (no zero-pad) + zero-padded seconds.
    assert _cd(3587)[0] == "♨ 59:47"
    assert _cd(187)[0] == "♨ 3:07"


# --------------------------------------------------------------------------- empty cell
def test_countdown_missing_transcript_is_empty() -> None:
    assert cachettl.countdown(None, 123.0) == ("", "")


# --------------------------------------------------------------------------- TTL / env
def test_cache_ttl_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CC_CACHE_TTL_S", raising=False)
    assert cachettl.cache_ttl_seconds() == 3600


def test_cache_ttl_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CC_CACHE_TTL_S", "1800")
    assert cachettl.cache_ttl_seconds() == 1800
    # With a 1800 s TTL, 600 s elapsed leaves 1200 s → green (the env drives countdown too).
    assert cachettl.countdown(mtime=0.0, now=600.0) == ("♨ 20:00", "green")


@pytest.mark.parametrize("junk", ["notanumber", "", "1800.5", "0", "-5"])
def test_cache_ttl_junk_falls_back_to_default(monkeypatch: pytest.MonkeyPatch, junk: str) -> None:
    monkeypatch.setenv("CC_CACHE_TTL_S", junk)
    assert cachettl.cache_ttl_seconds() == 3600


# --------------------------------------------------------------------------- adapter reach
class _FakeAdapter:
    """Minimal adapter exposing only ``transcript_path`` (the probed capability)."""

    def __init__(self, path: Path | None) -> None:
        self._path = path

    def transcript_path(
        self, cwd: str, session_id: str, config_dir: str | None = None
    ) -> Path | None:
        return self._path


def test_transcript_mtime_reads_stat(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}", encoding="utf-8")
    import os  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    os.utime(transcript, (1_000_000.0, 1_234_567.0))
    session = Session(session_id="s", cwd="/repo", aim="x")
    assert cachettl.transcript_mtime(_FakeAdapter(transcript), session) == 1_234_567.0


def test_transcript_mtime_none_when_no_path_or_capability(tmp_path: Path) -> None:
    session = Session(session_id="s", cwd="/repo", aim="x")
    assert cachettl.transcript_mtime(_FakeAdapter(None), session) is None
    assert cachettl.transcript_mtime(object(), session) is None
    assert cachettl.transcript_mtime(_FakeAdapter(tmp_path / "missing.jsonl"), session) is None


def test_countdown_for_warm_transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CC_CACHE_TTL_S", raising=False)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}", encoding="utf-8")
    import os  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    os.utime(transcript, (0.0, 100.0))
    session = Session(session_id="s", cwd="/repo", aim="x")
    # 100 s mtime, now 400 s → 300 s elapsed, 3300 s remaining → green.
    text, level = cachettl.countdown_for(_FakeAdapter(transcript), session, now=400.0)
    assert level == "green"
    assert text.startswith("♨ 55:")
