"""Unit tests for the SQLite store."""

from __future__ import annotations

from pathlib import Path

from command_center.models import LiveSession
from command_center.store import Store


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state.db")


def test_ensure_and_update(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.ensure("s1", cwd="/repo")
    assert session.session_id == "s1"
    assert session.cwd == "/repo"
    assert session.created_at > 0

    store.update_fields("s1", aim="done when green", deadline="2026-07-01")
    got = store.get("s1")
    assert got is not None
    assert got.aim == "done when green"
    assert got.deadline == "2026-07-01"
    assert got.updated_at >= got.created_at


def test_create_draft_stores_aim_prompt_and_flag(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create_draft(
        "job1",
        "/repo/sdsc/zoho",
        "Migrate Zendesk tickets to Zoho",
        prompt="do the migration",
        start_when="during holidays",
    )
    assert session.draft is True
    assert session.prompt == "do the migration"
    assert session.aim == "Migrate Zendesk tickets to Zoho"
    assert session.start_when == "during holidays"
    assert session.aim_score >= 0  # set_aim seeds an instant lexical score
    assert store.list_aim_history("job1"), "create_draft routes the AIM through set_aim"


def test_create_draft_blank_prompt_stays_null(tmp_path: Path) -> None:
    # A blank prompt is NOT copied from the AIM: NULL means "defaults to the AIM at
    # launch" (cmd_start_job falls back), and the mirrored file's empty # Prompt round-trips.
    store = _store(tmp_path)
    assert store.create_draft("job2", "/repo", "Ship the feature").prompt is None  # no prompt
    assert store.create_draft("job2b", "/repo", "Ship it", prompt="   ").prompt is None  # blank


def test_create_draft_stores_llm_models_and_defaults(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Explicit valid choices are stored verbatim.
    s = store.create_draft("jobllm", "/repo", "Do it", llm_overseer="fable-5", llm_exec="sonnet-5")
    assert s.llm_overseer == "fable-5" and s.llm_exec == "sonnet-5"
    # Default when omitted is fable-5 on both.
    d = store.create_draft("jobdef", "/repo", "Do it")
    assert d.llm_overseer == "fable-5" and d.llm_exec == "fable-5"
    # A bogus value falls back to the fable-5 default (validated against LLM_CHOICES).
    b = store.create_draft("jobbad", "/repo", "Do it", llm_overseer="gpt-9", llm_exec="")
    assert b.llm_overseer == "fable-5" and b.llm_exec == "fable-5"


def test_clear_draft_promotes_to_real_session(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_draft("job3", "/repo", "Do the thing")
    store.clear_draft("job3")
    got = store.get("job3")
    assert got is not None
    assert got.draft is False
    assert got.status == "idle"


def test_bool_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.update_fields("s1", done=True, keep=True)
    got = store.get("s1")
    assert got is not None
    assert got.done is True
    assert got.keep is True


def test_archived_excluded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("a")
    store.ensure("b")
    store.update_fields("b", archived=True)
    ids = {s.session_id for s in store.list_sessions()}
    assert ids == {"a"}
    assert len(store.list_sessions(include_archived=True)) == 2


def test_subgoals_and_progress(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_subgoals("s1", ["find valve", "throttle", "test"])
    subs = store.list_subgoals("s1")
    assert [s.text for s in subs] == ["find valve", "throttle", "test"]
    assert store.progress("s1") == (0, 3)

    store.set_subgoal_checked(subs[0].id, True)
    assert store.progress("s1") == (1, 3)

    # Replacing the checklist clears the old rows.
    store.set_subgoals("s1", ["only one"])
    assert store.progress("s1") == (0, 1)


def test_check_all_subgoals_reconciles_to_full(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_subgoals("s1", ["a", "b", "c"], source="auto")
    subs = store.list_subgoals("s1")
    store.set_subgoal_checked(subs[1].id, True)  # one already ticked → 1/3
    assert store.progress("s1") == (1, 3)

    flipped = store.check_all_subgoals("s1")  # mark-done reconciles the rest
    assert flipped == 2  # only the two still-unchecked rows flip
    assert store.progress("s1") == (3, 3)
    assert store.check_all_subgoals("s1") == 0  # idempotent — nothing left to flip


def test_manual_progress_roundtrip_and_cleared_on_done(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    session = store.get("s1")
    assert session is not None
    assert session.manual_progress is None  # default: auto (sub-goal ratio)

    store.update_fields("s1", manual_progress=40)
    session = store.get("s1")
    assert session is not None
    assert session.manual_progress == 40

    # Blank edit clears the override back to auto.
    store.update_fields("s1", manual_progress=None)
    session = store.get("s1")
    assert session is not None
    assert session.manual_progress is None

    # Mark-done (check_all_subgoals) clears a set override so done never reads 40%.
    store.update_fields("s1", manual_progress=40)
    store.check_all_subgoals("s1")
    session = store.get("s1")
    assert session is not None
    assert session.manual_progress is None


def test_set_subgoals_reports_change_and_records_history(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    assert store.set_subgoals("s1", ["a", "b"]) is True  # first version
    assert store.set_subgoals("s1", ["a", "b"]) is False  # identical -> no-op, no new history
    assert store.set_subgoals("s1", ["a", "b", "c"]) is True  # real change
    hist = store.list_subgoal_history("s1")
    assert len(hist) == 2  # only the two real changes recorded
    assert [t for t, _ in hist[-1].items] == ["a", "b", "c"]
    assert hist[0].drift_severity == "none"  # first-ever has nothing to drift from


def test_set_subgoals_merge_preserves_ticks_and_provenance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_subgoals("s1", ["alpha", "beta", "gamma"])
    subs = {s.text: s for s in store.list_subgoals("s1")}
    store.set_subgoal_checked(subs["beta"].id, True)
    # Regenerate with merge: beta survives (stays checked), delta is new (unchecked).
    store.set_subgoals(
        "s1", ["alpha", "beta", "delta"], source="agent", model="claude-haiku-4-5", merge=True
    )
    got = {s.text: s for s in store.list_subgoals("s1")}
    assert got["beta"].checked is True  # carried over
    assert got["delta"].checked is False  # new item
    assert got["alpha"].checked is False
    assert got["delta"].source == "agent"
    assert got["delta"].model == "claude-haiku-4-5"
    session = store.get("s1")
    assert session is not None and session.subgoals_adaptive is True  # agent lists adapt


def test_subgoals_stale_after_aim_change(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_aim("s1", "ship X: pytest -q green and PR #5 merged")  # AIM rev 1
    store.set_subgoals("s1", ["write test_x", "open PR #5"], source="agent")  # adaptive @ rev 1
    assert store.subgoals_stale("s1") is False
    store.set_aim("s1", "ship X and deploy: prod smoke test passes")  # AIM rev 2 -> stale
    assert store.subgoals_stale("s1") is True
    store.set_subgoals("s1", ["write test_x", "deploy"], source="agent", merge=True)  # re-aligned
    assert store.subgoals_stale("s1") is False
    # A pinned (non-adaptive) checklist never goes stale.
    store.ensure("s2")
    store.set_aim("s2", "do thing one and thing two concretely")
    store.set_subgoals("s2", ["a"], source="user")  # pinned
    store.set_aim("s2", "do thing one, two and three concretely")
    assert store.subgoals_stale("s2") is False


def test_drift_setters_and_resolution(tmp_path: Path) -> None:
    from command_center.models import Session, drift_unresolved

    store = _store(tmp_path)
    store.ensure("s1")

    def unresolved() -> bool:
        session = store.get("s1")
        assert isinstance(session, Session)
        return drift_unresolved(session)

    store.set_drift("s1", "medium", "coverage dropped")
    assert unresolved() is True

    store.ack_drift("s1")
    assert unresolved() is False  # acknowledged -> resolved

    store.set_drift("s1", "high", "goalpost moved")  # re-flag
    assert unresolved() is True
    store.set_drift("s1", "none", None)  # a later clean check clears it
    assert unresolved() is False


def test_prunable_and_delete_many(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # junk: a contentless leftover (e.g. a headless `claude -p` row at "/").
    store.ensure("junk", cwd="/")
    # protected by signal: each of these trips one guard and must survive.
    store.ensure("has_aim")
    store.update_fields("has_aim", aim="done when green")
    store.ensure("has_prompt")
    store.update_fields("has_prompt", prompt_count=2)
    store.ensure("kept")
    store.update_fields("kept", keep=True)
    store.ensure("has_subgoal")
    store.set_subgoals("has_subgoal", ["step one"])
    # live (currently running) — protected even though it is otherwise contentless.
    store.ensure("live_empty", cwd="/")

    victims = store.prunable_sessions(protect_ids={"live_empty"})
    assert {s.session_id for s in victims} == {"junk"}

    assert store.delete_many(s.session_id for s in victims) == 1
    assert store.get("junk") is None
    assert {s.session_id for s in store.list_sessions()} == {
        "has_aim",
        "has_prompt",
        "kept",
        "has_subgoal",
        "live_empty",
    }
    assert store.delete_many([]) == 0  # no-op on empty input


def test_prunable_headless_overrides_content(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # A headless `claude -p` leak: carries an env-inherited aim + auto next-step +
    # prompt_count, so the contentless guards would spare it — but headless_ids
    # prunes it anyway.
    store.ensure("headless", cwd="/repo")
    store.update_fields("headless", aim="inherited aim", next_step="auto step", prompt_count=1)
    # A real session that merely shares the same transcript shape must still be
    # protected when it is live, done, or kept.
    store.ensure("done_headless")
    store.update_fields("done_headless", aim="x", done=True)
    store.ensure("kept_headless")
    store.update_fields("kept_headless", aim="x", keep=True)
    store.ensure("live_headless")

    victims = store.prunable_sessions(
        protect_ids={"live_headless"},
        headless_ids={"headless", "done_headless", "kept_headless", "live_headless"},
    )
    assert {s.session_id for s in victims} == {"headless"}


def test_aim_score_columns_roundtrip(tmp_path: Path) -> None:
    # Guards the _SESSION_COLUMNS whitelist: an un-whitelisted column is silently dropped.
    store = _store(tmp_path)
    store.ensure("s1")
    store.update_fields(
        "s1", aim_score=70, aim_score_reason="names a passing test", last_progress_at=123
    )
    got = store.get("s1")
    assert got is not None
    assert got.aim_score == 70
    assert got.aim_score_reason == "names a passing test"
    assert got.last_progress_at == 123


def test_version_column_roundtrip(tmp_path: Path) -> None:
    # Guards the _SESSION_COLUMNS whitelist + the ALTER-in-place migration for `version`.
    store = _store(tmp_path)
    store.ensure("s1")
    assert store.get("s1").version is None  # type: ignore[union-attr]  # NULL by default
    store.update_fields("s1", version="2.1.193")
    got = store.get("s1")
    assert got is not None
    assert got.version == "2.1.193"


def test_model_effort_columns_roundtrip(tmp_path: Path) -> None:
    # Guards the _SESSION_COLUMNS whitelist + the ALTER-in-place migration for the OBSERVED
    # model/effort columns. Both default to "" (NOT NULL) and survive a round-trip.
    store = _store(tmp_path)
    store.ensure("s1")
    got = store.get("s1")
    assert got is not None
    assert got.model == "" and got.effort == ""  # NOT NULL defaults
    store.update_fields("s1", model="opus-4.8", effort="xhigh")
    got = store.get("s1")
    assert got is not None
    assert got.model == "opus-4.8"
    assert got.effort == "xhigh"


def test_set_aim_clears_auto_subgoals_and_resets(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_subgoals("s1", ["a", "b"], source="auto")
    store.update_fields("s1", aim="old aim", context_offset=500, aim_score=80)

    changed = store.set_aim("s1", "all tests in tests/ pass and PR #42 merged")
    assert changed is True
    got = store.get("s1")
    assert got is not None
    assert got.aim == "all tests in tests/ pass and PR #42 merged"
    assert store.progress("s1") == (0, 0)  # auto checklist cleared
    assert got.context_offset == 0  # offset reset so a fresh checklist re-derives
    assert got.aim_score >= 50  # concrete aim => specific (lexical), reason cleared
    assert got.aim_score_reason is None


def test_set_aim_met_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_aim_met("s1", True, "tests pass and PR merged", 12345)
    got = store.get("s1")
    assert got is not None
    assert got.aim_met is True
    assert got.aim_met_reason == "tests pass and PR merged"
    assert got.aim_assessed_at == 12345
    # Latest-wins: a later turn can flip it back to False.
    store.set_aim_met("s1", False, "regressed", 22222)
    got = store.get("s1")
    assert got is not None and got.aim_met is False and got.aim_assessed_at == 22222


def test_set_aim_clears_met_verdict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_aim("s1", "old concrete aim: pytest -q green")
    store.set_aim_met("s1", True, "was done", 999)
    # A new AIM invalidates the prior "is it done?" verdict.
    store.set_aim("s1", "a different concrete aim: ruff check clean")
    got = store.get("s1")
    assert got is not None
    assert got.aim_met is False
    assert got.aim_assessed_at == 0
    assert got.aim_met_reason is None


def test_set_aim_noop_when_unchanged(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_aim("s1", "ship it")
    store.set_subgoals("s1", ["x"], source="auto")
    changed = store.set_aim("s1", "ship it")  # same aim
    assert changed is False
    assert store.progress("s1") == (0, 1)  # checklist untouched


def test_set_aim_preserves_user_subgoals(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.update_fields("s1", aim="old")
    store.set_subgoals("s1", ["manual one", "manual two"], source="user")
    store.set_aim("s1", "a different aim")
    assert [s.text for s in store.list_subgoals("s1")] == ["manual one", "manual two"]


def test_set_aim_records_prev_on_change_only(tmp_path: Path) -> None:
    """The first AIM records no transition; a later change records old + a change time."""
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_aim("s1", "first aim")  # initial set: no prior AIM -> no transition
    first = store.get("s1")
    assert first is not None and first.aim_prev is None and first.aim_changed_at == 0
    store.set_aim("s1", "second aim")  # a real change -> remember where it came from
    second = store.get("s1")
    assert second is not None
    assert second.aim_prev == "first aim"
    assert second.aim_changed_at > 0


def test_aim_history_records_progression(tmp_path: Path) -> None:
    """Every AIM (re)definition is appended in order; the last is the current AIM."""
    store = _store(tmp_path)
    store.ensure("s1")
    assert store.list_aim_history("s1") == []  # nothing yet
    store.set_aim("s1", "first vague aim")
    store.set_aim("s1", "second aim")
    store.set_aim("s1", "third, concrete aim: pytest -q green")
    history = store.list_aim_history("s1")
    assert [h.aim for h in history] == [
        "first vague aim",
        "second aim",
        "third, concrete aim: pytest -q green",
    ]
    assert all(h.score >= 0 for h in history)  # each revision carries its lexical score
    store.set_aim("s1", "third, concrete aim: pytest -q green")  # no-op -> no new row
    assert len(store.list_aim_history("s1")) == 3


def test_count_aim_history_tracks_running_index(tmp_path: Path) -> None:
    """``count_aim_history`` is the current AIM's 1-based running index (0 before any row)."""
    store = _store(tmp_path)
    store.ensure("s1")
    assert store.count_aim_history("s1") == 0  # no rows yet
    store.set_aim("s1", "first aim")
    assert store.count_aim_history("s1") == 1
    store.set_aim("s1", "second aim")
    assert store.count_aim_history("s1") == 2
    store.set_aim("s1", "second aim")  # no-op -> index unchanged
    assert store.count_aim_history("s1") == 2


def test_aim_history_seeds_preexisting_original(tmp_path: Path) -> None:
    """A session whose AIM predates history-tracking still shows where it started."""
    store = _store(tmp_path)
    store.ensure("s1")
    store.update_fields("s1", aim="legacy aim", aim_score=40)  # set without going through set_aim
    assert store.list_aim_history("s1") == []  # no history rows for the legacy AIM
    store.set_aim("s1", "sharpened aim")  # first tracked change seeds the original first
    assert [h.aim for h in store.list_aim_history("s1")] == ["legacy aim", "sharpened aim"]


def test_set_short_aim_writes_session_and_latest_revision(tmp_path: Path) -> None:
    """The short label lands on the session AND mirrors onto the current AIM-history row."""
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_aim("s1", "first aim")
    store.set_aim("s1", "second aim")
    store.set_short_aim("s1", "implement second")
    got = store.get("s1")
    assert got is not None and got.short_aim == "implement second"
    history = store.list_aim_history("s1")
    assert history[-1].short_aim == "implement second"  # mirrored onto the current revision
    assert history[0].short_aim is None  # an earlier revision is untouched
    store.set_short_aim("s1", "  ")  # blank clears back to NULL
    assert (cleared := store.get("s1")) is not None and cleared.short_aim is None


def test_set_aim_clears_stale_short_aim(tmp_path: Path) -> None:
    """Changing the AIM drops the old short label so the column never shows a stale one."""
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_aim("s1", "first aim")
    store.set_short_aim("s1", "implement first")
    store.set_aim("s1", "a wholly different aim")
    got = store.get("s1")
    assert got is not None and got.short_aim is None


def test_set_subgoals_weight_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_subgoals("s1", ["a", "b", "c"], source="auto", weights=[2, 1, 3])
    assert [s.weight for s in store.list_subgoals("s1")] == [2, 1, 3]
    # Default weight is 1 when none supplied.
    store.set_subgoals("s1", ["only"])
    assert store.list_subgoals("s1")[0].weight == 1


def test_set_subgoal_check_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_subgoals("s1", ["build passes"], source="auto")
    sub = store.list_subgoals("s1")[0]
    assert sub.check_cmd is None
    store.set_subgoal_check(sub.id, "make build")
    assert store.list_subgoals("s1")[0].check_cmd == "make build"
    store.set_subgoal_check(sub.id, "")  # empty clears it
    assert store.list_subgoals("s1")[0].check_cmd is None


def test_progress_weighted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_subgoals("s1", ["a", "b", "c"], source="auto", weights=[3, 1, 1])
    subs = store.list_subgoals("s1")
    store.set_subgoal_checked(subs[0].id, True)  # the weight-3 item
    assert store.progress("s1") == (1, 3)  # unweighted count
    assert store.progress_weighted("s1") == (3, 5)  # 3 of 5 weight done

    # All-default weights => weighted == unweighted.
    store.set_subgoals("s1", ["x", "y"])
    assert store.progress_weighted("s1") == store.progress("s1")


def test_upsert_preserves_user_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1", cwd="/old")
    store.update_fields("s1", aim="keep me", next_step="my step", next_step_source="user")

    live = LiveSession(
        pid=42, session_id="s1", cwd="/new", name="renamed", agent="claude", alive=True
    )
    store.upsert_from_live(live)

    got = store.get("s1")
    assert got is not None
    assert got.cwd == "/new"  # reconcile updates infra fields
    assert got.name == "renamed"
    assert got.last_seen_pid == 42
    assert got.aim == "keep me"  # but never user-authored fields
    assert got.next_step == "my step"


def test_create_draft_stores_start_date(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create_draft(
        "job-sd",
        "/repo/home/mac",
        "Re-enable FileVault after the trip",
        start_when="return from Slovenia",
        start_date="2026-08-11",
    )
    assert session.start_date == "2026-08-11"
    assert session.start_when == "return from Slovenia"
    # Blank stays NULL (no fixed date → plain FUTURE bucket).
    blank = store.create_draft("job-nd", "/repo/home/mac", "Other", start_date="  ")
    assert blank.start_date is None


def test_create_draft_stores_depends_on(tmp_path: Path) -> None:
    store = _store(tmp_path)
    parent = "3a8b7c12-1234-5678-9abc-def012345678"
    session = store.create_draft("job-dep", "/repo/home/mac", "Do it", depends_on=parent)
    assert session.depends_on == parent
    # Blank stays NULL (no dependency).
    blank = store.create_draft("job-nodep", "/repo/home/mac", "Other", depends_on="  ")
    assert blank.depends_on is None


def test_depends_on_column_migrates_onto_existing_db(tmp_path: Path) -> None:
    # Guards the _SESSION_COLUMNS whitelist + the ALTER-in-place migration for depends_on:
    # a pre-migration DB (schema without the column) gains it, and update_fields persists it.
    import sqlite3

    from command_center import store as store_mod

    db = tmp_path / "legacy.db"
    legacy_schema = store_mod._SCHEMA.replace("    depends_on        TEXT,\n", "")
    conn = sqlite3.connect(db)
    conn.executescript(legacy_schema)
    conn.execute("INSERT INTO sessions (session_id, cwd) VALUES ('old', '/repo/old')")
    conn.commit()
    conn.close()
    with Store(db) as store:  # opening runs _ensure_columns → ALTER adds depends_on
        row = store.get("old")
        assert row is not None
        assert row.depends_on is None  # NULL default after the migration
        store.update_fields("old", depends_on="parent-uuid")
        got = store.get("old")
        assert got is not None and got.depends_on == "parent-uuid"
