#!/usr/bin/env python3
"""Regenerate the README/docs screenshots from the deterministic ``ccc demo`` data.

Seeds a throwaway demo home (fake sessions, never the real ``CLAUDE_HOME``), drives the
Textual TUI **headlessly** with Textual's ``run_test`` pilot, and writes SVG screenshots
under ``docs/img/``. Because the demo data is deterministic, the screenshots never go stale
for a reason other than a real UI change — rerun this after touching the TUI.

Usage:
  tools/gen_screenshots.py [-o OUTDIR] [-d DEMO_HOME] [-s WxH] [-k]

Options:
  -o, --outdir  Directory for the SVGs (default: ``docs/img`` next to the repo root).
  -d, --dir     Demo home to seed (default: a temp dir, removed afterwards).
  -s, --size    Terminal size ``WxH`` the TUI renders at (default: ``150x46``).
  -k, --keep    Keep the seeded demo home instead of deleting it.
  -h, --help    Show this help and exit.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


def _parse_size(text: str) -> tuple[int, int]:
    try:
        width, height = text.lower().split("x", 1)
        return (int(width), int(height))
    except ValueError as exc:  # pragma: no cover - arg validation
        raise argparse.ArgumentTypeError(f"size must be WxH, e.g. 150x46 (got {text!r})") from exc


async def _shoot(outdir: Path, size: tuple[int, int]) -> list[Path]:
    """Mount the TUI against the seeded demo store and export SVG screenshots."""
    from command_center import demo  # noqa: E402  (after sys.path insert)
    from command_center.views.tui import CommandCenterApp, SessionTable

    written: list[Path] = []
    app = CommandCenterApp()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        # Main view: the full session table + the detail pane for the top row.
        target = outdir / "tui-main.svg"
        target.write_text(app.export_screenshot(title="ccc"), encoding="utf-8")
        written.append(target)

        # Detail view: move the cursor onto a working session so the detail pane shows a
        # populated AIM + sub-goal checklist, then export a second shot.
        table = app.query_one("#sessions", SessionTable)
        if table.row_count:
            app._current = demo.sid_for("api-gateway")  # pylint: disable=protected-access
            app.update_detail()
            await pilot.pause()
            detail = outdir / "tui-detail.svg"
            detail.write_text(app.export_screenshot(title="ccc — detail"), encoding="utf-8")
            written.append(detail)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate docs screenshots from the ccc demo data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-o",
        "--outdir",
        type=Path,
        default=_REPO_ROOT / "docs" / "img",
        help="output directory for the SVGs (default: docs/img)",
    )
    parser.add_argument("-d", "--dir", default=None, help="demo home to seed (default: a temp dir)")
    parser.add_argument(
        "-s",
        "--size",
        type=_parse_size,
        default=(150, 46),
        help="terminal size WxH (default: 150x46)",
    )
    parser.add_argument(
        "-k", "--keep", action="store_true", help="keep the seeded demo home afterwards"
    )
    args = parser.parse_args(argv)

    from command_center import demo

    outdir: Path = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    home = Path(args.dir) if args.dir else Path(tempfile.mkdtemp(prefix="ccc-demo-shots-"))

    os.environ["CLAUDE_HOME"] = str(home)
    os.environ["CODEX_HOME"] = str(home / "codex")  # seeded Codex card, not real usage
    os.environ.setdefault("CCC_NO_CODEX", "1")
    os.environ.pop("NO_COLOR", None)  # keep colour in the SVG
    demo.seed(home)
    try:
        written = asyncio.run(_shoot(outdir, args.size))
    finally:
        if not args.keep and not args.dir:
            shutil.rmtree(home, ignore_errors=True)

    for path in written:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
