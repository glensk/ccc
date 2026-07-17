"""Tests for the read-only external homelab-overseer card data source.

Every test builds a throwaway fixture SQLite DB under ``tmp_path`` (the external
schema) and points ``nixos_overseer_dir`` at it — NO real path is ever touched.
The reads must never raise; every failure mode collapses to a placeholder.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from command_center import config, nixos_overseer

# A fixed "now" (Unix seconds) so age humanization and the 7-day window are deterministic.
_NOW = 1_700_000_000

# The external schema, owned by another repo (mirrored verbatim in the module docstring).
_SCHEMA = (
    "CREATE TABLE incidents ("
    "id TEXT, fingerprint TEXT, first_seen INT, last_seen INT, occurrences INT, "
    "status TEXT, tier TEXT, track TEXT, title TEXT, md_path TEXT, model TEXT, "
    "session_id TEXT, cost_usd REAL)",
    "CREATE TABLE kv (key TEXT, value TEXT)",
)


def _cfg(overseer_dir: Path | str) -> config.Config:
    cfg = config.Config()
    cfg.nixos_overseer_dir = str(overseer_dir)
    return cfg


def _build_db(
    overseer_dir: Path,
    incidents: list[dict[str, object]],
    kv: dict[str, str] | None = None,
) -> None:
    """Create ``<overseer_dir>/state/overseer.sqlite`` with the given incidents / kv."""
    db = overseer_dir / "state" / "overseer.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        for stmt in _SCHEMA:
            conn.execute(stmt)
        for inc in incidents:
            conn.execute(
                "INSERT INTO incidents "
                "(id, fingerprint, first_seen, last_seen, occurrences, status, tier) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    inc.get("id", ""),
                    inc.get("fingerprint", ""),
                    inc.get("first_seen", 0),
                    inc.get("last_seen", 0),
                    inc.get("occurrences", 1),
                    inc.get("status", "new"),
                    inc.get("tier", "b"),
                ),
            )
        for key, value in (kv or {}).items():
            conn.execute("INSERT INTO kv (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
    finally:
        conn.close()


# ---- db_path + placeholders --------------------------------------------------
def test_db_path_is_state_overseer_sqlite_under_dir() -> None:
    cfg = _cfg("~/overseer")
    assert (
        nixos_overseer.db_path(cfg) == Path("~/overseer").expanduser() / "state" / "overseer.sqlite"
    )


def test_dir_unset_is_placeholder(tmp_path: Path) -> None:
    """An empty ``nixos_overseer_dir`` (the default) means the feature is OFF."""
    cfg = _cfg("")
    sup = nixos_overseer.read_supervised(cfg)
    tier = nixos_overseer.read_tier_a(cfg, now=_NOW)
    assert sup.state == nixos_overseer.STATE_DIR_UNSET
    assert tier.state == nixos_overseer.STATE_DIR_UNSET
    assert "set nixos_overseer_dir in config.toml" in nixos_overseer.render_supervised(sup).plain
    assert "set nixos_overseer_dir in config.toml" in nixos_overseer.render_tier_a(tier).plain


def test_db_missing_is_placeholder(tmp_path: Path) -> None:
    """Dir set but no sqlite file → the 'db not found' placeholder (no raise)."""
    cfg = _cfg(tmp_path / "overseer")  # nothing created
    sup = nixos_overseer.read_supervised(cfg)
    tier = nixos_overseer.read_tier_a(cfg, now=_NOW)
    assert sup.state == nixos_overseer.STATE_DB_MISSING
    assert tier.state == nixos_overseer.STATE_DB_MISSING
    assert "overseer db not found" in nixos_overseer.render_supervised(sup).plain
    assert "overseer db not found" in nixos_overseer.render_tier_a(tier).plain


def test_corrupt_db_is_error_placeholder_never_raises(tmp_path: Path) -> None:
    """A file that is not a valid DB yields STATE_ERROR, not an exception."""
    overseer = tmp_path / "overseer"
    (overseer / "state").mkdir(parents=True)
    (overseer / "state" / "overseer.sqlite").write_bytes(b"this is not sqlite at all")
    cfg = _cfg(overseer)
    sup = nixos_overseer.read_supervised(cfg)
    tier = nixos_overseer.read_tier_a(cfg, now=_NOW)
    assert sup.state == nixos_overseer.STATE_ERROR
    assert tier.state == nixos_overseer.STATE_ERROR
    assert "overseer db unavailable" in nixos_overseer.render_supervised(sup).plain
    assert "overseer db unavailable" in nixos_overseer.render_tier_a(tier).plain


# ---- supervised card ---------------------------------------------------------
def test_supervised_shows_only_supervised_statuses_newest_first(tmp_path: Path) -> None:
    overseer = tmp_path / "overseer"
    _build_db(
        overseer,
        [
            {
                "id": "i1",
                "fingerprint": "fp1",
                "first_seen": _NOW - 100,
                "last_seen": _NOW - 60,
                "status": "proposed_tier_b",
            },
            {
                "id": "i2",
                "fingerprint": "fp2",
                "first_seen": _NOW - 300,
                "last_seen": _NOW - 200,
                "status": "needs_supervised_plan",
            },
            {
                "id": "i3",
                "fingerprint": "fp3",
                "first_seen": _NOW - 50,
                "last_seen": _NOW - 10,
                "status": "open_unverified",
            },
            # These statuses are NOT awaiting the human → excluded.
            {
                "id": "x1",
                "fingerprint": "fpx",
                "first_seen": _NOW - 40,
                "last_seen": _NOW,
                "status": "verified_closed",
            },
            {
                "id": "x2",
                "fingerprint": "fpy",
                "first_seen": _NOW - 20,
                "last_seen": _NOW,
                "status": "new",
            },
        ],
    )
    result = nixos_overseer.read_supervised(_cfg(overseer))
    assert result.state == nixos_overseer.STATE_OK
    # Newest first by first_seen DESC: i3 (-50), i1 (-100), i2 (-300).
    assert [r.id for r in result.rows] == ["i3", "i1", "i2"]
    assert {r.status for r in result.rows} == {
        "proposed_tier_b",
        "needs_supervised_plan",
        "open_unverified",
    }
    assert not result.dispatch_disabled
    rendered = nixos_overseer.render_supervised(result, now=_NOW).plain
    # Row format: `<id>  <status>  <fingerprint>  <age>` (age from last_seen).
    assert "i1  proposed_tier_b  fp1  1m" in rendered
    assert "i3  open_unverified  fp3  10s" in rendered
    assert "approve: overseer.py approve <id> --close" in rendered
    assert "dispatch disabled" not in rendered


def test_supervised_zero_rows_is_the_good_none_state(tmp_path: Path) -> None:
    overseer = tmp_path / "overseer"
    _build_db(
        overseer,
        [
            {
                "id": "x",
                "fingerprint": "f",
                "first_seen": _NOW,
                "last_seen": _NOW,
                "status": "verified_closed",
            }
        ],
    )
    result = nixos_overseer.read_supervised(_cfg(overseer))
    assert result.state == nixos_overseer.STATE_OK
    assert result.rows == ()
    assert nixos_overseer.render_supervised(result, now=_NOW).plain == "— none —"


def test_dispatch_disabled_via_kv_auto_disabled(tmp_path: Path) -> None:
    overseer = tmp_path / "overseer"
    _build_db(
        overseer,
        [
            {
                "id": "i1",
                "fingerprint": "fp1",
                "first_seen": _NOW - 100,
                "last_seen": _NOW - 60,
                "status": "open_unverified",
            }
        ],
        kv={"auto_disabled": "1"},
    )
    result = nixos_overseer.read_supervised(_cfg(overseer))
    assert result.dispatch_disabled is True
    rendered = nixos_overseer.render_supervised(result, now=_NOW).plain
    assert rendered.startswith("⛔ dispatch disabled")
    assert "i1  open_unverified  fp1" in rendered


def test_dispatch_not_disabled_when_kv_zero(tmp_path: Path) -> None:
    overseer = tmp_path / "overseer"
    _build_db(
        overseer,
        [
            {
                "id": "i1",
                "fingerprint": "fp1",
                "first_seen": _NOW,
                "last_seen": _NOW,
                "status": "open_unverified",
            }
        ],
        kv={"auto_disabled": "0"},
    )
    result = nixos_overseer.read_supervised(_cfg(overseer))
    assert result.dispatch_disabled is False


def test_dispatch_disabled_via_disabled_file(tmp_path: Path) -> None:
    overseer = tmp_path / "overseer"
    _build_db(
        overseer,
        [
            {
                "id": "i1",
                "fingerprint": "fp1",
                "first_seen": _NOW,
                "last_seen": _NOW,
                "status": "needs_supervised_plan",
            }
        ],
    )
    (overseer / "DISABLED").write_text("halted\n", encoding="utf-8")
    result = nixos_overseer.read_supervised(_cfg(overseer))
    assert result.dispatch_disabled is True
    assert "⛔ dispatch disabled" in nixos_overseer.render_supervised(result, now=_NOW).plain


# ---- tier_a card -------------------------------------------------------------
def test_tier_a_window_cap_and_tail(tmp_path: Path) -> None:
    overseer = tmp_path / "overseer"
    incidents: list[dict[str, object]] = []
    # 12 tier-a rows inside the 7-day window (newest first once sorted).
    for i in range(12):
        incidents.append(
            {
                "id": f"a{i:02d}",
                "fingerprint": f"fp{i}",
                "first_seen": _NOW - i * 60,
                "last_seen": _NOW - i * 30,
                "status": "verified_closed",
                "tier": "a",
            }
        )
    # tier-a but OLDER than 7 days → excluded by the window.
    incidents.append(
        {
            "id": "old",
            "fingerprint": "fpo",
            "first_seen": _NOW - 8 * 86400,
            "last_seen": _NOW - 8 * 86400,
            "status": "recovered",
            "tier": "a",
        }
    )
    # inside the window but tier b → excluded (tier_a card is tier='a' only).
    incidents.append(
        {
            "id": "bee",
            "fingerprint": "fpb",
            "first_seen": _NOW - 30,
            "last_seen": _NOW,
            "status": "open_unverified",
            "tier": "b",
        }
    )
    _build_db(overseer, incidents)
    result = nixos_overseer.read_tier_a(_cfg(overseer), now=_NOW)
    assert result.state == nixos_overseer.STATE_OK
    assert len(result.rows) == 10  # capped
    assert result.more == 2  # 12 - 10
    ids = [r.id for r in result.rows]
    assert ids[0] == "a00"  # newest (first_seen == _NOW)
    assert "old" not in ids and "bee" not in ids
    rendered = nixos_overseer.render_tier_a(result, now=_NOW).plain
    assert "… +2 more" in rendered
    assert "a00  verified_closed  fp0  0s" in rendered


def test_tier_a_any_status_within_window(tmp_path: Path) -> None:
    """tier_a takes ANY status (unlike supervised), as long as tier='a' and recent."""
    overseer = tmp_path / "overseer"
    _build_db(
        overseer,
        [
            {
                "id": "n",
                "fingerprint": "fn",
                "first_seen": _NOW - 10,
                "last_seen": _NOW,
                "status": "new",
                "tier": "a",
            },
            {
                "id": "c",
                "fingerprint": "fc",
                "first_seen": _NOW - 20,
                "last_seen": _NOW,
                "status": "verified_closed",
                "tier": "a",
            },
            {
                "id": "r",
                "fingerprint": "fr",
                "first_seen": _NOW - 30,
                "last_seen": _NOW,
                "status": "recovered",
                "tier": "a",
            },
        ],
    )
    result = nixos_overseer.read_tier_a(_cfg(overseer), now=_NOW)
    assert [r.id for r in result.rows] == ["n", "c", "r"]  # newest first, no cap tail
    assert result.more == 0
    assert "… +" not in nixos_overseer.render_tier_a(result, now=_NOW).plain


def test_tier_a_zero_rows_is_neutral_none(tmp_path: Path) -> None:
    overseer = tmp_path / "overseer"
    _build_db(
        overseer,
        [
            {
                "id": "x",
                "fingerprint": "f",
                "first_seen": _NOW,
                "last_seen": _NOW,
                "status": "new",
                "tier": "b",
            }
        ],  # only tier b → nothing for the tier_a card
    )
    result = nixos_overseer.read_tier_a(_cfg(overseer), now=_NOW)
    assert result.state == nixos_overseer.STATE_OK
    assert result.rows == ()
    assert nixos_overseer.render_tier_a(result, now=_NOW).plain == "— none —"


# ---- age humanization --------------------------------------------------------
def test_humanize_age_buckets() -> None:
    fn = nixos_overseer._humanize_age
    assert fn(0, _NOW) == "—"
    assert fn(_NOW - 5, _NOW) == "5s"
    assert fn(_NOW - 120, _NOW) == "2m"
    assert fn(_NOW - 2 * 3600, _NOW) == "2h"
    assert fn(_NOW - 3 * 86400, _NOW) == "3d"
    assert fn(_NOW - 14 * 86400, _NOW) == "2w"
    assert fn(_NOW + 100, _NOW) == "0s"  # future timestamp clamps to 0
