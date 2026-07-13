# claude-command-center

A terminal command center that organizes and tracks Claude Code sessions by user-defined categories, giving each session a concrete AIM (done-condition), live progress bar, and status tracking. Solves the problem of losing track of half-finished sessions scattered across projects by centralizing them in one visual interface and enabling "future jobs" — ideas parked for later execution without context-switching.

Key tools: `ccc` (interactive TUI for session management), `c` (shell wrapper to seed sessions with AIMs), `ccc ls` (scripting-friendly session listing)

Stack: Python 3.11+ with Textual for TUI | Deps: textual, textual-serve, pyyaml, pyobjc-framework-Cocoa (macOS only)
