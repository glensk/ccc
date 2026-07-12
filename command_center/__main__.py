"""Enable ``python -m command_center`` as an alias for the ``ccc`` CLI."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
