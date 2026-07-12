"""Agent adapters. ``claude`` is implemented; a ``codex`` adapter can drop in later."""

from __future__ import annotations

from .claude import ClaudeAdapter

__all__ = ["ClaudeAdapter"]
