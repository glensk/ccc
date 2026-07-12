#!/usr/bin/env python3
# pylint: disable=invalid-name  # filename intentionally hyphenated (PATH-compat shim)
"""PATH-compat shim: delegate to ``command_center.codex_in_claude:main``.

The real implementation now lives inside the ``command_center`` package (shipped by
the wheel and exposed as the ``codex-in-claude`` console entry point). This thin
repo-root script keeps the historical ``./codex-in-claude.py`` / ``codex-in-claude.py``
invocation working from a source checkout, where the package may not be installed —
it puts this directory (the repo root) on ``sys.path`` before importing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from command_center.codex_in_claude import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
