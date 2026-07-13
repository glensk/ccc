"""Multi-account Claude Code launch environment (the ONE billing pin).

Claude Code hashes ``CLAUDE_CONFIG_DIR`` into its Keychain service name whenever
the var is *SET* (verified against the 2.1.205 binary): setting it to the default
``~/.claude`` therefore does **not** authenticate, because the service name
``Claude Code-credentials-ebcf0c99`` does not exist. So the account pin is:

* the DEFAULT account (``claude_home()``, active when the var is unset) → **UNSET**
  ``CLAUDE_CONFIG_DIR``;
* any OTHER account → **SET** it to that account's absolute config dir.

``CLAUDE_SECURESTORAGE_CONFIG_DIR`` is always stripped: it takes PRECEDENCE in the
Keychain-service hash and would otherwise defeat the pin.

Two renderings of the same rule:

* :func:`launch_env` — a child-process env ``dict`` for ``Popen(env=)`` / ``execvpe``.
* :func:`launch_env_prefix` — a POSIX-shell snippet for the command *strings* the
  iTerm / tmux launchers build (there is no ``env=`` to pass there).
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from . import config

if TYPE_CHECKING:
    from .adapters.base import Adapter

# CLAUDE_SECURESTORAGE_CONFIG_DIR takes precedence over CLAUDE_CONFIG_DIR in the
# Keychain-service hash, so an ambient value would silently defeat the pin below.
_SECURE_VAR = "CLAUDE_SECURESTORAGE_CONFIG_DIR"
_CONFIG_VAR = "CLAUDE_CONFIG_DIR"

# One-shot dedupe so a same-id-in-two-registries conflict warns once, not every
# 5 s TUI refresh / daemon pass.
_WARNED_CONFLICTS: set[str] = set()


def _resolve(path: str | Path) -> Path:
    """``expanduser`` + ``resolve`` (non-strict), tolerating a missing path."""
    try:
        return Path(path).expanduser().resolve()
    except OSError:
        return Path(path).expanduser()


def default_config_dir() -> Path:
    """The DEFAULT account's config dir (``claude_home()``, the unset-var account)."""
    return _resolve(config.claude_home())


def is_default_config_dir(config_dir: str) -> bool:
    """True when *config_dir* is (or is empty ⇒) the default account.

    An empty string is treated as the default here so :func:`launch_env` UNSETS the
    var for an unstamped row; the multi-account "unknown ⇒ refuse" guard is enforced
    separately at the launch call sites (they check ``config_dir == "" and
    is_multi_account()`` BEFORE calling this).
    """
    if not config_dir:
        return True
    return _resolve(config_dir) == default_config_dir()


def is_multi_account() -> bool:
    """True when more than one Claude account is configured."""
    return len(config.claude_config_dirs()) > 1


def same_config_dir(a: str, b: str) -> bool:
    """True if *a* and *b* name the same account dir (empty ⇒ the default account)."""
    return (_resolve(a) if a else default_config_dir()) == (
        _resolve(b) if b else default_config_dir()
    )


def account_label(config_dir: str) -> str:
    """The configured label for *config_dir* (falls back to its basename / path)."""
    if not config_dir:
        return next(iter(config.claude_config_dirs()), "private")
    target = _resolve(config_dir)
    for label, path in config.claude_config_dirs().items():
        if _resolve(path) == target:
            return label
    return Path(config_dir).name or config_dir


def account_config_dir(label: str) -> str:
    """The absolute config dir for account *label* ("" when the label is unknown)."""
    path = config.claude_config_dirs().get(label)
    return str(path) if path is not None else ""


# The ccc ``model`` column marks each row with a per-account glyph: 🏠 for the ``private``
# (cpriv) account, 💼 for the ``work`` (cwork) account. Each is a width-2 colored emoji + a
# trailing space (3 terminal cells, matching the width-2 badge emoji convention in
# ``tabsymbol``); ``_NO_HOME`` is the same width in blanks so the model text stays
# column-aligned on rows for any OTHER (unknown / third) account. In single-account mode the
# marker is empty (every row would carry it, so it would mean nothing). The statusline
# (``dotfiles/claude/.claude/statusline-command.sh``) shows the SAME 🏠/💼 after the model —
# keep the two in sync.
_HOME_GLYPH = "🏠 "
_WORK_GLYPH = "💼 "
_NO_HOME = "   "


def _bills_account(config_dir: str, label: str, dirs: dict[str, Path]) -> bool:
    """True when *config_dir* resolves to the account *label*'s dir (empty ⇒ never)."""
    if not config_dir:
        return False
    target = dirs.get(label)
    return target is not None and _resolve(config_dir) == _resolve(target)


def is_private_account(config_dir: str, dirs: dict[str, Path] | None = None) -> bool:
    """True when *config_dir* bills to the account labelled ``private`` (cpriv).

    An empty *config_dir* is the multi-account UNKNOWN sentinel (id live under two
    accounts, or never observed) — never private. Compares RESOLVED paths so a symlinked
    or differently-spelled dir still matches. *dirs* is the already-parsed account map
    (pass it to avoid a config-file read per row); ``None`` reads the config.
    """
    return _bills_account(
        config_dir, "private", config.claude_config_dirs() if dirs is None else dirs
    )


def is_work_account(config_dir: str, dirs: dict[str, Path] | None = None) -> bool:
    """True when *config_dir* bills to the account labelled ``work`` (cwork). See
    :func:`is_private_account` for the empty-``config_dir`` / resolution semantics."""
    return _bills_account(config_dir, "work", config.claude_config_dirs() if dirs is None else dirs)


