"""CI wrapper for the pre-publish acceptance smoke matrix.

Invokes ``tools/smoke_matrix.py`` as a subprocess (it builds a wheel, spins a scratch
sandbox with a temp ``HOME``/``CLAUDE_HOME``, runs the acceptance commands, and proves the
real ``~/.claude`` state was untouched). Marked ``slow`` — it is exercised by a full
``pytest`` run but can be deselected with ``-m 'not slow'`` — and skipped when ``uv`` is
unavailable to build the wheel.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "tools" / "smoke_matrix.py"
_UV = shutil.which("uv")

pytestmark = pytest.mark.skipif(_UV is None, reason="uv not available to build the wheel")


@pytest.mark.slow
def test_smoke_matrix_passes() -> None:
    """The end-to-end acceptance battery runs green and proves real state is untouched."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    assert result.returncode == 0, f"smoke matrix failed:\n{result.stdout}\n{result.stderr}"
    assert "RESULT: PASS" in result.stdout
