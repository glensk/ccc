"""Parked prompts: register a ready-made prompt now, auto-fire it at token reset.

The composer draft of a Claude Code session lives only in process memory, so a
prompt "typed but not sent" can never be captured after the fact. ``ccc park``
replaces that habit: the prompt is registered FIRST as a persistent future-job
draft row (armed with ``fire_at``/``fire_window``), then this process waits in
the same terminal tab — live countdown in the tab title — and launches the job
through the one canonical path (``cmd_start_job``: account pin, guards, atomic
claim) when the selected rate-limit window resets. If the tab dies, the armed
row survives and the daemon's :func:`~command_center.daemon` fallback fires it
in a new tab instead.

Scheduling is deterministic: the fire time is the selected window's
``resets_at`` plus a small buffer, regardless of utilization — "how used is the
window" is shown to the user but never decides the schedule. Missing or
unusable usage data is a loud registration error, never treated as "available
now". At fire time the job's OWN window (and only that window — an exhausted
Fable-weekly must not hold back a five-hour job) can postpone the launch when a
FRESH authoritative snapshot still shows it 100% used; that predicate is
convergent because utilization falls below 100 at the window boundary, unlike
``resets_at`` which always lies ahead once any new usage lands.
"""

# Lazy in-function imports keep `ccc` CLI startup fast and break the cli<->park
# import cycle (cmd_park lives in cli, the fire path calls back into cmd_start_job).
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import argparse
import os
import select
import shlex
import subprocess
import sys
import tempfile
import time
from typing import IO

from . import accounts, config, terminal, usage
from .store import Store

WINDOW_CHOICES = ("five_hour", "seven_day", "fable_week")
DEFAULT_BUFFER_SEC = 90
# Conservative argv budget: macOS ARG_MAX is ~1 MB including the environment; the
# prompt travels as ONE argv element of `claude`, so cap well below the ceiling.
MAX_PROMPT_BYTES = 200_000
# The daemon dispatches armed jobs only this far past fire_at, so a live foreground
# waiter (which fires exactly on time) always wins the race for its own tab.
FIRE_GRACE_SEC = 120
# Re-arm-forward lease: the daemon pushes fire_at this far ahead BEFORE dispatching,
# so a crash mid-dispatch (or a tab that opened but never ran) retries — never lost.
FIRE_RETRY_SEC = 900
# postpone_until only trusts an OAuth fetch at most this old (fresh exhaustion).
POSTPONE_FRESH_SEC = 600
_POSTPONE_SLACK_SEC = 60
_TICK_SEC = 5.0


# ---- pure helpers (unit-tested) --------------------------------------------


def _window(snapshot: usage.Usage | None, name: str) -> usage.Window | None:
    if snapshot is None or name not in WINDOW_CHOICES:
        return None
    win = getattr(snapshot, name, None)
    return win if isinstance(win, usage.Window) else None


def fire_time(
    snapshot: usage.Usage | None,
    window: str,
    now: int,
    buffer_sec: int = DEFAULT_BUFFER_SEC,
) -> int | None:
    """Epoch second a prompt parked on *window* fires, or ``None`` without a usable reset.

    Deterministic: the window's ``resets_at`` + *buffer_sec*, regardless of
    utilization. ``None`` when the snapshot is missing, the window is absent, or its
    ``resets_at`` is not in the future — the caller must treat that as UNKNOWN (a
    registration error after one fetch attempt), never as "available now".
    """
    win = _window(snapshot, window)
    if win is None or win.resets_at <= now:
        return None
    return int(win.resets_at) + max(0, int(buffer_sec))


def postpone_until(
    snapshot: usage.Usage | None,
    fire_window: str,
    now: int,
    *,
    buffer_sec: int = DEFAULT_BUFFER_SEC,
    min_fresh_sec: int = POSTPONE_FRESH_SEC,
) -> int | None:
    """New fire time when the job's OWN window is freshly observed still exhausted.

    Only the job's selected window counts (an exhausted ``fable_week`` never holds a
    ``five_hour`` job), and only a fresh authoritative snapshot
    (``oauth_fetched_at`` within *min_fresh_sec*) showing >= 100% utilization with a
    future reset postpones. Stale or missing data returns ``None``: fire at the
    recorded time rather than holding a job on unknown data.
    """
    if snapshot is None or not fire_window:
        return None
    if now - snapshot.oauth_fetched_at > min_fresh_sec:
        return None
    win = _window(snapshot, fire_window)
    if win is None:
        return None
    if win.used_percentage >= 100.0 and win.resets_at > now + _POSTPONE_SLACK_SEC:
        return int(win.resets_at) + max(0, int(buffer_sec))
    return None


