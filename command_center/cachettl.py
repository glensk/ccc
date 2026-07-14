"""Prompt-cache TTL countdown for the ``model`` column (TUI + ``ccc ls``).

Anthropic's prompt cache is a *prefix* cache that expires ``TTL`` seconds after the
**last** request, not after the session started: every request re-reads the prefix and
refreshes the TTL, so an actively-used session never goes cold. Once it *does* expire the
next message pays a full cache re-write — on ANY account (the cache is per-account, so
switching cpriv↔cwork then costs the same as continuing). Surfacing the remaining warm
time lets you decide whether a parked session is still cheap to resume.

"Last request" has no first-class signal, so we approximate it by the **mtime of the
session's transcript file** — every API turn appends to it. That is a single ``os.stat``
per row and we never read the transcript (this runs per row per refresh).

This mirrors the user's statusline (``statusline-command.sh`` "Prompt-cache TTL
countdown" block); the thresholds/colours here are authoritative:

* remaining ≥ 1200 s → ``♨ M:SS`` **green**
* 600 ≤ remaining < 1200 s → ``♨ M:SS`` **orange**
* 0 < remaining < 600 s → ``♨ M:SS`` **red**
* remaining ≤ 0 → ``❄ cold`` **red**
* no transcript (missing / draft / done row) → empty cell

:func:`countdown` is a pure function of ``(mtime, now)`` — it returns a ``(text, level)``
pair where ``level`` is a colour-agnostic name (``"green"``/``"orange"``/``"red"``/``""``).
Each view maps that level to its own palette (``ccc ls`` uses xterm-256 codes, the TUI uses
Rich style strings) so the two stay pixel-identical without this module knowing either
colour system.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Session

# TTL default and its env override. The cache is nominally 1 h (drops to 5 m under a usage
# overage); ``CC_CACHE_TTL_S`` overrides it. Junk (non-integer, zero, negative) → the
# default, so a stray shell value never breaks the column.
_ENV_TTL = "CC_CACHE_TTL_S"
_DEFAULT_TTL_S = 3600

# Colour boundaries (seconds remaining). ``>=`` semantics: 1200 is green, 600 is orange.
_GREEN_MIN_S = 1200
_ORANGE_MIN_S = 600

# Warm/cold glyphs (kept identical to the statusline's ♨ / ❄).
_WARM_GLYPH = "♨"
_COLD_TEXT = "❄ cold"


def cache_ttl_seconds() -> int:
    """The prompt-cache TTL in seconds — ``CC_CACHE_TTL_S`` when a sane positive int, else 3600.

    Junk values (non-numeric, empty, zero, negative) fall back to :data:`_DEFAULT_TTL_S`
    rather than raising, so a misconfigured env var degrades quietly.
    """
    raw = os.environ.get(_ENV_TTL)
    if raw is None:
        return _DEFAULT_TTL_S
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_TTL_S
    return value if value > 0 else _DEFAULT_TTL_S


def countdown(mtime: float | None, now: float, *, ttl: int | None = None) -> tuple[str, str]:
    """The ``(text, level)`` cache-TTL cell for a transcript last touched at *mtime*.

    *mtime* is the transcript's modification time (``st_mtime``), *now* the current epoch
    seconds; *ttl* overrides :func:`cache_ttl_seconds` (tests pass it explicitly). ``level``
    is a colour name — ``"green"`` / ``"orange"`` / ``"red"`` — that the caller maps to its
    own palette. A ``None`` *mtime* (no transcript) yields ``("", "")`` — an empty cell.
    Warm rows read ``♨ M:SS`` (whole minutes, zero-padded seconds); an expired cache reads
    ``❄ cold``.
    """
    if mtime is None:
        return ("", "")
    ttl_s = cache_ttl_seconds() if ttl is None else ttl
    remaining = int(ttl_s - (now - mtime))
    if remaining <= 0:
        return (_COLD_TEXT, "red")
    minutes, seconds = divmod(remaining, 60)
    text = f"{_WARM_GLYPH} {minutes}:{seconds:02d}"
    if remaining >= _GREEN_MIN_S:
        return (text, "green")
    if remaining >= _ORANGE_MIN_S:
        return (text, "orange")
    return (text, "red")


def transcript_mtime(adapter: object, session: Session) -> float | None:
    """The mtime of *session*'s transcript, or ``None`` when it can't be resolved/stat'd.

    ``transcript_path`` is a concrete-adapter capability (not part of the ``Adapter``
    protocol), so it is probed defensively via ``getattr`` — a stub adapter without it
    degrades to ``None`` (an empty cell). Exactly one ``os.stat`` and never a transcript
    read, since this runs per row per refresh.
    """
    getter = getattr(adapter, "transcript_path", None)
    if getter is None:
        return None
    try:
        path = getter(session.cwd, session.session_id)
    except OSError:
        return None
    if not isinstance(path, Path):
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def countdown_for(adapter: object, session: Session, now: float | None = None) -> tuple[str, str]:
    """:func:`countdown` for *session* — resolve its transcript mtime, then format.

    A convenience the views call once per (non-draft, non-done) row; *now* defaults to the
    wall clock and is injectable for tests. The draft/done gating stays in the views (they
    already branch on it) — this only turns a live/parked/waiting row into a cell.
    """
    return countdown(transcript_mtime(adapter, session), time.time() if now is None else now)
