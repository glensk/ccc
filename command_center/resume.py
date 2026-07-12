"""Auto-resume session-limit-halted sessions once the rate limit resets.

When the shared Claude account hits its session/rate limit, tracked sessions stall
(``Status.HALTED``: their last main-chain assistant turn is a rate-limit error).
This module resumes them automatically once the limit resets, via the existing
``claude-session-continue.py`` script — **staggered ~2 min across repos** and
**strictly serial within a repo** (the next starts only after the prior session's
turn produces a completed transcript turn and goes idle).

Design (kept testable): a **pure planner** ``plan()`` maps observed live/transcript
state + the persisted queue to a list of effect-free :class:`Action`s and the next
:class:`QueueState`; an effectful executor :func:`apply_actions` performs them
(reap a stuck REPL, open a resume tab, spawn the reset detector, notify). The
``--watch`` loop is an flock singleton spawned by the daemon when work exists.

Reset detection is explicit, not transcript-inferred: a single headless
``claude-session-continue.py --wait-only --signal-file <f>`` reuses the script's
verified probe/verify, then touches ``<f>``; that file is the reset gate.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .adapters.claude import ClaudeAdapter
from .core import reconcile
from .models import LiveSession, now_ms
from .notify import notify
from .store import Store

# Terminal entry states never re-launched; live ones drive dispatch.
_TERMINAL = ("done", "failed")
_IN_FLIGHT = ("launching", "running")

_NOTIFIED: set[str] = set()  # one-shot notify keys for this watcher process
_REPO_CACHE: dict[str, str] = {}


# ---------------------------------------------------------------------------
# data records
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    """A halted session eligible for auto-resume."""

    session_id: str
    cwd: str
    repo: str


@dataclass
class Observation:
    """The live + transcript facts the planner needs for one session this tick."""

    alive: bool
    raw_status: str  # busy | idle | waiting | "" (parked)
    halted: bool
    transcript_size: int
    cwd: str
    repo: str


@dataclass
class Entry:
    """One session's slot in the resume queue."""

    session_id: str
    repo: str
    cwd: str
    state: str = "queued"  # queued | launching | running | done | failed
    launched_at: int = 0  # epoch ms a resume was dispatched
    baseline_offset: int = 0  # transcript size at launch (progress is growth past it)
    attempts: int = 0
    fail_reason: str = ""


@dataclass
class QueueState:
    """Persisted orchestration state (single-writer: the flock watcher)."""

    reset_confirmed_at: int = 0  # epoch ms the limit was confirmed reset (0 = waiting)
    last_launch_at: int = 0  # epoch ms of the last real resume (global stagger gate)
    reset_wait_pid: int = 0  # pid of the headless --wait-only detector (0 = none)
    entries: dict[str, Entry] = field(default_factory=dict)


@dataclass
class Action:
    """A side effect the executor performs (effect-free in the planner)."""

    kind: str  # reap | launch_resume | ensure_reset_wait | confirm_reset | notify
    session_id: str = ""
    cwd: str = ""
    detail: str = ""


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def _state_path() -> Path:
    return config.app_home() / "resume_queue.json"


def _signal_path() -> Path:
    return config.app_home() / "resume_reset.signal"


def _lock_path() -> Path:
    return config.app_home() / "resume_watch.lock"


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------
def repo_of(cwd: str) -> str:
    """Git toplevel of *cwd* (the repo key for serialization); fallback *cwd*."""
    if not cwd:
        return ""
    if cwd in _REPO_CACHE:
        return _REPO_CACHE[cwd]
    top = cwd
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            top = proc.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    _REPO_CACHE[cwd] = top
    return top


def _bills_non_default(config_dir: str) -> bool:
    """True when auto-resuming *config_dir*'s session could bill a NON-default account.

    Auto-resume owns a SINGLE reset gate — the default account's limit (per-account
    reset detectors are out of scope, D12). In single-account mode nothing is
    non-default. In multi-account mode a session is fail-closed non-default unless its
    ``config_dir`` provably resolves to the default account: an unknown ("") account is
    treated as non-default so a not-yet-attributed work session can never slip through.
    """
    from . import accounts

    if not accounts.is_multi_account():
        return False
    return config_dir == "" or not accounts.is_default_config_dir(config_dir)


