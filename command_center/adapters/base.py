"""The adapter contract every agent backend must satisfy."""

from __future__ import annotations

from typing import Protocol

from ..models import LiveSession


class Adapter(Protocol):
    """Read-only window onto one agent's live sessions and their activity."""

    name: str

    def discover(self) -> list[LiveSession]:
        """Return every session the agent currently knows about."""

    def last_activity_ms(self, live: LiveSession) -> int:
        """Epoch-ms of the session's most recent activity (for idle detection)."""

    def is_oneshot_headless(self, cwd: str, session_id: str) -> bool:
        """True if the session's transcript is a headless one-shot (``claude -p``)."""

    def claude_version(self, cwd: str, session_id: str) -> str | None:
        """The agent version that last wrote to the session's transcript (e.g. ``2.1.193``).

        ``None`` when the transcript is missing or carries no version field.
        """

    def is_halted(self, cwd: str, session_id: str) -> bool:
        """True if the session's last turn ended in a Claude rate-limit halt.

        i.e. the transcript's final conversation turn is an API-error assistant
        message reading "You've hit your … limit ·" (5-hour or weekly window).
        """

    def probe(self) -> bool:
        """True if this backend's on-disk layout looks as expected on this machine."""
