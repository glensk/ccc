"""Claude Command Center — overview & lifecycle for parked Claude Code sessions.

Per session it tracks an AIM ("this session is done when: ..."), a sub-goal
checklist (progress bar), a human-overridable next step, a deadline, a
"blocked-on" tag, and an auto-summary. Idle sessions are auto-closed to free
memory (resumable by id). Surfaces: ``ccc ls`` (flat terminal list), ``ccc``
(Textual TUI), ``ccc serve`` (same TUI in the browser).
"""

__version__ = "0.1.0"