def candidates(store: Store, adapter: ClaudeAdapter) -> list[Candidate]:
    """Halted sessions eligible for auto-resume (alive HALTED or parked-after-429).

    Excludes done / draft / archived; requires a real cwd and a transcript on disk
    (``claude --resume`` needs a recorded conversation). Fail-closed on multi-account:
    a session that would bill a non-default account is skipped (D12) — auto-resume's
    reset gate only tracks the default account's limit.
    """
    out: list[Candidate] = []
    for session in store.list_sessions():
        if session.done or session.draft or session.archived:
            continue
        if _bills_non_default(session.config_dir):
            continue
        if not session.cwd or not os.path.isdir(session.cwd):
            continue
        if not adapter.is_halted(session.cwd, session.session_id):
            continue
        if adapter.transcript_path(session.cwd, session.session_id, session.config_dir) is None:
            continue
        out.append(Candidate(session.session_id, session.cwd, repo_of(session.cwd)))
    return out


def purge_non_default_entries(store: Store, state: QueueState) -> None:
    """Drop any queued entry that would bill a non-default account (D12).

    ``resume.py`` observes ``candidate_ids | set(state.entries)``, so an entry queued
    while single-account (or before an account was attributed) can reach ``plan()`` and
    dispatch through the single default-account reset gate. Purge those BEFORE
    ``_observe()``/``plan()`` so a non-default session is never auto-resumed. Logged.
    """
    for session_id in list(state.entries):
        session = store.get(session_id)
        config_dir = session.config_dir if session else ""
        if _bills_non_default(config_dir):
            del state.entries[session_id]
            print(
                f"resume-halted: dropped {session_id[:8]} — bills a non-default Claude "
                "account; auto-resume only handles the default account's reset."
            )


def _transcript_size(adapter: ClaudeAdapter, cwd: str, session_id: str) -> int:
    path = adapter.transcript_path(cwd, session_id)
    if path is None:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _observe(adapter: ClaudeAdapter, store: Store, ids: set[str]) -> dict[str, Observation]:
    """Build the per-session :class:`Observation` map the planner consumes."""
    live: dict[str, LiveSession] = {ls.session_id: ls for ls in adapter.discover()}
    observed: dict[str, Observation] = {}
    for session_id in ids:
        session = store.get(session_id)
        cwd = session.cwd if session else ""
        live_session = live.get(session_id)
        observed[session_id] = Observation(
            alive=bool(live_session and live_session.alive),
            raw_status=(live_session.raw_status if live_session else ""),
            halted=adapter.is_halted(cwd, session_id) if cwd else False,
            transcript_size=_transcript_size(adapter, cwd, session_id) if cwd else 0,
            cwd=cwd,
            repo=repo_of(cwd) if cwd else "",
        )
    return observed


# ---------------------------------------------------------------------------
# pure planner
# ---------------------------------------------------------------------------
def _is_idle(raw_status: str) -> bool:
    return raw_status in ("idle", "waiting") or raw_status.startswith("wait")


def _fail_or_requeue(entry: Entry, cfg: config.Config, reason: str, actions: list[Action]) -> None:
    """Bounded retry: requeue until ``resume_max_attempts``, then fail + notify."""
    entry.attempts += 1
    if entry.attempts >= cfg.resume_max_attempts:
        entry.state = "failed"
        entry.fail_reason = reason
        actions.append(Action("notify", entry.session_id, detail=f"resume failed: {reason}"))
    else:
        entry.state = "queued"