def _span(seconds: int) -> str:
    """Compact duration: ``45s`` / ``37m`` / ``2h 05m`` / ``2d 3h``."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        hours, minutes = divmod(seconds // 60, 60)
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(seconds // 3600, 24)
    return f"{days}d {hours}h"


def format_fire(fire_at: int, now: int | None = None) -> str:
    """Human line for an armed fire time — overdue-aware, never a silent "now".

    ``fires 14:03 (in 37m)`` ahead of time, ``fires now`` inside the dispatch grace,
    ``overdue 37m`` past it (both dispatchers failed — the row must say so instead
    of claiming "now" forever).
    """
    now = int(time.time()) if now is None else now
    delta = int(fire_at) - now
    hhmm = time.strftime("%H:%M", time.localtime(int(fire_at)))
    if delta > 60:
        return f"fires {hhmm} (in {_span(delta)})"
    if delta >= -FIRE_GRACE_SEC:
        return "fires now"
    return f"overdue {_span(-delta)}"


def prompt_size_error(prompt: str) -> str | None:
    """Reject prompts that could not survive the single-argv launch (E2BIG)."""
    size = len(prompt.encode("utf-8", errors="replace"))
    if size > MAX_PROMPT_BYTES:
        return (
            f"error: prompt is {size} bytes — larger than the {MAX_PROMPT_BYTES} byte "
            'argv budget for `claude "<prompt>"`; split it or reference a file instead'
        )
    return None


# ---- prompt acquisition -----------------------------------------------------


def _resolve_prompt(  # pylint: disable=too-many-return-statements  # one per source
    args: argparse.Namespace,
) -> tuple[str | None, str | None]:
    """The prompt text from (in precedence order) argv, clipboard, piped stdin, $EDITOR.

    Returns ``(prompt, error)`` — exactly one is set.
    """
    if getattr(args, "prompt", None):
        return str(args.prompt), None
    if getattr(args, "clipboard", False):
        try:
            out = subprocess.run(["pbpaste"], capture_output=True, check=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"error: could not read the clipboard (pbpaste): {exc}"
        text = out.stdout.decode("utf-8", errors="replace").strip()
        return (text, None) if text else (None, "error: clipboard is empty")
    if not sys.stdin.isatty():
        text = sys.stdin.read().strip()
        return (text, None) if text else (None, "error: empty prompt on stdin")
    editor = os.environ.get("EDITOR") or "vi"
    fd, path = tempfile.mkstemp(prefix="ccc-park-", suffix=".md")
    os.close(fd)
    try:
        code = subprocess.call([*shlex.split(editor), path])
        if code != 0:
            return None, f"error: editor exited {code} — nothing parked"
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read().strip()
    except OSError as exc:
        return None, f"error: could not run $EDITOR ({editor}): {exc}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return (text, None) if text else (None, "error: empty prompt — nothing parked")


# ---- the waiter --------------------------------------------------------------


def _control_tty() -> IO[bytes] | None:
    """The controlling terminal for Enter-detection — NOT stdin.

    A piped prompt leaves stdin at EOF (permanently ``select``-readable), which
    would turn "Enter fires early" into an instant unintended launch. Without a
    controlling TTY early-fire is disabled and the waiter is a pure timer.
    """
    try:
        return open("/dev/tty", "rb", buffering=0)  # noqa: SIM115 - caller closes
    except OSError:
        return None


def _await_enter(tty: IO[bytes] | None, timeout: float) -> bool:
    """True when a line arrives on *tty* within *timeout* (False = tick elapsed)."""
    if tty is None:
        time.sleep(max(0.0, timeout))
        return False
    ready, _, _ = select.select([tty], [], [], max(0.0, timeout))
    if not ready:
        return False
    return bool(tty.readline())


def _status_line(text: str) -> None:
    sys.stdout.write(f"\r\033[K{text}")
    sys.stdout.flush()


def _wait_until_fire(  # pylint: disable=too-many-locals  # one countdown loop, flat on purpose
    session_id: str, fire_at: int, fire_window: str, label: str, *, no_auto: bool
) -> str:
    """Count down in this tab until the job should launch.

    Returns ``"fire"`` or ``"cancel"``. Absolute wall-clock deadline (Mac sleep/wake
    safe); the tab title carries the countdown; Enter (on the controlling TTY) fires
    early; Ctrl-C cancels. At the deadline one fresh usage fetch may postpone when
    the job's own window is still exhausted (see :func:`postpone_until`) — the DB row
    is kept in step so the daemon fallback agrees with what the user sees.
    """
    tty = _control_tty()
    last_title = ""
    checked_postpone = False
    try:
        while True:
            now = int(time.time())
            remaining = fire_at - now
            if remaining <= 0:
                if not checked_postpone:
                    checked_postpone = True
                    usage.fetch_claude_usage(label)  # best-effort refresh (throttling inside)
                    postponed = postpone_until(usage.read_usage(label), fire_window, now)
                    if postponed is not None and postponed > fire_at:
                        fire_at = postponed
                        checked_postpone = False
                        with Store() as store:
                            store.update_fields(session_id, fire_at=fire_at)
                        print(
                            f"\n{fire_window} window still exhausted — "
                            f"postponed: {format_fire(fire_at, now)}"
                        )
                        continue
                if no_auto:
                    # Manual mode: disarm in the DB first, or the daemon would fire
                    # this job in a NEW tab while we sit here waiting for Enter.
                    with Store() as store:
                        store.update_fields(session_id, fire_at=0)
                    terminal.set_tab("🟢 ready", None)
                    sys.stdout.write("\a")
                    _status_line("🟢 tokens reset — press Enter to launch (Ctrl-C keeps the job)")
                    if tty is None:
                        input()
                    else:
                        tty.readline()
                return "fire"
            hours, rest = divmod(remaining, 3600)
            minutes, seconds = divmod(rest, 60)
            clock = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
            hhmm = time.strftime("%H:%M", time.localtime(fire_at))
            enter_hint = " — Enter = now · Ctrl-C = cancel" if tty is not None else ""
            _status_line(f"⏳ fires {hhmm} (in {clock}){enter_hint}")
            title = f"⏳{_span(remaining)} → auto"
            if title != last_title:
                terminal.set_tab(title, None)
                last_title = title
            if _await_enter(tty, min(_TICK_SEC, remaining)):
                return "fire"
    except KeyboardInterrupt:
        return "cancel"
    finally:
        if tty is not None:
            tty.close()
        sys.stdout.write("\n")
        sys.stdout.flush()


# ---- the command --------------------------------------------------------------


def run_park(args: argparse.Namespace) -> int:  # pylint: disable=too-many-return-statements,too-many-branches,too-many-statements,too-many-locals
    """``ccc park`` — register a parked prompt, wait in this tab, launch at reset."""
    from .models import short_id

    prompt, err = _resolve_prompt(args)
    if err or prompt is None:
        print(err or "error: no prompt", file=sys.stderr)
        return 1
    size_err = prompt_size_error(prompt)
    if size_err:
        print(size_err, file=sys.stderr)
        return 1

    now = int(time.time())
    window = getattr(args, "window", "five_hour")
    buffer_sec = int(getattr(args, "buffer", DEFAULT_BUFFER_SEC))
    config_dir = accounts.env_config_dir()
    label = accounts.effective_account_label(config_dir)

    fire_at = 0
    if not getattr(args, "now", False):
        # Window boundaries don't move, so a cached resets_at that is still in the
        # future is usable as-is; fetch only when the cache yields nothing.
        snapshot = usage.read_usage(label)
        fire_at_opt = fire_time(snapshot, window, now, buffer_sec)
        if fire_at_opt is None:
            snapshot = usage.fetch_claude_usage(label) or snapshot
            fire_at_opt = fire_time(snapshot, window, now, buffer_sec)
        if fire_at_opt is None:
            print(
                f"error: no usable {window} reset time for account '{label}' — run one "
                "Claude turn (the statusline captures usage) or check `ccc doctor`, "
                "or park with -N/--now to launch immediately",
                file=sys.stderr,
            )
            return 1
        fire_at = fire_at_opt
        win = _window(snapshot, window)
        used = f"{win.used_percentage:.0f}%" if win is not None else "?"
        print(f"{window} window at {used} for '{label}' — {format_fire(fire_at, now)}")
        # Courtesy only (utilization never decides the schedule): a barely-used
        # window usually means the park was reflex, not need — offer to start now.
        if win is not None and win.used_percentage < 50 and sys.stdin.isatty():
            try:
                answer = input("window is <50% used — schedule anyway? [Y = wait / n = start now] ")
            except (EOFError, KeyboardInterrupt):
                print("\nnothing parked")
                return 130
            if answer.strip().lower() in ("n", "no", "now"):
                fire_at = 0

    import uuid

    session_id = str(uuid.uuid4())
    aim = (getattr(args, "aim", None) or "").strip()
    if not aim:
        first = next(
            (line.strip() for line in prompt.splitlines() if line.strip()), "parked prompt"
        )
        aim = first[:80]
    cwd = os.getcwd()
    with Store() as store:
        store.create_draft(
            session_id,
            cwd,
            aim,
            prompt=prompt,
            config_dir=config_dir,
            fire_at=fire_at,
            fire_window=(window if fire_at else ""),
        )
    cfg = config.load_config()
    if cfg.future_files:
        from .spawn import spawn_ccc

        spawn_ccc(["sync-future"])  # mirror the row; deliberately NO score-aim spawn
    print(f"parked as future job {short_id(session_id)} — the prompt is safe in the store")

    if fire_at:
        outcome = _wait_until_fire(
            session_id, fire_at, window, label, no_auto=getattr(args, "no_auto", False)
        )
        if outcome == "cancel":
            with Store() as store:
                store.update_fields(session_id, fire_at=0)
            from .colors import short_folder

            terminal.set_tab(short_folder(cwd), None)
            print(
                f"cancelled — kept as future job {short_id(session_id)} (auto-fire off); "
                f"start it with:  ccc start-job {session_id}"
            )
            return 130

    terminal.set_tab(aim[:24], None)
    # Same tab, same process family: launch through the ONE canonical path (account
    # pin, guards, atomic claim, resume-awareness). cmd_start_job execs claude and
    # never returns on success. Imported here to avoid a module-level cycle with cli.
    from . import cli

    rc = cli.cmd_start_job(argparse.Namespace(session_id=session_id, force=False, auto=True))
    print(
        f"launch failed (exit {rc}) — the job survives; retry with: ccc start-job {session_id}",
        file=sys.stderr,
    )
    return rc
