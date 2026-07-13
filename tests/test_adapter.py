"""Unit tests for the Claude adapter (uses a fake CLAUDE_HOME fixture)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from command_center.adapters import ClaudeAdapter
from command_center.models import CODEX_WORKFLOW_NAME


def _write_session(home: Path, pid: int, session_id: str, cwd: str, **extra: object) -> None:
    payload = {"pid": pid, "sessionId": session_id, "cwd": cwd, "status": "idle"}
    payload.update(extra)
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    (home / "sessions" / f"{pid}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_discover_and_liveness(tmp_path: Path) -> None:
    _write_session(tmp_path, os.getpid(), "alive-1", "/Users/x/repo", name="n", kind="interactive")
    _write_session(tmp_path, 999_999, "dead-1", "/Users/x/other")
    adapter = ClaudeAdapter(claude_home=tmp_path)

    by_id = {s.session_id: s for s in adapter.discover()}
    assert by_id["alive-1"].alive is True
    assert by_id["alive-1"].name == "n"
    assert by_id["alive-1"].kind == "interactive"
    assert by_id["dead-1"].alive is False


def test_discover_skips_headless_sdk(tmp_path: Path) -> None:
    # Real user sessions carry entrypoint "cli"; headless `claude -p` (the daemon's
    # own summary/grading calls) register with entrypoint "sdk-cli" — they must be
    # skipped so reconcile never persists them as junk "parked" rows. Distinct pids
    # keep the per-pid registry filenames from colliding.
    _write_session(tmp_path, 111, "real", "/Users/x/repo", entrypoint="cli")
    _write_session(tmp_path, 222, "headless", "/", entrypoint="sdk-cli")
    # Missing entrypoint (older Claude builds) defaults to "cli" and is kept.
    _write_session(tmp_path, 333, "legacy", "/Users/x/old")
    adapter = ClaudeAdapter(claude_home=tmp_path)

    by_id = {s.session_id: s for s in adapter.discover()}
    assert "headless" not in by_id
    assert by_id["real"].entrypoint == "cli"
    assert "legacy" in by_id


def test_transcript_path_encoding(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    cwd = "/home/user/projects/infra/home-assistant-sandbox"
    encoded = "-home-user-projects-infra-home-assistant-sandbox"
    target = tmp_path / "projects" / encoded / "sid.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n", encoding="utf-8")

    assert adapter.transcript_path(cwd, "sid") == target


def test_transcript_path_glob_fallback(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    target = tmp_path / "projects" / "weird-encoding" / "sid.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n", encoding="utf-8")

    # cwd does not match the directory name, so it must fall back to a glob.
    assert adapter.transcript_path("/does/not/match", "sid") == target


def test_transcript_path_caches_glob_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    target = tmp_path / "projects" / "weird-encoding" / "sid.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n", encoding="utf-8")

    # First resolution walks the glob fallback and memoizes the hit.
    assert adapter.transcript_path("/does/not/match", "sid") == target

    # A second resolution must be served from the cache: any glob call now fails loudly.
    def _no_glob(self: Path, pattern: str) -> list[Path]:
        raise AssertionError(f"glob should not run on a cache hit: {pattern}")

    monkeypatch.setattr(Path, "glob", _no_glob)
    assert adapter.transcript_path("/does/not/match", "sid") == target


def test_transcript_path_positive_cache_revalidates(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    encoded = "-repo"
    target = tmp_path / "projects" / encoded / "sid.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n", encoding="utf-8")

    assert adapter.transcript_path("/repo", "sid") == target

    # Deleting the file must invalidate the positive cache: the stale path is never
    # returned; with nothing left to find, resolution falls back to None.
    target.unlink()
    assert adapter.transcript_path("/repo", "sid") is None


def test_transcript_path_negative_ttl_exact_probe_still_lands(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    # Missing transcript → None, negatively cached.
    assert adapter.transcript_path("/repo", "sid") is None

    # Creating it at the EXACT munged path must be found despite the negative cache:
    # the exact-path probe stays live during the TTL, only the glob is skipped.
    target = tmp_path / "projects" / "-repo" / "sid.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n", encoding="utf-8")
    assert adapter.transcript_path("/repo", "sid") == target


def test_transcript_path_negative_ttl_delays_glob_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center.adapters import claude as claude_mod

    adapter = ClaudeAdapter(claude_home=tmp_path)
    # Missing transcript → None, negatively cached.
    assert adapter.transcript_path("/does/not/match", "sid") is None

    # A glob-only location is NOT discovered while the negative cache is trusted.
    target = tmp_path / "projects" / "weird-encoding" / "sid.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n", encoding="utf-8")
    assert adapter.transcript_path("/does/not/match", "sid") is None

    # Expiring the TTL re-enables glob discovery.
    monkeypatch.setattr(claude_mod, "_TRANSCRIPT_NEG_TTL", 0.0)
    assert adapter.transcript_path("/does/not/match", "sid") == target


def test_is_oneshot_headless(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)

    # Headless `claude -p` transcript: first record is the enqueued one-shot prompt.
    (proj / "headless.jsonl").write_text(
        json.dumps({"type": "queue-operation", "operation": "enqueue", "content": "..."})
        + "\n"
        + json.dumps({"type": "assistant"})
        + "\n",
        encoding="utf-8",
    )
    # Interactive transcript: opens with session meta, never a queue-operation.
    (proj / "real.jsonl").write_text(json.dumps({"type": "last-prompt"}) + "\n", encoding="utf-8")
    # Leading blank line must be skipped to reach the first real record.
    (proj / "blanky.jsonl").write_text(
        "\n" + json.dumps({"type": "queue-operation"}) + "\n", encoding="utf-8"
    )

    assert adapter.is_oneshot_headless("/repo", "headless") is True
    assert adapter.is_oneshot_headless("/repo", "blanky") is True
    assert adapter.is_oneshot_headless("/repo", "real") is False
    assert adapter.is_oneshot_headless("/repo", "missing") is False  # no transcript


def _rec(**fields: object) -> str:
    return json.dumps(fields)


def _err(text: str) -> str:
    """A rate-limit-style API-error assistant record carrying *text*."""
    return _rec(
        type="assistant",
        isApiErrorMessage=True,
        message={"role": "assistant", "content": [{"type": "text", "text": text}]},
    )


def test_is_halted(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)
    user = _rec(type="user", message={"role": "user", "content": "go"})
    ok_assistant = _rec(
        type="assistant",
        message={"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    )

    def write(name: str, *records: str) -> None:
        (proj / f"{name}.jsonl").write_text("\n".join(records) + "\n", encoding="utf-8")

    # Last turn is a weekly-limit halt → halted.
    write("weekly", user, ok_assistant, _err("You've hit your weekly limit · resets 2pm (Berlin)"))
    # 5-hour "session limit" halt is the same kind of block → also flagged.
    write("session", user, _err("You've hit your session limit · resets 1:10am (Berlin)"))
    # A user prompt that merely *quotes* the phrase is not an API error → not halted.
    write("quoted", _rec(type="user", message={"role": "user", "content": "show 'hit your limit'"}))
    # Resumed past the limit: a fresh *assistant* turn follows the error → no longer halted.
    write("resumed", _err("You've hit your weekly limit · resets 2pm"), user, ok_assistant)
    # Still waiting: a trailing *user* record after the halt (a queued "continue", a
    # background <task-notification>, a slash command) does NOT clear it — the session is
    # still rate-limited. Keying on the last *assistant* record keeps it halted and stops
    # the status flip-flopping HALTED↔WORKING (red ||↔green ▶) while no work is happening.
    write(
        "waiting_user_after",
        user,
        ok_assistant,
        _err("You've hit your weekly limit · resets 2pm"),
        user,
    )
    task_notif = _rec(
        type="user",
        message={"role": "user", "content": "<task-notification>done</task-notification>"},
    )
    write("waiting_task_notif", _err("You've hit your session limit · resets 1:10am"), task_notif)
    # The halt is on a sub-agent side-chain, not the main thread → ignored.
    sidechain_err = _rec(
        type="assistant",
        isApiErrorMessage=True,
        isSidechain=True,
        message={
            "role": "assistant",
            "content": [{"type": "text", "text": "hit your weekly limit"}],
        },
    )
    write("sidechain", user, ok_assistant, sidechain_err)
    # A non-rate-limit API error (overloaded, etc.) is not a halt.
    write("overloaded", user, _err("API Error: Overloaded"))

    assert adapter.is_halted("/repo", "weekly") is True
    assert adapter.is_halted("/repo", "session") is True
    assert adapter.is_halted("/repo", "quoted") is False
    assert adapter.is_halted("/repo", "resumed") is False
    assert adapter.is_halted("/repo", "waiting_user_after") is True
    assert adapter.is_halted("/repo", "waiting_task_notif") is True
    assert adapter.is_halted("/repo", "sidechain") is False
    assert adapter.is_halted("/repo", "overloaded") is False
    assert adapter.is_halted("/repo", "missing") is False  # no transcript


def test_is_halted_scans_only_tail_of_large_transcript(tmp_path: Path) -> None:
    """The halt is the final line; a multi-MB prefix before it must not hide it."""
    adapter = ClaudeAdapter(claude_home=tmp_path)
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)
    filler = _rec(
        type="assistant",
        message={"role": "assistant", "content": [{"type": "text", "text": "x" * 200}]},
    )
    halt = _err("You've hit your weekly limit · resets 2pm")
    (proj / "big.jsonl").write_text("\n".join([filler] * 2000 + [halt]) + "\n", encoding="utf-8")
    assert adapter.is_halted("/repo", "big") is True


def test_claude_version(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)
    # The most recent versioned record wins (Claude Code can update mid-session).
    (proj / "sid.jsonl").write_text(
        _rec(type="user", version="2.1.181")
        + "\n"
        + _rec(type="assistant", version="2.1.193")
        + "\n",
        encoding="utf-8",
    )
    assert adapter.claude_version("/repo", "sid") == "2.1.193"

    # No version field anywhere, and a missing transcript, both yield None.
    (proj / "noversion.jsonl").write_text(_rec(type="user") + "\n", encoding="utf-8")
    assert adapter.claude_version("/repo", "noversion") is None
    assert adapter.claude_version("/repo", "missing") is None


def test_uses_codex_workflow_scans_transcript_and_mtime_caches(tmp_path: Path) -> None:
    from command_center.adapters import claude as claude_mod

    claude_mod._CODEX_WORKFLOW_CACHE.clear()
    adapter = ClaudeAdapter(claude_home=tmp_path)
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)
    path = proj / "sid.jsonl"
    base_mtime = 1_782_302_578

    path.write_text(_rec(type="user", message={"role": "user", "content": "normal ask"}) + "\n")
    os.utime(path, (base_mtime, base_mtime))
    assert adapter.uses_codex_workflow("/repo", "sid") is False

    # Same mtime: cached false is reused, like the prompt cache for frozen transcripts.
    path.write_text(
        _rec(
            type="user",
            message={
                "role": "user",
                "content": f"<command-name>/{CODEX_WORKFLOW_NAME}</command-name>",
            },
        )
        + "\n",
        encoding="utf-8",
    )
    os.utime(path, (base_mtime, base_mtime))
    assert adapter.uses_codex_workflow("/repo", "sid") is False

    # Mtime moved: a growing transcript is re-read and the command marker is detected.
    os.utime(path, (base_mtime + 1, base_mtime + 1))
    assert adapter.uses_codex_workflow("/repo", "sid") is True
    assert adapter.uses_codex_workflow("/repo", "missing") is False


def test_uses_codex_workflow_ignores_skill_listing_and_doc_mentions(tmp_path: Path) -> None:
    """The bare workflow NAME is injected into EVERY session's ``skill_listing``
    attachment (the available-skills list) and appears in this repo's AGENTS.md prose
    as ``/codex-implement-task-and-claude-review``. Neither is an invocation, so a
    bare-substring scan would mis-badge every session. Only the ``<command-name>/…``
    tag counts."""
    from command_center.adapters import claude as claude_mod

    claude_mod._CODEX_WORKFLOW_CACHE.clear()
    adapter = ClaudeAdapter(claude_home=tmp_path)
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)
    path = proj / "sid.jsonl"
    path.write_text(
        _rec(
            type="user",
            attachment={
                "type": "skill_listing",
                "content": f"- {CODEX_WORKFLOW_NAME}: Delegate a task to OpenAI Codex.",
            },
        )
        + "\n"
        + _rec(
            type="user",
            message={"role": "user", "content": f"see `/{CODEX_WORKFLOW_NAME}` in the docs"},
        )
        + "\n",
        encoding="utf-8",
    )
    assert adapter.uses_codex_workflow("/repo", "sid") is False


def test_session_effort_parses_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center.adapters import claude as claude_mod

    adapter = ClaudeAdapter()
    commands = {
        4321: "claude --session-id abc --model claude-fable-5 --effort xhigh 'do it'",
        4322: "claude --session-id def --effort=high 'go'",
        4323: "claude --session-id ghi 'no effort flag'",
        4324: "claude --session-id jkl --effort bogus 'invalid level'",
    }
    # Stub the cached ps scan (ppid -> [(pid, command)]) — one child per fake parent.
    monkeypatch.setattr(
        claude_mod, "_children_map", lambda: {1: [(pid, cmd) for pid, cmd in commands.items()]}
    )
    assert adapter.session_effort(4321) == "xhigh"  # --effort <level>
    assert adapter.session_effort(4322) == "high"  # --effort=<level>
    assert adapter.session_effort(4323) is None  # no flag
    assert adapter.session_effort(4324) is None  # invalid level rejected
    assert adapter.session_effort(9999) is None  # pid not in the scan
    assert adapter.session_effort(0) is None  # non-positive pid


def test_observed_model_last_real_and_skips_synthetic(tmp_path: Path) -> None:
    from command_center.adapters import claude as claude_mod

    claude_mod._OBSERVED_MODEL_CACHE.clear()
    adapter = ClaudeAdapter(claude_home=tmp_path)
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)

    def _assistant(model: object) -> str:
        return _rec(
            type="assistant",
            message={
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": "x"}],
            },
        )

    # (a) the LAST real model wins over an earlier one.
    (proj / "a.jsonl").write_text(
        _assistant("claude-opus-4-8") + "\n" + _assistant("claude-fable-5") + "\n",
        encoding="utf-8",
    )
    assert adapter.observed_model("/repo", "a") == "claude-fable-5"

    # (b) trailing <synthetic> and missing-model entries are skipped → last REAL wins.
    (proj / "b.jsonl").write_text(
        _assistant("claude-fable-5")
        + "\n"
        + _assistant("<synthetic>")
        + "\n"
        + _rec(type="assistant", message={"role": "assistant", "content": []})
        + "\n",  # no model
        encoding="utf-8",
    )
    assert adapter.observed_model("/repo", "b") == "claude-fable-5"

    # (c) no transcript → None.
    assert adapter.observed_model("/repo", "missing") is None

    # (d) a transcript with no model entries at all → None.
    (proj / "d.jsonl").write_text(
        _rec(type="user", message={"role": "user", "content": "hi"}) + "\n", encoding="utf-8"
    )
    assert adapter.observed_model("/repo", "d") is None


def test_todos(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    assert adapter.todos("sid") == []
    task_dir = tmp_path / "tasks" / "sid"
    task_dir.mkdir(parents=True)
    (task_dir / "1.json").write_text(
        json.dumps({"subject": "first", "status": "completed"}), encoding="utf-8"
    )
    (task_dir / "2.json").write_text(
        json.dumps({"subject": "second", "status": "in_progress"}), encoding="utf-8"
    )
    assert adapter.todos("sid") == [("completed", "first"), ("in_progress", "second")]


def test_has_subagent_ignores_dot_claude_path_helpers(tmp_path: Path, monkeypatch) -> None:
    """An idle session whose only descendants are ``~/.claude/``-pathed helpers
    (the per-refresh statusline command, hook scripts) must NOT read as a subagent —
    the substring ``claude`` in their *path* used to flip an idle row to the green ▶.
    """
    from command_center.adapters import claude as claude_mod

    adapter = ClaudeAdapter(claude_home=tmp_path)
    # pid 100 (the session) -> 101 statusline helper -> (nothing claude-the-program)
    fake_tree = {
        100: [(101, "bash /home/user/.claude/statusline-command.sh")],
        101: [(102, "node /home/user/.claude/hooks/some-hook.js")],
    }
    monkeypatch.setattr(claude_mod, "_children_map", lambda: fake_tree)
    assert adapter.has_subagent(100) is False


def test_has_subagent_detects_real_claude_p_subagent(tmp_path: Path, monkeypatch) -> None:
    """A genuine ``claude -p …`` child (bare or absolute-path argv[0]) still counts."""
    from command_center.adapters import claude as claude_mod

    adapter = ClaudeAdapter(claude_home=tmp_path)
    bare = {100: [(101, "claude -p Summarize the transcript --model claude-haiku-4-5")]}
    monkeypatch.setattr(claude_mod, "_children_map", lambda: bare)
    assert adapter.has_subagent(100) is True

    absolute = {100: [(101, "/home/user/.bun/bin/claude -p do-thing")]}
    monkeypatch.setattr(claude_mod, "_children_map", lambda: absolute)
    assert adapter.has_subagent(100) is True

    # ccc's own CLI is not a subagent even though it lives under command-center.
    ccc = {100: [(101, "/home/user/.local/bin/ccc daemon")]}
    monkeypatch.setattr(claude_mod, "_children_map", lambda: ccc)
    assert adapter.has_subagent(100) is False


def test_has_background_task(tmp_path: Path, monkeypatch) -> None:
    """A live Bash-tool shell descendant (``shell-snapshots/snapshot-…``) is a
    background task; persistent helpers (MCP node, caffeinate) are not."""
    from command_center.adapters import claude as claude_mod

    adapter = ClaudeAdapter(claude_home=tmp_path)
    snap = "/home/user/.claude/shell-snapshots/snapshot-zsh-123.sh"
    # session 100 -> snapshot zsh -> the actual backgrounded command
    bg_tree = {
        100: [(101, f"/bin/zsh -c source {snap} 2>/dev/null || true && exec sleep 90")],
        101: [(102, "sleep 90")],
    }
    monkeypatch.setattr(claude_mod, "_children_map", lambda: bg_tree)
    assert adapter.has_background_task(100) is True

    # Only persistent helpers (MCP server, caffeinate) → NOT a background task.
    helpers = {
        100: [
            (201, "caffeinate -i -t 300"),
            (202, "npm exec @playwright/mcp@0.0.76 --cdp-endpoint http://localhost:9222"),
        ],
        202: [(203, "node /home/user/.npm/_npx/abc/playwright-mcp")],
    }
    monkeypatch.setattr(claude_mod, "_children_map", lambda: helpers)
    assert adapter.has_background_task(100) is False
    assert adapter.has_background_task(0) is False  # guard


def test_probe(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    assert adapter.probe() is False
    _write_session(tmp_path, os.getpid(), "s", "/c")
    assert adapter.probe() is True


# ---------------------------------------------------------------------------
# session_events (the normalized stream behind the full-session rendering)
# ---------------------------------------------------------------------------
def _events_transcript(home: Path, records: list[dict]) -> Path:
    proj = home / "projects" / "-Users-x-repo"
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / "sid.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def test_events_in_file_pairs_tools_and_filters(tmp_path: Path) -> None:
    from command_center.adapters.claude import events_in_file

    records: list[dict] = [
        {"type": "user", "message": {"role": "user", "content": "fix the failing test"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "secret reasoning"},
                    {"type": "text", "text": "Looking at the test."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "pytest -x"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": "1 failed"}],
                    },
                ],
            },
        },
        {
            "type": "assistant",
            "isSidechain": True,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "subagent chatter"}],
            },
        },
        {"type": "assistant", "message": {"role": "assistant", "content": "Fixed."}},
    ]
    events = events_in_file(_events_transcript(tmp_path, records))
    assert [e.kind for e in events] == ["prompt", "text", "tool", "text"]
    tool = events[2]
    assert tool.tool_name == "Bash"
    assert tool.tool_input == {"command": "pytest -x"}
    assert tool.tool_result == "1 failed"  # paired by tool_use_id
    joined = " ".join(e.text for e in events)
    assert "secret reasoning" not in joined  # thinking skipped
    assert "subagent chatter" not in joined  # sidechain skipped


def test_events_prompts_align_with_all_user_prompts(tmp_path: Path) -> None:
    """Prompt events use the SAME filter as all_user_prompts → identical (N) indexing."""
    from command_center.adapters.claude import events_in_file

    lone_notification = "<task-notification><task-id>a</task-id></task-notification>"
    records: list[dict] = [
        {"type": "user", "message": {"role": "user", "content": "one"}},
        {"type": "user", "message": {"role": "user", "content": lone_notification}},
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta"}},
        {"type": "user", "message": {"role": "user", "content": "two"}},
    ]
    path = _events_transcript(tmp_path, records)
    adapter = ClaudeAdapter(claude_home=tmp_path)
    prompts = [e.text for e in events_in_file(path) if e.kind == "prompt"]
    assert prompts == adapter.all_user_prompts_in_file(path) == ["one", "two"]


def test_session_events_missing_transcript(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    assert adapter.session_events("/Users/x/none", "missing") == []