def plan(  # pylint: disable=too-many-branches,too-many-locals,too-many-statements
    observed: dict[str, Observation],
    candidate_ids: set[str],
    state: QueueState,
    now: int,
    cfg: config.Config,
    reset_signal: bool,
) -> tuple[QueueState, list[Action]]:
    """Pure: given observed state + the queue, return the next queue + actions.

    No side effects — the executor performs the returned actions and persists the
    returned state. This is the unit-tested heart of the feature.
    """
    state = copy.deepcopy(state)
    actions: list[Action] = []

    # 1. Enqueue new candidates. A previously-done session that halted AGAIN in a
    #    later window is re-queued fresh; failed entries stay suppressed.
    for session_id in candidate_ids:
        entry = state.entries.get(session_id)
        obs = observed.get(session_id)
        if entry is None:
            state.entries[session_id] = Entry(
                session_id=session_id,
                repo=obs.repo if obs else "",
                cwd=obs.cwd if obs else "",
            )
        elif entry.state == "done":
            entry.state = "queued"
            entry.attempts = 0
            entry.launched_at = 0
            entry.baseline_offset = 0

    # 2. Reconcile + classify. A queued entry the user already resumed (alive, not
    #    halted) is adopted as in-flight rather than relaunched (no double resume).
    rehalt = False
    for session_id, entry in list(state.entries.items()):
        if entry.state in _TERMINAL:
            continue
        obs = observed.get(session_id) or Observation(False, "", False, 0, entry.cwd, entry.repo)

        if entry.state == "queued":
            if obs.alive and not obs.halted:  # someone resumed it out-of-band (no double launch)
                entry.state = "running"
                entry.launched_at = entry.launched_at or now
                entry.baseline_offset = 0  # finish hinges on idle, not further growth
            continue

        # in-flight (launching | running). "resumed" = transcript grew past the launch
        # baseline (a real resume produced content) — the persistent, poll-timing-
        # independent signal (Codex O5), not a transient "seen busy" flag.
        resumed = obs.transcript_size > entry.baseline_offset
        if obs.halted:  # the account-wide limit is back → requeue + re-gate everyone
            rehalt = True
            _fail_or_requeue(entry, cfg, "re-halted on the limit", actions)
            continue
        if resumed and (_is_idle(obs.raw_status) or not obs.alive):  # turn completed → free repo
            entry.state = "done"
            actions.append(Action("notify", session_id, detail="resumed and finished its turn"))
            continue
        if resumed and entry.state == "launching":
            entry.state = "running"  # the resume took; its turn is in progress
        # Fail only when the resume never took: no progress AND no live process past the
        # grace window. A live-but-slow turn is left alone (never reap a working session).
        if (
            not resumed
            and not obs.alive
            and now - entry.launched_at > cfg.resume_launch_timeout_sec * 1000
        ):
            _fail_or_requeue(entry, cfg, "no resume progress before timeout", actions)
            continue
        # else: still launching/running — leave in place

    if rehalt:
        state.reset_confirmed_at = 0  # limit returned; wait for the next reset

    # 3. Reset gate — do not dispatch any resume until the limit is confirmed reset.
    if not state.reset_confirmed_at:
        if reset_signal:
            state.reset_confirmed_at = now
            actions.append(Action("confirm_reset"))
        else:
            if any(e.state == "queued" for e in state.entries.values()):
                actions.append(Action("ensure_reset_wait"))
            state.entries = {s: e for s, e in state.entries.items() if e.state != "done"}
            return state, actions

    # 4. Dispatch — one launch per tick (global stagger), one in-flight per repo.
    busy_repos = {e.repo for e in state.entries.values() if e.state in _IN_FLIGHT}
    if now - state.last_launch_at >= cfg.resume_stagger_sec * 1000:
        for session_id, entry in state.entries.items():
            if entry.state != "queued" or entry.repo in busy_repos:
                continue
            obs = observed.get(session_id)
            if obs and obs.alive:  # stuck live REPL: kill it before re-resuming
                actions.append(Action("reap", session_id))
            actions.append(Action("launch_resume", session_id, cwd=entry.cwd))
            entry.state = "launching"
            entry.launched_at = now
            entry.baseline_offset = obs.transcript_size if obs else 0
            state.last_launch_at = now
            break  # global stagger: at most one resume dispatched per tick

    state.entries = {s: e for s, e in state.entries.items() if e.state != "done"}
    return state, actions


# ---------------------------------------------------------------------------
# executor
# ---------------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _notify_once(cfg: config.Config, message: str) -> None:
    if message in _NOTIFIED:
        return
    _NOTIFIED.add(message)
    notify("ccc resume-halted", message, cfg.notify)


def _resolve_continue_script(cfg: config.Config) -> str:
    """Path to claude-session-continue (config override → entry point → legacy ``.py``).

    The packaged ``claude-session-continue`` console script is preferred over the
    historical ``claude-session-continue.py`` so a wheel install resolves to its own
    entry point; ``resume_continue_script`` overrides both.
    """
    import shutil

    if cfg.resume_continue_script:
        return cfg.resume_continue_script
    return (
        shutil.which("claude-session-continue") or shutil.which("claude-session-continue.py") or ""
    )


