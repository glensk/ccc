# Delegating implementation to OpenAI Codex

`ccc` can hand the **implementation** of a task to OpenAI Codex and have Claude only
*oversee* it — so the heavy generation runs on your Codex (ChatGPT) subscription rather
than on Anthropic tokens. This is entirely optional and requires the `codex` CLI on PATH.

## From a Claude Code session

```commands
/codex-implement-task-and-claude-review [--write] [--no-takeover] [model] <task>
```

It runs a bounded loop: an optional read-only **scout** round (plan) → Codex implements and
self-checks → Claude verifies by running the project's checks → on failure Claude gives
concrete feedback → Codex revises. If Codex still fails after round 3, Claude announces it
and takes over (unless `--no-takeover`). The **first output line is always the model**,
e.g. `model: gpt-5.5 (effort xhigh)`.

Two design points worth keeping:

- **Codex does the code discovery, not Claude.** Claude does not pre-read the repo to
  "build the task" — that would duplicate the reading Codex must do anyway and burn the
  very tokens this command saves. Claude supplies only intent + acceptance criteria; Codex
  (running `-C <repo>`) reads the code itself.
- **Event-driven hand-off.** Each Codex round runs in the background and the harness
  re-invokes Claude the instant it finishes — no fixed wait, no polling.

Modes:

- **Default (patch)** keeps Codex read-only: it returns a `git apply`-able diff that Claude
  applies and verifies — your global Codex read-only lockout is untouched.
- **`--write`** lets Codex edit files directly (`workspace-write`, that call only) and run
  the tests itself; Claude reviews the resulting git diff.

## The model / effort manager: `codex-in-claude.py`

One script governs the Codex **model + reasoning effort** for both the delegate command
and the adversarial `/codex-debate`. It is on PATH and called by bare name (so the repo can
move):

```commands
codex-in-claude.py models                                    # list models (* = configured)
codex-in-claude.py set-model gpt-5.5 --for delegate-review   # or --for debate / --for all
codex-in-claude.py get-model --for debate
codex-in-claude.py set-effort high                           # low|medium|high|xhigh|default
codex-in-claude.py usage [--json]                            # Codex 5h + weekly quota
codex-in-claude.py delegate [--write] [--scout] -C <repo> "<task>"  # one round; prints model first
```

`delegate` is the single engine both the skill and the slash command drive. It prints
`model: <slug> (effort <e>)` as its guaranteed first stdout line, caps simultaneous Codex
runs with a cross-process semaphore (tapered from live quota), and preflights the quota —
a run that would start ≥100% used exits with a distinct code and the reset time, *without*
launching Codex. Environment kill-switches: `CCC_NO_CODEX=1` disables all Codex use for
the session/shell.

Configuration lives in `~/.config/codex-in-claude/config.json` (override with
`$CODEX_IN_CLAUDE_CONFIG`). Resolution is per-command → `default` → the latest Codex model;
the effort is a single global key. Keep the engine **read-only by default** — `--write` is
the only path that overrides Codex's global read-only lockout, per call.

## As a future job

The same selector powers **future jobs**. A draft created with `-j codex` (a `git apply`
patch Claude verifies) or `-j codex-write` (Codex edits directly) launches straight into
`/codex-implement-task-and-claude-review` when you start it — so a parked task gets done by
Codex and verified by Claude:

```commands
ccc new-job -a "add retry with backoff to the fetch client" -c work/api-gateway -j codex
```

Codex-workflow sessions are marked in `ccc` with an inverse **`OAI`** badge in the version
column (including manually-invoked ones detected from the transcript). A Codex-workflow
session that is idle while its Codex quota window is exhausted shows a `😴` status until the
window resets.
