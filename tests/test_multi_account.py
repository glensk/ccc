"""Phase 2 — multi-account discovery, attribution, and launch-env pinning.

Covers the decisions that let ccc discover/reconcile/resume sessions across the
``private`` (``~/.claude``) and ``work`` (``~/.claude-work``) accounts without ever
billing the wrong seat: config_dir stamping + persistence, the D9 same-id conflict,
the D14 owning-account-first transcript resolution, the ``launch_env`` pin, the
cmd_resume fail-closed guards, the hooks account-switch warning, the D12 auto-resume
purge, D10's ``llm_custom_command`` routing (with the pinned ``claude -p`` fallback),
and the realpath-dedupe of a shared settings.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import types
from pathlib import Path

import pytest

from command_center import accounts, config, core, hooks, idlenotify, llm, resume
from command_center.adapters import ClaudeAdapter
from command_center.models import Status
from command_center.store import Store


# ---------------------------------------------------------------------------
# backfill migration (D3): a legacy row (no config_dir column) → claude_home()
# ---------------------------------------------------------------------------
def test_backfill_stamps_legacy_rows_with_default_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-migration DB backfills every existing row to the default account."""
    from command_center import store as store_mod

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    db = tmp_path / "legacy.db"
    # A pre-migration schema = today's base schema with the config_dir column removed.
    legacy_schema = store_mod._SCHEMA.replace(
        "    config_dir        TEXT    NOT NULL DEFAULT '',\n", ""
    )
    conn = sqlite3.connect(db)
    conn.executescript(legacy_schema)
    conn.execute("INSERT INTO sessions (session_id, cwd) VALUES ('old', '/repo/old')")
    conn.commit()
    conn.close()
    with Store(db) as store:  # opening runs _ensure_columns → ALTER + backfill
        row = store.get("old")
        assert row is not None
        assert row.config_dir == str(config.claude_home())  # backfilled to the default
        # A row created AFTER the migration stays '' (UNKNOWN, not silently default).
        store.ensure("fresh", cwd="/repo/new")
        assert (store.get("fresh") or row).config_dir == ""


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture(name="two_accounts")
def _two_accounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Pin two accounts (private=CLAUDE_HOME, work) and route ccc state under tmp."""
    private = tmp_path / "private"
    work = tmp_path / "work"
    private.mkdir()
    work.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(private))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CCC_HOME", raising=False)
    dirs = {"private": private, "work": work}
    monkeypatch.setattr(config, "claude_config_dirs", lambda: dict(dirs))
    return dirs


def _write_registry(home: Path, pid: int, session_id: str, cwd: str) -> None:
    """Write a live ``sessions/<pid>.json`` entry under *home*."""
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    (home / "sessions" / f"{pid}.json").write_text(
        json.dumps(
            {"pid": pid, "sessionId": session_id, "cwd": cwd, "status": "idle", "entrypoint": "cli"}
        ),
        encoding="utf-8",
    )


def _alive(monkeypatch: pytest.MonkeyPatch, *pids: int) -> None:
    """Declare exactly *pids* to be running processes, for registry-liveness tests.

    Patching ``_pid_alive`` (rather than writing this process's real pid into a registry)
    keeps the alive/dead distinction deterministic AND keeps a live pid out of the store,
    where the daemon's reaper would SIGTERM it — i.e. kill the test runner.
    """
    from command_center.adapters import claude as claude_adapter

    live = set(pids)
    monkeypatch.setattr(claude_adapter, "_pid_alive", lambda pid: pid in live)


def _write_transcript(home: Path, cwd: str, session_id: str) -> Path:
    """Write a minimal transcript for *session_id* under *home*'s projects dir."""
    encoded = cwd.replace("/", "-")
    path = home / "projects" / encoded / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "last-prompt"}) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# discovery + stamping + persistence
# ---------------------------------------------------------------------------
def test_discover_two_registries_stamps_config_dir(two_accounts: dict[str, Path]) -> None:
    """Both accounts' live registries are scanned; each entry carries its account dir."""
    _write_registry(two_accounts["private"], 101, "priv-sid", "/repo/a")
    _write_registry(two_accounts["work"], 202, "work-sid", "/repo/b")

    by_id = {ls.session_id: ls for ls in ClaudeAdapter().discover()}

    assert by_id["priv-sid"].config_dir == str(two_accounts["private"])
    assert by_id["work-sid"].config_dir == str(two_accounts["work"])
    assert not by_id["priv-sid"].conflict and not by_id["work-sid"].conflict