def _reap_fresh(adapter: ClaudeAdapter, store: Store, session_id: str) -> None:
    """SIGTERM the session's *fresh* live pid (never a stored one), then close its pane.

    Re-reads the live registry at kill time so a stale/reused stored pid is never
    signalled (Codex O4). The pane is closed only after the process is confirmed gone.
    """
    live = {ls.session_id: ls for ls in adapter.discover()}
    live_session = live.get(session_id)
    if live_session is None or not live_session.alive or live_session.pid <= 0:
        return
    from .daemon import _reap  # SIGTERM → SIGKILL; reuse the daemon's reaper

    _reap(live_session.pid)
    for _ in range(15):  # wait up to ~3s for the registry entry to disappear
        time.sleep(0.2)
        if not any(ls.session_id == session_id and ls.alive for ls in adapter.discover()):
            break
    session = store.get(session_id)
    if session and session.iterm_session_id:
        from . import terminal

        terminal.close_iterm_session(session.iterm_session_id)


def _launch_resume(session_id: str, cwd: str, cfg: config.Config, config_dir: str = "") -> bool:
    from . import terminal

    script = _resolve_continue_script(cfg)
    if not script:
        return False
    return terminal.resume_halted_in_new_tab(cwd, session_id, script, config_dir)


def _consume_reset_signal(state: QueueState) -> None:
    """Reset confirmed: remove the signal file and stop the detector."""
    try:
        _signal_path().unlink(missing_ok=True)
    except OSError:
        pass
    if state.reset_wait_pid and _pid_alive(state.reset_wait_pid):
        try:
            os.kill(state.reset_wait_pid, 15)
        except OSError:
            pass
    state.reset_wait_pid = 0


