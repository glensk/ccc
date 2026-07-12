#!/usr/bin/env python3
# pylint: disable=invalid-name  # filename intentionally hyphenated (matches codex-review.py)
"""codex-in-claude.py — pick the OpenAI Codex model for Claude Code's codex commands,
and run one delegated task round (engine behind /codex-implement-task-and-claude-review).

This single script is the shared control point for every Codex-related Claude Code
command (``/codex-implement-task-and-claude-review`` and ``/codex-debate``):

* ``models``      — list the Codex models available on this login.
* ``get-model``   — print the model resolved for a given command.
* ``set-model``   — set the model for a command (or the global default).
* ``delegate``    — run ONE Codex round, **printing the model as the first stdout line**,
                    then Codex's reply. Used by the codex-implement-task-and-claude-review skill.
* ``usage``       — print the current Codex rate-limit usage (5h + weekly windows).

Model source: ``codex debug models`` (``--refresh``), else the offline cache
``~/.codex/models_cache.json`` (fast / wifi-friendly default). Only models with
``visibility == "list"`` are shown unless ``--include-hidden``.

Config (JSON, atomic writes)::

    ~/.config/codex-in-claude/config.json        # override via $CODEX_IN_CLAUDE_CONFIG
    {"default": "gpt-5.5", "delegate-review": null, "debate": null}

Resolution order for a command: per-command value -> ``default`` -> ``gpt-5.5``.

``delegate`` exit codes (the skill branches on these):
  0 ok | 2 usage | 3 invalid-model | 4 codex-missing-or-auth | 5 timeout |
  6 codex-nonzero | 7 bad-patch (reserved) | 8 quota-exhausted (skipped, see below).

Concurrency + quota awareness: each ``delegate`` first runs a **quota preflight** — it reads
the Codex ``rate_limits`` (5h + weekly) from ``$CODEX_HOME/sessions/**/rollout-*.jsonl`` and,
if a live window is ``>=100%`` used, prints the reset time and exits ``EX_QUOTA`` (8) WITHOUT
launching codex (bypass: ``-Q`` / ``$CODEX_IN_CLAUDE_IGNORE_QUOTA``). It then takes one of N
cross-process flock slots, where the effective cap is **usage-tapered**: <50% used -> 3,
50-75% -> 2, >75% -> 1 (N ceiling = ``-j/--max-concurrent`` -> ``$CODEX_IN_CLAUDE_MAX_CONCURRENT``
-> 3; ``0`` = unlimited). Stale windows (reset already passed) are ignored; unknown usage fails
open. Keeps a fan-out from thrashing CPU/API and from slamming the wall with many in-flight runs.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO

DEFAULT_MODEL = "gpt-5.5"  # newest/best per the Codex catalog
COMMANDS = ("delegate-review", "debate")  # codex-related commands this manager governs
EFFORTS = ("low", "medium", "high", "xhigh")  # codex reasoning levels (API-validated)
CODEX_CACHE = Path.home() / ".codex" / "models_cache.json"

# Concurrency cap: at most this many `delegate` processes run `codex exec` at once.
DEFAULT_MAX_CONCURRENT = 3
SLOT_DIR = Path(
    os.environ.get(
        "CODEX_IN_CLAUDE_SLOT_DIR",
        str(Path.home() / ".config" / "codex-in-claude" / "slots"),
    )
)

# delegate exit codes
EX_OK, EX_USAGE = 0, 2
EX_INVALID_MODEL, EX_NO_CODEX, EX_TIMEOUT, EX_CODEX_FAIL, EX_BAD_PATCH = 3, 4, 5, 6, 7
EX_QUOTA = 8  # skipped: Codex quota exhausted (>=100% used on a live window)

# Codex usage (rate-limit) reading — Codex has no usage API; it writes a rate_limits
# block onto token_count events in $CODEX_HOME/sessions/**/rollout-*.jsonl.
_CODEX_SCAN_LIMIT = (
    200  # newest rollout files to scan for a usable window (short runs log windowless)
)


# --------------------------------------------------------------------------- #
# Clickable-terminal helpers (OSC 8) — per repo convention.
# --------------------------------------------------------------------------- #
def osc8_link(target: str, label: str | None = None) -> str:
    """Wrap *target* (URL) as an OSC 8 hyperlink; degrades to plain text."""
    label = label or target
    return f"\x1b]8;;{target}\x1b\\{label}\x1b]8;;\x1b\\"


def local_link(path: Path) -> str:
    """Clickable link to a local *path* (openterm:// for iTerm2/WezTerm, file:// fallback)."""
    abspath = str(path.resolve())
    return osc8_link("openterm://" + urllib.parse.quote(abspath), abspath)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def config_path() -> Path:
    """Shared config file path ($CODEX_IN_CLAUDE_CONFIG override)."""
    env = os.environ.get("CODEX_IN_CLAUDE_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "codex-in-claude" / "config.json"


def load_config() -> dict:
    """Load the config, tolerating a missing/corrupt file (returns defaults)."""
    path = config_path()
    base = {"default": DEFAULT_MODEL, "delegate-review": None, "debate": None, "effort": "xhigh"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            base.update(data)
    except (OSError, ValueError):
        pass
    return base


def save_config(cfg: dict) -> Path:
    """Atomically persist *cfg*; returns the path written."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=str(path.parent), prefix=".cfg.", suffix=".tmp", delete=False
    ) as handle:
        json.dump(cfg, handle, indent=2, sort_keys=True)
        handle.write("\n")
        tmp = handle.name
    os.replace(tmp, path)
    return path


def resolve_model(for_command: str | None) -> str:
    """Resolve the model for *for_command*: per-command -> default -> DEFAULT_MODEL."""
    cfg = load_config()
    if for_command and cfg.get(for_command):
        return str(cfg[for_command])
    return str(cfg.get("default") or DEFAULT_MODEL)


def resolve_effort() -> str | None:
    """Configured reasoning effort, or None to use the model's catalog default."""
    val = load_config().get("effort")
    return str(val) if val in EFFORTS else None


# --------------------------------------------------------------------------- #
# Model catalog
# --------------------------------------------------------------------------- #
def _parse_models(blob: str) -> list[dict]:
    """Extract the model list from a `codex debug models` / cache JSON blob."""
    data = json.loads(blob)
    models = data if isinstance(data, list) else data.get("models", [])
    return [m for m in models if isinstance(m, dict)]


def list_models(*, refresh: bool, include_hidden: bool, timeout: int = 30) -> list[dict]:
    """Return the available Codex models.

    Default reads the offline cache (fast, works on a bad connection); ``--refresh``
    calls ``codex debug models`` (network) and falls back to the cache on failure.
    """
    models: list[dict] = []
    if refresh:
        try:
            proc = subprocess.run(
                ["codex", "debug", "models"],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                models = _parse_models(proc.stdout)
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            models = []
    if not models:
        try:
            models = _parse_models(CODEX_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            models = []
    if not include_hidden:
        models = [m for m in models if m.get("visibility", "list") == "list"]
    return models


def effort_of(slug: str) -> str:
    """Catalog default reasoning effort for *slug* (informational), or '?'."""
    for m in list_models(refresh=False, include_hidden=True):
        if m.get("slug") == slug:
            return str(m.get("default_reasoning_level") or "?")
    return "?"


def valid_slug(slug: str) -> bool:
    """True if *slug* is a known model (visible or hidden)."""
    return any(m.get("slug") == slug for m in list_models(refresh=False, include_hidden=True))


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #
def cmd_models(args: argparse.Namespace) -> int:
    """List models, starring the ones currently configured."""
    models = list_models(refresh=args.refresh, include_hidden=args.include_hidden)
    if not models:
        print(
            "No models found (cache empty and/or `codex debug models` unavailable).\n"
            "Try: codex-in-claude.py models --refresh",
            file=sys.stderr,
        )
        return EX_NO_CODEX
    cfg = load_config()
    configured = {cfg.get("default"), cfg.get("delegate-review"), cfg.get("debate")}
    width = max(len(str(m.get("slug", ""))) for m in models)
    print(f"Available Codex models  (default: {cfg.get('default') or DEFAULT_MODEL})\n")
    for m in models:
        slug = str(m.get("slug", ""))
        star = "*" if slug in configured else " "
        eff = str(m.get("default_reasoning_level") or "?")
        vis = str(m.get("visibility", "list"))
        hide = "  [hidden]" if vis != "list" else ""
        desc = str(m.get("description") or "")
        print(f" {star} {slug:<{width}}  effort={eff:<7}{hide}  {desc}")
    print()
    print("per-command:")
    for cmd in COMMANDS:
        print(f"    {cmd:<16} -> {resolve_model(cmd)}")
    print(f"    {'effort':<16} -> {resolve_effort() or 'default (each model own)'}")
    print(f"\nconfig: {local_link(config_path())}")
    return EX_OK


def cmd_get_model(args: argparse.Namespace) -> int:
    """Print the resolved model for a command (or the global default)."""
    print(resolve_model(args.for_command))
    return EX_OK


def cmd_set_model(args: argparse.Namespace) -> int:
    """Set the model for a command (or the global default with --for all/omitted)."""
    slug = args.slug
    if not valid_slug(slug):
        known = ", ".join(
            str(m.get("slug")) for m in list_models(refresh=False, include_hidden=False)
        )
        print(
            f"Unknown model '{slug}'. Known (visible): {known or '(none)'}\n"
            "Use --include-hidden via `models -H` to see hidden ones.",
            file=sys.stderr,
        )
        return EX_INVALID_MODEL
    cfg = load_config()
    target = args.for_command
    if target in (None, "all"):
        cfg["default"] = slug
        where = "default (all commands)"
    else:
        cfg[target] = slug
        where = target
    path = save_config(cfg)
    print(f"set {where} model -> {slug}\nconfig: {path}")
    return EX_OK


def cmd_get_effort(_args: argparse.Namespace) -> int:
    """Print the configured reasoning effort (or 'default' = the model's own default)."""
    print(resolve_effort() or "default")
    return EX_OK


def cmd_set_effort(args: argparse.Namespace) -> int:
    """Set the global reasoning effort; 'default' clears it (each model uses its own)."""
    level = args.level
    cfg = load_config()
    if level == "default":
        cfg["effort"] = None
        msg = "effort -> default (each model's own)"
    elif level in EFFORTS:
        cfg["effort"] = level
        msg = f"effort -> {level}"
    else:
        print(f"Unknown effort '{level}'. Choose: {', '.join(EFFORTS)}, default.", file=sys.stderr)
        return EX_USAGE
    path = save_config(cfg)
    print(f"{msg}\nconfig: {path}")
    return EX_OK


_PATCH_CONTRACT = (
    "You are Codex, implementing a task delegated by Claude Code. You are READ-ONLY: "
    "you CANNOT edit files. Inspect the repo as needed, then produce a COMPLETE solution "
    "as a single git-apply-able unified diff. Output EXACTLY these two sections and nothing "
    "after the diff:\n"
    "### SELF-CHECK\n"
    "<bullets: what you changed and why it is correct, edge cases considered, and the exact "
    "test/lint/build commands that SHOULD be run to verify it>\n"
    "### DIFF\n"
    "```diff\n"
    "<unified diff, paths relative to the repo root, applies cleanly with `git apply`>\n"
    "```\n"
)

_WRITE_CONTRACT = (
    "You are Codex, implementing a task delegated by Claude Code. You MAY edit files in this "
    "workspace. Implement the task fully, then RUN the project's tests/lint to verify your work "
    "and fix until they pass. Do NOT commit or push. End with a SELF-CHECK section:\n"
    "### SELF-CHECK\n"
    "<files changed; commands you ran and their pass/fail results; any caveats or risks>\n"
)

_SCOUT_CONTRACT = (
    "You are Codex, SCOUTING a task for Claude Code before implementation. You are READ-ONLY: "
    "inspect the repo (read files, run read-only commands) and return a short PLAN. Do NOT write "
    "code, edits, or a diff. Output EXACTLY this section, ~25 lines max:\n"
    "### PLAN\n"
    "<(1) the files/symbols you will change; (2) the approach as a short numbered list of steps; "
    "(3) risks, unknowns, or questions that need the caller's decision>\n"
)


def _build_delegate_prompt(
    task: str, *, write: bool, feedback: str | None, round_no: int, scout: bool = False
) -> str:
    """Compose the Codex prompt: contract header + task (+ revision feedback)."""
    header = _SCOUT_CONTRACT if scout else (_WRITE_CONTRACT if write else _PATCH_CONTRACT)
    parts = [header, "\n---\nTASK:\n", task.strip(), "\n"]
    if feedback and feedback.strip():
        parts += [
            f"\n--- REVISION (round {round_no}). Claude reviewed your previous attempt and it "
            "did NOT pass. Address every point concretely:\n",
            feedback.strip(),
            "\n",
        ]
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Concurrency cap — a cross-process flock semaphore so a wide fan-out of
# `delegate` calls runs at most N codex runs at once (uncapped concurrency
# previously thrashed CPU/API and caused Codex timeouts).
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Codex usage / rate-limit awareness (read-only; vendored, mirrors ccc usage.py)
# --------------------------------------------------------------------------- #
def _codex_home() -> Path:
    """Codex state dir (``$CODEX_HOME`` or ``~/.codex``)."""
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _codex_window(raw: object) -> tuple[float, int] | None:
    """Parse one ``rate_limits`` window -> ``(used_percent, resets_at_epoch)`` or None."""
    if not isinstance(raw, dict):
        return None
    pct, resets = raw.get("used_percent"), raw.get("resets_at")
    if pct is None or resets is None:
        return None
    try:
        return (float(pct), int(resets))
    except (TypeError, ValueError):
        return None


def _dig_rate_limits(obj: object) -> dict | None:
    """Extract the ``rate_limits`` dict from a rollout line (top-level or under payload)."""
    if not isinstance(obj, dict):
        return None
    for cand in (obj.get("rate_limits"), (obj.get("payload") or {}).get("rate_limits")):
        if isinstance(cand, dict):
            return cand
    return None


def _latest_rate_limits(path: Path) -> dict | None:
    """Newest *usable* ``rate_limits`` block in a rollout JSONL (scanning from the end).

    Skips windowless ``premium`` blocks (both windows null) that short ``codex exec``
    runs log, so the freshest block with real 5h/weekly data wins.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        rate_limits = _dig_rate_limits(obj)
        if rate_limits is not None and (
            _codex_window(rate_limits.get("primary")) is not None
            or _codex_window(rate_limits.get("secondary")) is not None
        ):
            return rate_limits
    return None


def _codex_usage_windows(now: int | None = None) -> dict[str, tuple[float, int] | None]:
    """Live 5h/weekly windows from the newest usable rollout block.

    ``{"five_hour": (pct, resets)|None, "seven_day": (pct, resets)|None}``. A window
    whose ``resets_at <= now`` is STALE (its reset already passed) and reported None —
    else the gate would never reopen after a reset. All-None => usage unknown.
    """
    now_ts = int(time.time()) if now is None else now
    sessions = _codex_home() / "sessions"
    try:
        files = sorted(
            sessions.glob("**/rollout-*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return {"five_hour": None, "seven_day": None}
    for path in files[:_CODEX_SCAN_LIMIT]:
        rate_limits = _latest_rate_limits(path)
        if rate_limits is None:
            continue

        def _live(win: tuple[float, int] | None) -> tuple[float, int] | None:
            return win if (win is not None and win[1] > now_ts) else None

        return {
            "five_hour": _live(_codex_window(rate_limits.get("primary"))),
            "seven_day": _live(_codex_window(rate_limits.get("secondary"))),
        }
    return {"five_hour": None, "seven_day": None}


def read_codex_usage(now: int | None = None) -> tuple[float | None, int | None]:
    """``(used_percent, resets_at)`` of the most-consumed live window, or ``(None, None)``."""
    windows = _codex_usage_windows(now)
    live = [w for w in (windows["five_hour"], windows["seven_day"]) if w is not None]
    if not live:
        return (None, None)
    return max(live, key=lambda w: w[0])


def _format_reset(resets_at: int, now: int | None = None) -> str:
    """Relative reset, minute precision: ``in 4h 5m`` / ``in 3d 2h 4m`` / ``now``."""
    now = int(time.time()) if now is None else now
    secs = int(resets_at) - now
    if secs <= 0:
        return "now"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    parts = [f"{days}d"] if days else []
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{mins}m")
    return "in " + " ".join(parts)


def _usage_tier_cap(ceiling: int, now: int | None = None) -> int:
    """Usage-tapered concurrency cap within ``[1, ceiling]``.

    <50% used -> 3, 50-75% -> 2, >75% -> 1 (of whichever window is most consumed);
    unknown usage -> ceiling (fail open). Never 0 — the >=100% case is the caller's
    preflight (EX_QUOTA), not the slot loop.
    """
    pct, _ = read_codex_usage(now)
    if pct is None:
        return ceiling
    if pct < 50.0:
        tier = 3
    elif pct <= 75.0:
        tier = 2
    else:
        tier = 1
    return max(1, min(tier, ceiling))


def _max_concurrent(flag_value: int | None) -> int:
    """Resolve the cap: -j flag -> $CODEX_IN_CLAUDE_MAX_CONCURRENT -> default 3.

    A value <= 0 disables gating (unlimited).
    """
    if flag_value is not None:
        return flag_value
    env = os.environ.get("CODEX_IN_CLAUDE_MAX_CONCURRENT")
    if env is not None:
        try:
            return int(env)
        except ValueError:
            print(
                f"WARN: ignoring non-integer CODEX_IN_CLAUDE_MAX_CONCURRENT={env!r}.",
                file=sys.stderr,
            )
    return DEFAULT_MAX_CONCURRENT


@contextlib.contextmanager
def _concurrency_slot(ceiling: int, poll: float = 3.0) -> Iterator[None]:
    """Hold a cross-process slot for the body, capping concurrent codex runs.

    The pool is *ceiling* flock files, but the effective cap is **usage-tapered**
    within ``[1, ceiling]`` and recomputed every poll: <50% used -> 3, 50-75% -> 2,
    >75% -> 1 (see :func:`_usage_tier_cap`); unknown usage -> ceiling. Only the first
    ``cap`` slots are ever tried, so as usage climbs fewer *new* runs are admitted
    while in-flight ones finish (fewer sessions to reload after a reset). A slot
    auto-releases if its holder dies (the OS drops the flock). ``ceiling <= 0``
    disables gating. Fail-open: any setup error runs ungated.
    """
    if ceiling <= 0:
        yield
        return
    try:
        SLOT_DIR.mkdir(parents=True, exist_ok=True)
        handles: list[TextIO] = [
            open(SLOT_DIR / f"slot{i}.lock", "w", encoding="utf-8") for i in range(ceiling)
        ]
    except OSError as exc:
        print(f"… concurrency gate disabled ({exc}); running ungated.", file=sys.stderr)
        yield
        return
    held: TextIO | None = None
    try:
        ticks = 0
        while held is None:
            cap = _usage_tier_cap(ceiling)  # usage-tapered, recomputed each poll
            for handle in handles[:cap]:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held = handle
                    break
                except OSError:
                    continue
            if held is None:
                if ticks % 10 == 0:  # heartbeat every ~30s so a waiting task looks alive
                    print(
                        f"… waiting for a Codex slot (usage-tapered cap {cap}/{ceiling})…",
                        file=sys.stderr,
                        flush=True,
                    )
                ticks += 1
                time.sleep(poll)
        yield
    finally:
        if held is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        for handle in handles:
            handle.close()


def cmd_usage(args: argparse.Namespace) -> int:
    """Print the current Codex rate-limit usage (5h + weekly windows)."""
    windows = _codex_usage_windows()
    if args.json:
        payload = {
            key: ({"used_percent": win[0], "resets_at": win[1]} if win is not None else None)
            for key, win in (
                ("five_hour", windows["five_hour"]),
                ("seven_day", windows["seven_day"]),
            )
        }
        print(json.dumps(payload))
        return EX_OK
    parts = []
    for label, key in (("5h", "five_hour"), ("weekly", "seven_day")):
        win = windows[key]
        parts.append(
            f"{label} —"
            if win is None
            else f"{label} {win[0]:.0f}% (resets {_format_reset(win[1])})"
        )
    print("codex usage: " + "  ·  ".join(parts))
    return EX_OK


def cmd_delegate(args: argparse.Namespace) -> int:
    """Run one Codex round. First stdout line = the model; then Codex's reply."""
    if not args.prompt.strip():
        print("ERROR: empty prompt.", file=sys.stderr)
        return EX_USAGE
    model = args.model or resolve_model("delegate-review")
    if args.model and not valid_slug(args.model):
        print(f"ERROR: unknown model '{args.model}'.", file=sys.stderr)
        return EX_INVALID_MODEL
    effort = args.effort or resolve_effort()  # None -> let codex use the model's default
    shown_effort = effort or effort_of(model)
    # The guaranteed first line — captured/printed by us, never Claude preamble.
    print(f"model: {model} (effort {shown_effort})", flush=True)

    # Quota preflight: skip fast (never launch codex) when a live rate-limit window is
    # exhausted. Fail-open on unknown usage; bypass with -Q / $CODEX_IN_CLAUDE_IGNORE_QUOTA.
    ignore_quota = getattr(args, "ignore_quota", False)
    if not (ignore_quota or os.environ.get("CODEX_IN_CLAUDE_IGNORE_QUOTA") == "1"):
        used_pct, resets_at = read_codex_usage()
        if used_pct is not None and used_pct >= 100.0:
            when = f" (resets {_format_reset(resets_at)})" if resets_at else ""
            print(
                f"ERROR: Codex quota exhausted — {used_pct:.0f}% used{when}. "
                "Skipping; retry after reset.",
                file=sys.stderr,
            )
            return EX_QUOTA

    write = args.write and not args.scout  # scouting is always read-only (plan, no edits)
    sandbox = "workspace-write" if write else "read-only"
    prompt = _build_delegate_prompt(
        args.prompt,
        write=write,
        feedback=args.feedback,
        round_no=args.round,
        scout=args.scout,
    )
    env = dict(os.environ)
    env["CCC_NO_CODEX"] = "1"  # never re-trigger the plan/k8s automation
    env["CCC_INTERNAL"] = "1"
    env["AI_NO_AUTOCOMMIT"] = "1"  # never let a nested run auto-commit

    with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False) as handle:
        out_path = handle.name
    cmd = ["codex", "exec", "-s", sandbox, "--skip-git-repo-check", "-o", out_path, "-m", model]
    if effort:
        cmd += ["-c", f"model_reasoning_effort={effort}"]
    if args.cwd:
        cmd += ["-C", args.cwd]
    cmd.append(prompt)

    # Take one concurrency slot before launching codex; the rest of a fan-out waits.
    with _concurrency_slot(_max_concurrent(args.max_concurrent)):
        # When Codex may write, snapshot the worktree so the caller reviews ONLY Codex's diff.
        before = _git_status(args.cwd) if write else None
        try:
            proc = subprocess.run(
                cmd, env=env, capture_output=True, text=True, timeout=args.timeout, check=False
            )
        except FileNotFoundError:
            print("ERROR: `codex` CLI not found on PATH.", file=sys.stderr)
            return EX_NO_CODEX
        except subprocess.TimeoutExpired:
            print(
                f"ERROR: Codex timed out after {args.timeout}s "
                "(slow/flaky connection? retry or raise --timeout).",
                file=sys.stderr,
            )
            return EX_TIMEOUT
    try:
        reply = Path(out_path).read_text(encoding="utf-8").strip()
    except OSError:
        reply = ""
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass

    if not reply and proc.returncode != 0:
        print(f"ERROR: Codex exited {proc.returncode}:\n{proc.stderr.strip()}", file=sys.stderr)
        return EX_CODEX_FAIL
    print(reply or proc.stdout.strip())
    if write:
        after = _git_status(args.cwd)
        changed = sorted(set(after) - set(before or []))
        if changed:
            print("\n### CODEX-WROTE (review this diff)\n" + "\n".join(changed))
    return EX_OK


def _git_status(cwd: str | None) -> list[str]:
    """`git status --porcelain` lines for *cwd* (empty on any error)."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return [ln for ln in proc.stdout.splitlines() if ln.strip()]
    except (OSError, subprocess.SubprocessError):
        return []


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser (every flag has a short form)."""
    parser = argparse.ArgumentParser(
        prog="codex-in-claude.py",
        description="Manage Codex model choice for Claude Code commands; delegate tasks to Codex.",
        epilog=(
            "Examples:\n"
            "  codex-in-claude.py models --refresh\n"
            "  codex-in-claude.py set-model gpt-5.5 --for delegate-review\n"
            "  codex-in-claude.py get-model --for debate\n"
            "  codex-in-claude.py delegate --write -C . 'add retry to fetch()'\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_models = sub.add_parser("models", help="list available Codex models")
    p_models.add_argument(
        "-r", "--refresh", action="store_true", help="refresh via `codex debug models`"
    )
    p_models.add_argument(
        "-H", "--include-hidden", action="store_true", help="include hidden models"
    )
    p_models.set_defaults(func=cmd_models)

    p_get = sub.add_parser("get-model", help="print the model resolved for a command")
    p_get.add_argument(
        "-f",
        "--for",
        dest="for_command",
        choices=COMMANDS,
        default=None,
        help="command (default: global)",
    )
    p_get.set_defaults(func=cmd_get_model)

    p_set = sub.add_parser("set-model", help="set the model for a command (or global default)")
    p_set.add_argument("slug", help="model slug, e.g. gpt-5.5")
    p_set.add_argument(
        "-f",
        "--for",
        dest="for_command",
        choices=(*COMMANDS, "all"),
        default=None,
        help="command (default/all = global)",
    )
    p_set.set_defaults(func=cmd_set_model)

    p_geteff = sub.add_parser("get-effort", help="print the configured reasoning effort")
    p_geteff.set_defaults(func=cmd_get_effort)

    p_seteff = sub.add_parser("set-effort", help="set the global reasoning effort")
    p_seteff.add_argument(
        "level", choices=(*EFFORTS, "default"), help="reasoning level (default = each model's own)"
    )
    p_seteff.set_defaults(func=cmd_set_effort)

    p_del = sub.add_parser("delegate", help="run one Codex round (prints model first)")
    p_del.add_argument("prompt", help="the task for Codex")
    p_del.add_argument(
        "-w", "--write", action="store_true", help="let Codex edit files (workspace-write)"
    )
    p_del.add_argument(
        "-S",
        "--scout",
        action="store_true",
        help="read-only plan only (no diff) — for a pre-implementation scout round",
    )
    p_del.add_argument("-C", "--cwd", default=None, help="repo dir Codex works in (codex -C)")
    p_del.add_argument("-r", "--round", type=int, default=1, help="loop round (1-based)")
    p_del.add_argument(
        "-f", "--feedback", default=None, help="Claude's review feedback for a revision round"
    )
    p_del.add_argument(
        "-m", "--model", default=None, help="override model (else resolved from config)"
    )
    p_del.add_argument(
        "-e",
        "--effort",
        choices=EFFORTS,
        default=None,
        help="override reasoning effort (else config/model default)",
    )
    p_del.add_argument(
        "-t", "--timeout", type=int, default=600, help="seconds before giving up (default 600)"
    )
    p_del.add_argument(
        "-j",
        "--max-concurrent",
        type=int,
        default=None,
        metavar="N",
        help="cap simultaneous Codex runs across all delegate processes "
        "(default 3, or $CODEX_IN_CLAUDE_MAX_CONCURRENT; 0 = unlimited)",
    )
    p_del.add_argument(
        "-Q",
        "--ignore-quota",
        action="store_true",
        help="skip the quota preflight (run even when a rate-limit window is exhausted)",
    )
    p_del.set_defaults(func=cmd_delegate)

    p_usage = sub.add_parser("usage", help="show current Codex rate-limit usage (5h + weekly)")
    p_usage.add_argument("-j", "--json", action="store_true", help="machine-readable JSON")
    p_usage.set_defaults(func=cmd_usage)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse args and dispatch to the chosen subcommand; returns its exit code."""
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