def test_reconcile_persists_config_dir(two_accounts: dict[str, Path], tmp_path: Path) -> None:
    """core.reconcile stamps the store row with the live account's config_dir."""
    _write_registry(two_accounts["work"], 303, "w-sid", "/repo/w")
    with Store(tmp_path / "s.db") as store:
        core.reconcile(store, ClaudeAdapter())
        row = store.get("w-sid")
        assert row is not None
        assert row.config_dir == str(two_accounts["work"])


def test_work_session_not_force_parked(two_accounts: dict[str, Path], tmp_path: Path) -> None:
    """A live work session (absent from the private registry) is not force-parked."""
    _write_registry(two_accounts["work"], os.getpid(), "w-live", "/repo/w")  # a LIVE pid
    with Store(tmp_path / "s.db") as store:
        core.reconcile(store, ClaudeAdapter())
        row = store.get("w-live")
        assert row is not None
        assert row.status != Status.PARKED.value  # in the (merged) live registry → not parked


# ---------------------------------------------------------------------------
# D9 conflict
# ---------------------------------------------------------------------------
_PRIV_PID, _WORK_PID = 111, 222


def test_same_id_two_registries_is_a_conflict(
    two_accounts: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same id RUNNING in both registries → conflict: no account picked (config_dir blank).

    Both processes are alive — a conflict is about two live processes, not two files
    (see the stale-entry tests below).
    """
    _alive(monkeypatch, _PRIV_PID, _WORK_PID)
    _write_registry(two_accounts["private"], _PRIV_PID, "dup", "/repo/x")
    _write_registry(two_accounts["work"], _WORK_PID, "dup", "/repo/x")

    by_id = {ls.session_id: ls for ls in ClaudeAdapter().discover()}
    assert by_id["dup"].conflict is True
    assert by_id["dup"].config_dir == ""


def test_stale_dead_entry_is_not_a_conflict(
    two_accounts: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashed session's leftover registry file must not fake a D9 conflict.

    ``cwork --resume <private-id>`` legitimately puts one id in both registries. If the
    private REPL was killed (its ``sessions/<pid>.json`` survives), only the work process
    is live — flagging a conflict would blank ``config_dir`` forever (the stale file never
    "exits") and permanently refuse ``ccc resume`` for that id.
    """
    _alive(monkeypatch, _WORK_PID)  # private crashed; only work is running
    _write_registry(two_accounts["private"], _PRIV_PID, "dup", "/repo/x")
    _write_registry(two_accounts["work"], _WORK_PID, "dup", "/repo/x")

    by_id = {ls.session_id: ls for ls in ClaudeAdapter().discover()}
    assert by_id["dup"].conflict is False
    assert by_id["dup"].config_dir == str(two_accounts["work"])  # attributed to the live one


def test_live_entry_wins_over_a_stale_one(
    two_accounts: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead registry entry never shadows the running process (else reconcile parks it)."""
    _alive(monkeypatch, _WORK_PID)
    _write_registry(two_accounts["private"], _PRIV_PID, "dup", "/repo/x")
    _write_registry(two_accounts["work"], _WORK_PID, "dup", "/repo/x")

    by_id = {ls.session_id: ls for ls in ClaudeAdapter().discover()}
    assert by_id["dup"].pid == _WORK_PID
    assert by_id["dup"].alive is True


def test_all_dead_entries_pick_one_without_conflict(
    two_accounts: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An id whose every registry entry is stale resolves to one dead row, no conflict."""
    _alive(monkeypatch)  # nothing is running
    _write_registry(two_accounts["private"], _PRIV_PID, "dup", "/repo/x")
    _write_registry(two_accounts["work"], _WORK_PID, "dup", "/repo/x")

    by_id = {ls.session_id: ls for ls in ClaudeAdapter().discover()}
    assert by_id["dup"].alive is False
    assert by_id["dup"].conflict is False


def test_conflict_does_not_mutate_stored_config_dir(
    two_accounts: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reconcile leaves a good stored config_dir untouched on a live conflict."""
    _alive(monkeypatch, _PRIV_PID, _WORK_PID)
    with Store(tmp_path / "s.db") as store:
        store.ensure("dup", cwd="/repo/x")
        store.update_fields("dup", config_dir=str(two_accounts["private"]))
        _write_registry(two_accounts["private"], _PRIV_PID, "dup", "/repo/x")
        _write_registry(two_accounts["work"], _WORK_PID, "dup", "/repo/x")
        core.reconcile(store, ClaudeAdapter())
        row = store.get("dup")
        assert row is not None
        assert row.config_dir == str(two_accounts["private"])  # never flipped to work


def test_cmd_resume_refuses_on_live_conflict(
    two_accounts: dict[str, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cmd_resume refuses while an id is live under two accounts."""
    from command_center import cli

    # Safety net: cmd_resume ends in os.execvp, which would REPLACE the test runner if a
    # guard ever regresses. Make that a loud failure instead of a silently truncated run.
    monkeypatch.setattr(
        os, "execvp", lambda *a, **k: pytest.fail("cmd_resume exec'd despite a live conflict")
    )
    _alive(monkeypatch, _PRIV_PID, _WORK_PID)
    _write_registry(two_accounts["private"], _PRIV_PID, "dup", "/repo/x")
    _write_registry(two_accounts["work"], _WORK_PID, "dup", "/repo/x")
    _write_transcript(two_accounts["private"], "/repo/x", "dup")
    with Store() as store:  # the DB cmd_resume reads (default path under CLAUDE_HOME)
        store.ensure("dup", cwd="/repo/x")
        store.update_fields("dup", config_dir=str(two_accounts["private"]))
    assert cli.cmd_resume(argparse.Namespace(session_id="dup")) == 1
    assert "two Claude accounts" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# D14 transcript resolution (owning account first, no symlink)
# ---------------------------------------------------------------------------
def test_transcript_path_prefers_owning_account(two_accounts: dict[str, Path]) -> None:
    """The session's own account wins even when both hold a transcript (no symlink)."""
    owner = _write_transcript(two_accounts["work"], "/repo/z", "sid")
    _write_transcript(two_accounts["private"], "/repo/z", "sid")
    adapter = ClaudeAdapter()
    assert adapter.transcript_path("/repo/z", "sid", str(two_accounts["work"])) == owner


def test_transcript_path_falls_back_to_other_account(two_accounts: dict[str, Path]) -> None:
    """A transcript held only by the OTHER account still resolves (D14 fallback)."""
    only = _write_transcript(two_accounts["work"], "/repo/z", "sid")
    adapter = ClaudeAdapter()
    # config_dir claims private, but the transcript lives under work → fallback finds it.
    assert adapter.transcript_path("/repo/z", "sid", str(two_accounts["private"])) == only


# ---------------------------------------------------------------------------
# launch_env
# ---------------------------------------------------------------------------
def test_launch_env_default_account_unsets_var(two_accounts: dict[str, Path]) -> None:
    base = {"CLAUDE_CONFIG_DIR": str(two_accounts["work"]), "PATH": "/bin"}
    env = accounts.launch_env(str(two_accounts["private"]), base=base)
    assert "CLAUDE_CONFIG_DIR" not in env  # default account → unset


def test_launch_env_work_account_sets_var(two_accounts: dict[str, Path]) -> None:
    env = accounts.launch_env(str(two_accounts["work"]), base={"PATH": "/bin"})
    assert env["CLAUDE_CONFIG_DIR"] == str(two_accounts["work"])


def test_launch_env_always_strips_securestorage(two_accounts: dict[str, Path]) -> None:
    base = {"CLAUDE_SECURESTORAGE_CONFIG_DIR": "/somewhere"}
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in accounts.launch_env("", base=base)
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in accounts.launch_env(
        str(two_accounts["work"]), base=base
    )


def test_launch_env_ambient_work_never_leaks_into_private(two_accounts: dict[str, Path]) -> None:
    """An ambient work CLAUDE_CONFIG_DIR is dropped for a default-account launch."""
    base = {"CLAUDE_CONFIG_DIR": str(two_accounts["work"])}
    env = accounts.launch_env("", base=base)  # "" == default account
    assert "CLAUDE_CONFIG_DIR" not in env


def test_launch_env_prefix_default_vs_work(two_accounts: dict[str, Path]) -> None:
    assert (
        accounts.launch_env_prefix("")
        == "unset CLAUDE_SECURESTORAGE_CONFIG_DIR CLAUDE_CONFIG_DIR; "
    )
    work_prefix = accounts.launch_env_prefix(str(two_accounts["work"]))
    assert "export CLAUDE_CONFIG_DIR=" in work_prefix
    assert str(two_accounts["work"]) in work_prefix


# ---------------------------------------------------------------------------
# cmd_resume fail-closed guards
# ---------------------------------------------------------------------------
def test_cmd_resume_refuses_unknown_account_multi(
    two_accounts: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An '' config_dir in multi-account mode refuses (never defaults to private)."""
    from command_center import cli

    with Store() as store:
        store.ensure("unknown", cwd="/repo/x")  # config_dir defaults to ''
    assert cli.cmd_resume(argparse.Namespace(session_id="unknown")) == 1
    assert "no recorded Claude account" in capsys.readouterr().err


def test_cmd_resume_refuses_missing_transcript(
    two_accounts: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A known account but no transcript under it refuses (nothing to resume)."""
    from command_center import cli

    with Store() as store:
        store.ensure("no-tx", cwd="/repo/x")
        store.update_fields("no-tx", config_dir=str(two_accounts["private"]))
    assert cli.cmd_resume(argparse.Namespace(session_id="no-tx")) == 1
    assert "no recorded conversation" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# hooks — stamping + D11 account-switch warning
# ---------------------------------------------------------------------------
def test_hooks_stamps_config_dir_from_env(
    two_accounts: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(two_accounts["work"]))
    with Store(tmp_path / "s.db") as store:
        session, warning = hooks.ensure_current_session(store, "sid", "/repo/w")
        assert session.config_dir == str(two_accounts["work"])
        assert warning is None  # first run: no prior account to switch from


def test_hooks_account_switch_warning_fires_on_mismatch(
    two_accounts: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Store(tmp_path / "s.db") as store:
        store.ensure("sid", cwd="/repo/w")
        store.update_fields("sid", config_dir=str(two_accounts["private"]))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(two_accounts["work"]))
        _session, warning = hooks.ensure_current_session(store, "sid", "/repo/w")
        assert warning is not None and "work" in warning
        # And it does NOT fire when the account is unchanged.
        _s2, w2 = hooks.ensure_current_session(store, "sid", "/repo/w")
        assert w2 is None


# ---------------------------------------------------------------------------
# D12 auto-resume purge
# ---------------------------------------------------------------------------
def test_purge_non_default_entry_before_plan(two_accounts: dict[str, Path], tmp_path: Path) -> None:
    """A queued entry that bills a non-default account is purged from the queue (D12)."""
    with Store(tmp_path / "s.db") as store:
        store.ensure("w-job", cwd="/repo/w")
        store.update_fields("w-job", config_dir=str(two_accounts["work"]))
        store.ensure("p-job", cwd="/repo/p")
        store.update_fields("p-job", config_dir=str(two_accounts["private"]))
        state = resume.QueueState(
            entries={
                "w-job": resume.Entry(session_id="w-job", repo="/repo/w", cwd="/repo/w"),
                "p-job": resume.Entry(session_id="p-job", repo="/repo/p", cwd="/repo/p"),
            }
        )
        resume.purge_non_default_entries(store, state)
        assert "w-job" not in state.entries  # non-default → dropped
        assert "p-job" in state.entries  # default → kept


# ---------------------------------------------------------------------------
# D10 — llm.run_model routes through llm_custom_command, else pinned claude -p
# ---------------------------------------------------------------------------
def test_run_model_routes_through_llm_custom_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured llm_custom_command serves the call (claude -p never runs)."""
    monkeypatch.setattr(llm, "_run_claude", lambda *_a, **_k: pytest.fail("claude -p used"))
    monkeypatch.setattr(
        config, "load_config", lambda: config.Config(llm_custom_command="my-router")
    )
    captured: dict[str, object] = {}

    class _Result:
        returncode = 0
        stdout = "  judged  "

    def _run(cmd: object, **kwargs: object) -> _Result:
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        captured["env"] = kwargs.get("env")
        return _Result()

    monkeypatch.setattr(llm.subprocess, "run", _run)
    out = llm.run_model("a long {braced} prompt", "some-model")
    assert out == "judged"
    assert captured["cmd"] == "my-router"
    assert captured["input"] == "a long {braced} prompt"
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["CCC_INTERNAL"] == "1" and env["AI_NO_AUTOCOMMIT"] == "1"


def test_run_model_falls_back_to_pinned_claude(
    two_accounts: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No llm_custom_command → headless claude -p with llm_account pinned (never ambient)."""
    monkeypatch.setattr(llm.shutil, "which", lambda _name: "/usr/bin/claude")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(two_accounts["work"]))  # ambient work
    captured: dict[str, object] = {}

    class _Result:
        returncode = 0
        stdout = "ok"

    def _run(argv: list[str], **kwargs: object) -> _Result:
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return _Result()

    monkeypatch.setattr(llm.subprocess, "run", _run)
    assert llm.run_model("p", "m") == "ok"
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[:2] == ["claude", "-p"]
    env = captured["env"]
    assert isinstance(env, dict)
    # llm_account defaults to "private" (the default account) → CLAUDE_CONFIG_DIR unset,
    # even though the ambient env had it set to work.
    assert "CLAUDE_CONFIG_DIR" not in env
    assert env["CCC_INTERNAL"] == "1"


# ---------------------------------------------------------------------------
# per-action purpose/note labels exported as CCC_LLM_PURPOSE / CCC_LLM_NOTE
# ---------------------------------------------------------------------------
def _capture_custom_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Route through llm_custom_command and capture the env the subprocess would get."""
    monkeypatch.setattr(llm, "_run_claude", lambda *_a, **_k: pytest.fail("claude -p used"))
    monkeypatch.setattr(
        config, "load_config", lambda: config.Config(llm_custom_command="my-router")
    )
    captured: dict[str, object] = {}

    def _run(cmd: object, **kwargs: object) -> types.SimpleNamespace:
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return types.SimpleNamespace(returncode=0, stdout="ok")

    monkeypatch.setattr(llm.subprocess, "run", _run)
    return captured


def test_run_model_exports_purpose_and_note_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """purpose+note ride into the custom command's env as CCC_LLM_PURPOSE / CCC_LLM_NOTE."""
    captured = _capture_custom_env(monkeypatch)
    assert llm.run_model("prompt", "m", purpose="aim-score", note="ship the parser") == "ok"
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["CCC_LLM_PURPOSE"] == "aim-score"
    assert env["CCC_LLM_NOTE"] == "ship the parser"


def test_run_model_purpose_only_omits_note_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A purpose with an empty note exports CCC_LLM_PURPOSE but never an empty CCC_LLM_NOTE."""
    captured = _capture_custom_env(monkeypatch)
    assert llm.run_model("prompt", "m", purpose="aim-met") == "ok"
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["CCC_LLM_PURPOSE"] == "aim-met"
    assert "CCC_LLM_NOTE" not in env


def test_run_model_no_labels_exports_no_label_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No purpose/note → neither label var appears in the env (backwards-compat)."""
    captured = _capture_custom_env(monkeypatch)
    assert llm.run_model("prompt", "m") == "ok"
    env = captured["env"]
    assert isinstance(env, dict)
    assert "CCC_LLM_PURPOSE" not in env
    assert "CCC_LLM_NOTE" not in env


def test_summarize_passes_summary_nextstep_purpose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``summarize`` labels its call ``summary-nextstep`` and forwards the note (log only)."""
    seen: dict[str, object] = {}

    def _dispatch(_prompt: str, _model: str, purpose: str = "", note: str = "") -> str:
        seen["purpose"] = purpose
        seen["note"] = note
        return '{"summary":"s","next_step":"- n"}'

    monkeypatch.setattr(llm, "_dispatch", _dispatch)
    tpath = tmp_path / "t.jsonl"
    tpath.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n",
        encoding="utf-8",
    )
    summary, next_step = llm.summarize("the aim", tpath, "m", note="ship the parser")
    assert summary == "s" and next_step == "- n"
    assert seen["purpose"] == "summary-nextstep"
    assert seen["note"] == "ship the parser"


def test_concise_note_collapses_and_truncates() -> None:
    """concise_note collapses all whitespace to one line and caps length with a trailing …."""
    assert llm.concise_note(None) == ""
    assert llm.concise_note("") == ""
    assert llm.concise_note("  ship   the\n\tparser  ") == "ship the parser"  # whitespace collapsed
    long = "word " * 60  # 300 chars pre-collapse
    out = llm.concise_note(long)
    assert len(out) == 160 and out.endswith("…")  # cut to limit-1 + ellipsis
    assert llm.concise_note("x" * 160, limit=160) == "x" * 160  # exactly at limit → untouched
    assert llm.concise_note("x" * 161, limit=160).endswith("…")  # over limit → truncated


# ---------------------------------------------------------------------------
# realpath dedupe — settings.json processed once
# ---------------------------------------------------------------------------
def test_settings_effort_cached_dedupes_by_realpath(
    two_accounts: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two accounts sharing one settings.json read it exactly once (realpath key)."""
    real = two_accounts["private"] / "settings.json"
    real.write_text(json.dumps({"effortLevel": "high"}), encoding="utf-8")
    link = two_accounts["work"] / "settings.json"
    link.symlink_to(real)
    calls: list[str] = []
    real_level = core._settings_effort_level

    def _counting(config_dir: str = "") -> str:
        calls.append(config_dir)
        return str(real_level(config_dir))

    monkeypatch.setattr(core, "_settings_effort_level", _counting)
    cache: dict[str, str] = {}
    a = core._settings_effort_cached(str(two_accounts["private"]), cache)
    b = core._settings_effort_cached(str(two_accounts["work"]), cache)
    assert a == "high" and b == "high"
    assert len(calls) == 1  # the shared realpath is read once


def test_idlenotify_writes_shared_settings_once(
    two_accounts: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """set_enabled processes a symlink-shared settings.json exactly once."""
    real = two_accounts["private"] / "settings.json"
    real.write_text(json.dumps({"agentPushNotifEnabled": True}) + "\n", encoding="utf-8")
    (two_accounts["work"] / "settings.json").symlink_to(real)
    writes: list[Path] = []
    real_write = idlenotify._write_through

    def _spy(path: Path, text: str) -> None:
        writes.append(path)
        real_write(path, text)

    monkeypatch.setattr(idlenotify, "_write_through", _spy)
    idlenotify.set_enabled(False)
    assert len(writes) == 1  # the shared realpath is written once
    assert json.loads(real.read_text(encoding="utf-8"))["agentPushNotifEnabled"] is False


def test_cmd_resume_job_refuses_unknown_account(
    two_accounts: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`ccc resume-job` (the Obsidian ▶ button) fails closed on an unattributed row.

    Symmetry with cmd_resume: a row with no recorded account must never open a new tab
    that silently bills the default seat.
    """
    from command_center import cli

    _write_transcript(two_accounts["private"], "/repo/x", "orphan")
    with Store() as store:
        store.ensure("orphan", cwd="/repo/x")  # created, never observed → config_dir ''
    assert cli.cmd_resume_job(argparse.Namespace(session_id="orphan")) == 1
    assert "no recorded Claude account" in capsys.readouterr().err


def test_jump_resume_selected_refuses_unknown_account(
    two_accounts: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ccc jump` from the ccc tab fails closed on an unattributed row.

    Symmetry with cmd_resume / cmd_resume_job: the Karabiner chord has no terminal
    attached, so the refusal goes to stderr with a non-zero exit — never a silent
    new tab billed to the default seat.
    """
    from command_center import jump

    monkeypatch.setattr(jump.jumpstate, "get_selected", lambda: "orphan")
    monkeypatch.setattr(
        jump.terminal,
        "resume_in_new_tab",
        lambda *_a, **_k: pytest.fail("resume_in_new_tab despite an unknown account"),
    )
    with Store() as store:
        store.ensure("orphan", cwd="/repo/x")  # created, never observed → config_dir ''
    assert jump._resume_selected() == 1
    assert "no recorded Claude account" in capsys.readouterr().err


def test_jump_resume_selected_refuses_on_live_conflict(
    two_accounts: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ccc jump` refuses while an id is live under two accounts (D9), like cmd_resume."""
    from command_center import jump

    _alive(monkeypatch, _PRIV_PID, _WORK_PID)
    _write_registry(two_accounts["private"], _PRIV_PID, "dup", "/repo/x")
    _write_registry(two_accounts["work"], _WORK_PID, "dup", "/repo/x")
    monkeypatch.setattr(jump.jumpstate, "get_selected", lambda: "dup")
    monkeypatch.setattr(
        jump.terminal,
        "resume_in_new_tab",
        lambda *_a, **_k: pytest.fail("resume_in_new_tab despite a live conflict"),
    )
    with Store() as store:
        store.ensure("dup", cwd="/repo/x")  # known account, but live in BOTH registries
        store.update_fields("dup", config_dir=str(two_accounts["private"]))
    assert jump._resume_selected() == 1
    assert "two Claude accounts" in capsys.readouterr().err