def _ensure_reset_wait(state: QueueState, cfg: config.Config) -> None:
    """Make sure exactly one headless ``--wait-only`` reset detector is running."""
    if state.reset_wait_pid and _pid_alive(state.reset_wait_pid):
        return  # a detector is already waiting — leave its signal file alone
    signal = _signal_path()
    try:
        signal.unlink(missing_ok=True)  # clear any stale signal before a fresh wait
    except OSError:
        pass
    script = _resolve_continue_script(cfg)
    if not script:
        _notify_once(cfg, "claude-session-continue not found — set resume_continue_script")
        return
    from . import accounts

    try:
        config.app_home().mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(  # noqa: S603  # detached headless reset detector
            [script, "auto", "--wait-only", "--signal-file", str(signal)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            # The reset detector probes the DEFAULT account's limit (auto-resume only
            # handles that account) — pin its env so an ambient work CLAUDE_CONFIG_DIR
            # can't make it probe/bill the wrong seat (D8).
            env=accounts.launch_env(""),
        )
        state.reset_wait_pid = proc.pid
    except OSError:
        state.reset_wait_pid = 0


def _reset_signal_present() -> bool:
    return _signal_path().exists()


def apply_actions(
    actions: list[Action],
    state: QueueState,
    store: Store,
    adapter: ClaudeAdapter,
    cfg: config.Config,
) -> None:
    """Perform the planner's effects (mutates *state* for reset-wait bookkeeping)."""
    for action in actions:
        if action.kind == "reap":
            _reap_fresh(adapter, store, action.session_id)
        elif action.kind == "launch_resume":
            resumed = store.get(action.session_id)
            config_dir = resumed.config_dir if resumed else ""
            if not _launch_resume(action.session_id, action.cwd, cfg, config_dir):
                _notify_once(
                    cfg, "cannot open a terminal to resume — is iTerm/osascript available?"
                )
        elif action.kind == "ensure_reset_wait":
            _ensure_reset_wait(state, cfg)
        elif action.kind == "confirm_reset":
            _consume_reset_signal(state)
        elif action.kind == "notify":
            notify("ccc resume-halted", action.detail, cfg.notify)


# ---------------------------------------------------------------------------
# tick / watch / cli
# ---------------------------------------------------------------------------
def _is_drained(state: QueueState) -> bool:
    return not any(e.state in _IN_FLIGHT or e.state == "queued" for e in state.entries.values())


def _summary(state: QueueState, actions: list[Action]) -> str:
    by_state: dict[str, int] = {}
    for entry in state.entries.values():
        by_state[entry.state] = by_state.get(entry.state, 0) + 1
    kinds = ", ".join(a.kind + (f":{a.session_id[:8]}" if a.session_id else "") for a in actions)
    reset = "reset✓" if state.reset_confirmed_at else "waiting-reset"
    states = " ".join(f"{k}={v}" for k, v in sorted(by_state.items())) or "(empty)"
    return f"[{reset}] {states}" + (f" | actions: {kinds}" if kinds else "")


def tick(cfg: config.Config, *, dry_run: bool = False) -> bool:
    """One orchestration step. Returns True when the queue is drained (watch can exit)."""
    adapter = ClaudeAdapter()
    with Store() as store:
        reconcile(store, adapter)
        cands = candidates(store, adapter)
        candidate_ids = {c.session_id for c in cands}
        state = load_state()
        # D12: fail closed BEFORE observe/plan — a queued non-default-account entry must
        # never dispatch through the single default-account reset gate.
        purge_non_default_entries(store, state)
        observed = _observe(adapter, store, candidate_ids | set(state.entries))
        new_state, actions = plan(
            observed, candidate_ids, state, now_ms(), cfg, _reset_signal_present()
        )
        if dry_run:
            print(f"[dry-run] candidates={len(cands)} {_summary(new_state, actions)}")
            return True
        apply_actions(actions, new_state, store, adapter, cfg)
        save_state(new_state)
        print(f"candidates={len(cands)} {_summary(new_state, actions)}")
        return _is_drained(new_state)


def watch(cfg: config.Config) -> int:
    """Run :func:`tick` on a poll loop until drained. flock singleton (exit if held)."""
    import fcntl

    config.app_home().mkdir(parents=True, exist_ok=True)
    lock_file = open(_lock_path(), "w", encoding="utf-8")  # noqa: SIM115  # held for the loop
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another resume-halted watcher is already running")
        lock_file.close()
        return 0
    try:
        while True:
            if tick(cfg):
                print("resume-halted: queue drained — exiting")
                return 0
            time.sleep(max(5, cfg.resume_poll_sec))
    except KeyboardInterrupt:
        return 130
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def has_candidates() -> bool:
    """True if any halted session is eligible for auto-resume (daemon spawn gate)."""
    adapter = ClaudeAdapter()
    with Store() as store:
        return bool(candidates(store, adapter))


# ---------------------------------------------------------------------------
# state persistence (atomic; single-writer)
# ---------------------------------------------------------------------------
def load_state() -> QueueState:
    """Load the queue state (empty on missing / corrupt — readers see whole files)."""
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return QueueState()
    if not isinstance(data, dict):
        return QueueState()
    entries: dict[str, Entry] = {}
    raw_entries = data.get("entries")
    if isinstance(raw_entries, dict):
        for session_id, raw in raw_entries.items():
            if not isinstance(raw, dict):
                continue
            entries[session_id] = Entry(
                session_id=str(raw.get("session_id", session_id)),
                repo=str(raw.get("repo", "")),
                cwd=str(raw.get("cwd", "")),
                state=str(raw.get("state", "queued")),
                launched_at=int(raw.get("launched_at", 0) or 0),
                baseline_offset=int(raw.get("baseline_offset", 0) or 0),
                attempts=int(raw.get("attempts", 0) or 0),
                fail_reason=str(raw.get("fail_reason", "")),
            )
    return QueueState(
        reset_confirmed_at=int(data.get("reset_confirmed_at", 0) or 0),
        last_launch_at=int(data.get("last_launch_at", 0) or 0),
        reset_wait_pid=int(data.get("reset_wait_pid", 0) or 0),
        entries=entries,
    )


def save_state(state: QueueState) -> None:
    """Persist the queue atomically (tmp + ``os.replace`` → readers never see a partial)."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "reset_confirmed_at": state.reset_confirmed_at,
        "last_launch_at": state.last_launch_at,
        "reset_wait_pid": state.reset_wait_pid,
        "entries": {sid: dataclasses.asdict(entry) for sid, entry in state.entries.items()},
    }
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"resume-halted: could not persist state: {exc}", file=sys.stderr)
