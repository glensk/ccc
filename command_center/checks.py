"""Run a user-configured shell predicate; exit 0 means the check passed.

Shared by the session-level done-check (`done_check_cmd`) and the per-sub-goal
machine-check predicates. The command is user-authored (same trust model as
`done_check_cmd`) — auto-derived sub-goals never get one. Never raises.
"""

from __future__ import annotations

import subprocess


def run_exit0(command: str, cwd: str | None = None, timeout: int = 30) -> bool:
    """Return True iff *command* (run via the shell in *cwd*) exits 0.

    Output is captured/discarded; a timeout, non-zero exit, or spawn error all
    read as "not satisfied" (False) so callers degrade gracefully.
    """
    try:
        result = subprocess.run(  # noqa: S602  # user-configured command, intentional
            command,
            shell=True,
            cwd=cwd or None,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0
