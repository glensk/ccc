"""Cross-process UI state for the ``f+j`` toggle between the ccc TUI and session tabs.

Two one-way signals coordinate the live TUI with out-of-process ``ccc jump`` runs
(fired by the global Karabiner chord). Each is a single tiny file under
:func:`config.app_home` — separate files, so the TUI's writes and ``ccc jump``'s
writes never race on a shared blob:

- **selected** (``jump_selected``) — the session id under the TUI cursor. The TUI
  writes it on every row highlight; ``ccc jump``, when run *from* the ccc tab, reads
  it to know which session to jump to (``f+j`` in ccc acts like ``r``).
- **request** (``jump_request``) — a session id ``ccc jump`` asks the TUI to move its
  cursor to, when run from a *session* tab (focus ccc + select that session's row).
  The TUI consumes (clears) it once it has moved the cursor there.
"""

from __future__ import annotations

from . import config

_SELECTED = "jump_selected"
_REQUEST = "jump_request"


def _read(name: str) -> str | None:
    try:
        value = (config.app_home() / name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _write(name: str, value: str | None) -> None:
    path = config.app_home() / name
    try:
        if value is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    except OSError:
        pass


def set_selected(session_id: str | None) -> None:
    """Record the session id currently under the TUI cursor (TUI → ``ccc jump``)."""
    _write(_SELECTED, session_id)


def get_selected() -> str | None:
    """The session id last under the TUI cursor, or None."""
    return _read(_SELECTED)


def request_select(session_id: str) -> None:
    """Ask the live TUI to move its cursor to *session_id* (``ccc jump`` → TUI)."""
    _write(_REQUEST, session_id)


def peek_request() -> str | None:
    """The pending cursor-move request, or None (does not clear it)."""
    return _read(_REQUEST)


def clear_request() -> None:
    """Drop the pending cursor-move request (the TUI calls this once it has acted)."""
    _write(_REQUEST, None)