def home_marker(config_dir: str, dirs: dict[str, Path] | None = None) -> str:
    """A fixed-width per-account glyph for the ccc ``model`` column (TUI + ``ccc ls``).

    Returns ``"🏠 "`` for the ``private`` (cpriv) account, ``"💼 "`` for the ``work``
    (cwork) account, an equal-width blank for any OTHER account (so the model text stays
    aligned), and ``""`` in single-account mode (the mark would be on every row and so
    carries no signal). *dirs* is the already-parsed account map — pass it to avoid a
    config read per row.
    """
    dirs = config.claude_config_dirs() if dirs is None else dirs
    if len(dirs) <= 1:  # single account → the mark would be on every row → drop it
        return ""
    if is_private_account(config_dir, dirs):
        return _HOME_GLYPH
    if is_work_account(config_dir, dirs):
        return _WORK_GLYPH
    return _NO_HOME


def _export_value(config_dir: str) -> str:
    """The exact string to export as ``CLAUDE_CONFIG_DIR`` for *config_dir*.

    Claude hashes the LITERAL value into its Keychain service name, so what we export
    must match what the user's own account shell aliases export byte-for-byte.
    Comparisons in this module resolve symlinks, and :func:`env_config_dir` stamps a
    RESOLVED path into the store — so map back to the CONFIGURED spelling here. Without
    this, a symlinked account dir would export a different string than the alias, hash to
    a different service name, and read as "not authenticated" with no visible cause.
    An unconfigured dir is exported as given (expanded).
    """
    target = _resolve(config_dir)
    for path in config.claude_config_dirs().values():
        if _resolve(path) == target:
            return str(path)
    return str(Path(config_dir).expanduser())


def launch_env(config_dir: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """Child env that launches/resumes a session under *config_dir*'s account.

    Starts from a COPY of *base* (``os.environ`` by default), always strips
    ``CLAUDE_SECURESTORAGE_CONFIG_DIR``, then either UNSETS ``CLAUDE_CONFIG_DIR``
    (default account) or SETS it to the account's dir — so an ambient work
    ``CLAUDE_CONFIG_DIR`` never leaks into a private launch (and vice-versa).
    """
    env = dict(os.environ if base is None else base)
    env.pop(_SECURE_VAR, None)
    if is_default_config_dir(config_dir):
        env.pop(_CONFIG_VAR, None)
    else:
        env[_CONFIG_VAR] = _export_value(config_dir)
    return env


def apply_to_environ(config_dir: str) -> None:
    """Pin *config_dir*'s account into ``os.environ`` IN PLACE (for an ``os.execvp``).

    ``os.execvp`` inherits the current ``os.environ``, so mutating it here — strip
    ``CLAUDE_SECURESTORAGE_CONFIG_DIR``, then unset (default account) or set
    ``CLAUDE_CONFIG_DIR`` — pins the account without needing ``execvpe`` (which would
    escape tests that monkeypatch the 2-arg ``os.execvp``).
    """
    os.environ.pop(_SECURE_VAR, None)
    if is_default_config_dir(config_dir):
        os.environ.pop(_CONFIG_VAR, None)
    else:
        os.environ[_CONFIG_VAR] = _export_value(config_dir)


def launch_env_prefix(config_dir: str) -> str:
    """A POSIX-shell prefix pinning the account for a launcher command *string*.

    The iTerm / tmux launchers hand a shell a command string (no ``env=`` to pass),
    so the pin must survive INTO the string. Returns a trailing-space snippet to
    prepend verbatim, e.g. ``"unset CLAUDE_SECURESTORAGE_CONFIG_DIR; export
    CLAUDE_CONFIG_DIR=/home/user/.claude-work; "``. The default account unsets
    both vars.
    """
    if is_default_config_dir(config_dir):
        return f"unset {_SECURE_VAR} {_CONFIG_VAR}; "
    quoted = shlex.quote(_export_value(config_dir))
    return f"unset {_SECURE_VAR}; export {_CONFIG_VAR}={quoted}; "


def env_config_dir() -> str:
    """The account this shell is billing to, from the in-session env.

    Hooks run INSIDE a Claude session, so ``CLAUDE_CONFIG_DIR`` is authoritative:
    when set, that account's resolved dir; when unset, the default account
    (``claude_home()``). Always a concrete absolute path (never "").
    """
    env = os.environ.get(_CONFIG_VAR)
    return str(_resolve(env) if env else default_config_dir())


def live_conflict(session_id: str, adapter: Adapter | None = None) -> bool:
    """True if *session_id* is live under two account registries (a D9 conflict).

    Best-effort (a discover error → no conflict): a conflicting id must never be
    resumed/focused until one side exits, or ccc could silently bill the wrong
    account. Shared by every launch-shaped surface (``ccc resume``, ``ccc jump``);
    *adapter* is injectable for tests, defaulting to a fresh :class:`ClaudeAdapter`.
    """
    try:
        if adapter is None:
            # Lazy: keep this module import-light (hooks' hot path) and free of a
            # module-level accounts ⇄ adapters edge (adapters.claude imports us).
            from .adapters import ClaudeAdapter  # pylint: disable=import-outside-toplevel

            adapter = ClaudeAdapter()
        for live in adapter.discover():
            if live.session_id == session_id and live.conflict:
                return True
    except OSError:
        return False
    return False


def warn_conflict(session_id: str, first: str, second: str) -> None:
    """Warn ONCE that *session_id* is live in two account registries (D9).

    A same-id collision is a conflict, not a race to win — the caller leaves
    ``config_dir`` unstamped and refuses resume/focus until one side exits.
    """
    if session_id in _WARNED_CONFLICTS:
        return
    _WARNED_CONFLICTS.add(session_id)
    print(
        f"ccc: warning: session {session_id[:8]} is live under two Claude accounts "
        f"({account_label(first)} and {account_label(second)}); refusing to attribute "
        "an account until one exits.",
        file=sys.stderr,
    )
