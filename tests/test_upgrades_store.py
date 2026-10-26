"""Tests for the Upgrade Advisor SQLite store."""

import logging
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.upgrades import store

T0 = datetime(2026, 10, 26, 6, 0, tzinfo=UTC)


@pytest.fixture
def conn() -> sqlite3.Connection:
    return store.connect(":memory:")


def _verdict(conn: sqlite3.Connection, container: str = "prometheus", digest: str = "sha256:new") -> int:
    return store.save_verdict(
        conn,
        container=container,
        running_digest="sha256:old",
        candidate_digest=digest,
        running_version="v3.1.0",
        candidate_version="v3.1.1",
        verdict="upgrade",
        summary="patch release",
        details={"risks": ["none"], "n": 1},
        now=T0,
    )


def test_connect_is_idempotent_and_enables_wal(tmp_path: Path) -> None:
    path = str(tmp_path / "u.db")
    store.connect(path).close()
    c = store.connect(path)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_first_seen_is_sticky(conn: sqlite3.Connection) -> None:
    first = store.first_seen(conn, "prometheus", "sha256:a", T0)
    again = store.first_seen(conn, "prometheus", "sha256:a", T0 + timedelta(days=5))
    assert first == T0
    assert again == T0
    assert store.first_seen(conn, "prometheus", "sha256:b", T0 + timedelta(days=5)) == T0 + timedelta(days=5)


def test_naive_datetime_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="naive"):
        store.first_seen(conn, "c", "d", datetime(2026, 1, 1))  # noqa: DTZ001


def test_batch_token_is_single_use(conn: sqlite3.Connection) -> None:
    bid, token = store.create_batch(conn, [_verdict(conn)], T0, timedelta(hours=48))
    assert store.consume_batch(conn, bid, token, T0 + timedelta(hours=1)) == "ok"
    assert store.consume_batch(conn, bid, token, T0 + timedelta(hours=2)) == "used"
    batch = store.get_batch(conn, bid)
    assert batch is not None
    assert batch.used_at == T0 + timedelta(hours=1)


def test_create_batch_generates_and_returns_token(conn: sqlite3.Connection) -> None:
    _, t1 = store.create_batch(conn, [_verdict(conn, "a")], T0, timedelta(hours=1))
    _, t2 = store.create_batch(conn, [_verdict(conn, "b")], T0, timedelta(hours=1))
    assert len(t1) >= 43
    assert t1 != t2


def test_batch_token_expires_after_ttl(conn: sqlite3.Connection) -> None:
    bid, token = store.create_batch(conn, [_verdict(conn)], T0, timedelta(hours=48))
    assert store.consume_batch(conn, bid, token, T0 + timedelta(hours=49)) == "expired"
    batch = store.get_batch(conn, bid)
    assert batch is not None
    assert batch.used_at is None


def test_expiry_boundary_is_expired(conn: sqlite3.Connection) -> None:
    bid, token = store.create_batch(conn, [_verdict(conn)], T0, timedelta(hours=48))
    expires = T0 + timedelta(hours=48)
    assert store.check_batch(conn, bid, token, expires - timedelta(microseconds=1)) == "ok"
    assert store.consume_batch(conn, bid, token, expires) == "expired"
    assert store.consume_batch(conn, bid, token, expires - timedelta(microseconds=1)) == "ok"


def test_used_beats_expired(conn: sqlite3.Connection) -> None:
    bid, token = store.create_batch(conn, [_verdict(conn)], T0, timedelta(hours=48))
    assert store.consume_batch(conn, bid, token, T0) == "ok"
    late = T0 + timedelta(days=30)
    assert store.consume_batch(conn, bid, token, late) == "used"
    assert store.check_batch(conn, bid, token, late) == "used"


def test_wrong_token_is_invalid(conn: sqlite3.Connection) -> None:
    bid, token = store.create_batch(conn, [_verdict(conn)], T0, timedelta(hours=48))
    assert store.consume_batch(conn, bid, "nope", T0) == "invalid"
    assert store.consume_batch(conn, "unknown", token, T0) == "invalid"
    batch = store.get_batch(conn, bid)
    assert batch is not None
    assert batch.used_at is None
    assert store.consume_batch(conn, bid, token, T0) == "ok"


def test_check_batch_reports_all_states_without_writing(conn: sqlite3.Connection) -> None:
    bid, token = store.create_batch(conn, [_verdict(conn)], T0, timedelta(hours=48))
    assert store.check_batch(conn, bid, token, T0) == "ok"
    assert store.check_batch(conn, bid, "nope", T0) == "invalid"
    assert store.check_batch(conn, "unknown", token, T0) == "invalid"
    assert store.check_batch(conn, bid, token, T0 + timedelta(hours=49)) == "expired"
    batch = store.get_batch(conn, bid)
    assert batch is not None
    assert batch.used_at is None
    assert store.consume_batch(conn, bid, token, T0) == "ok"
    assert store.check_batch(conn, bid, token, T0) == "used"
    batch = store.get_batch(conn, bid)
    assert batch is not None
    assert batch.used_at == T0


def test_batch_functions_reject_naive_now(conn: sqlite3.Connection) -> None:
    bid, token = store.create_batch(conn, [_verdict(conn)], T0, timedelta(hours=48))
    naive = datetime(2026, 10, 26, 6, 0)  # noqa: DTZ001
    with pytest.raises(ValueError, match="naive"):
        store.check_batch(conn, bid, token, naive)
    with pytest.raises(ValueError, match="naive"):
        store.consume_batch(conn, bid, token, naive)
    with pytest.raises(ValueError, match="naive"):
        store.deploys_since(conn, naive)


def test_token_stored_hashed(conn: sqlite3.Connection) -> None:
    _, token = store.create_batch(conn, [_verdict(conn)], T0, timedelta(hours=48))
    dump = "\n".join(conn.iterdump())
    assert token not in dump


def test_create_batch_links_verdicts(conn: sqlite3.Connection) -> None:
    a, b = _verdict(conn, "a"), _verdict(conn, "b")
    other = _verdict(conn, "c")
    bid, _ = store.create_batch(conn, [a, b], T0, timedelta(hours=48))
    batch = store.get_batch(conn, bid)
    assert batch is not None
    assert batch.verdict_ids == [a, b]
    assert batch.expires_at == T0 + timedelta(hours=48)
    assert [v.id for v in store.get_batch_verdicts(conn, bid)] == [a, b]
    assert store.get_batch_verdicts(conn, "missing") == []
    latest = store.latest_verdict(conn, "c", "sha256:new")
    assert latest is not None
    assert latest.id == other
    assert latest.batch_id is None


def test_rebatching_a_verdict_raises_and_leaves_first_batch_intact(conn: sqlite3.Connection) -> None:
    a, b = _verdict(conn, "a"), _verdict(conn, "b")
    first, _ = store.create_batch(conn, [a], T0, timedelta(hours=48))
    with pytest.raises(ValueError, match="already in a batch"):
        store.create_batch(conn, [b, a], T0, timedelta(hours=48))
    assert [v.id for v in store.get_batch_verdicts(conn, first)] == [a]
    # b's link and the second batch row were rolled back with the failed transaction
    latest_b = store.latest_verdict(conn, "b", "sha256:new")
    assert latest_b is not None
    assert latest_b.batch_id is None
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1


def test_unknown_verdict_id_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        store.create_batch(conn, [12345], T0, timedelta(hours=48))
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0


def test_empty_batch_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="at least one"):
        store.create_batch(conn, [], T0, timedelta(hours=48))


def test_assert_batch_members(conn: sqlite3.Connection) -> None:
    a, b, c = _verdict(conn, "a"), _verdict(conn, "b"), _verdict(conn, "c")
    first, _ = store.create_batch(conn, [a, b], T0, timedelta(hours=48))
    second, _ = store.create_batch(conn, [c], T0, timedelta(hours=48))
    store.assert_batch_members(conn, first, [a, b])
    store.assert_batch_members(conn, first, [])
    with pytest.raises(ValueError, match="not part of batch"):
        store.assert_batch_members(conn, first, [a, c])
    with pytest.raises(ValueError, match="not part of batch"):
        store.assert_batch_members(conn, second, [999])


def test_get_batch_missing(conn: sqlite3.Connection) -> None:
    assert store.get_batch(conn, "nope") is None


def test_verdict_roundtrip_and_latest(conn: sqlite3.Connection) -> None:
    assert store.latest_verdict(conn, "prometheus", "sha256:new") is None
    _verdict(conn)
    second = _verdict(conn)
    rec = store.latest_verdict(conn, "prometheus", "sha256:new")
    assert rec is not None
    assert rec.id == second
    assert rec.details == {"risks": ["none"], "n": 1}
    assert rec.created_at == T0
    assert rec.created_at.tzinfo is not None
    assert rec.running_version == "v3.1.0"


def test_record_decision_same_is_noop_and_conflict_raises(conn: sqlite3.Connection) -> None:
    vid = _verdict(conn)
    store.record_decision(conn, vid, True, T0)
    store.record_decision(conn, vid, True, T0 + timedelta(hours=1))
    with pytest.raises(ValueError, match="opposite decision"):
        store.record_decision(conn, vid, False, T0 + timedelta(hours=2))
    rows = conn.execute("SELECT approved, decided_at FROM decisions WHERE verdict_id = ?", (vid,)).fetchall()
    assert [(r["approved"], r["decided_at"]) for r in rows] == [(1, T0.isoformat(timespec="microseconds"))]


def test_record_decision_unknown_verdict_violates_fk(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.record_decision(conn, 999, True, T0)


def test_save_deploy_unknown_verdict_violates_fk(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.save_deploy(conn, verdict_id=999, now=T0, outcome="ok", running_digest=None, detail="")


def test_invalid_verdict_and_outcome_strings_raise(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="verdict"):
        store.save_verdict(
            conn,
            container="c",
            running_digest="a",
            candidate_digest="b",
            running_version=None,
            candidate_version=None,
            verdict="maybe",  # type: ignore[arg-type]
            summary="",
            details={},
            now=T0,
        )
    vid = _verdict(conn)
    with pytest.raises(ValueError, match="outcome"):
        store.save_deploy(
            conn,
            verdict_id=vid,
            now=T0,
            outcome="great",  # type: ignore[arg-type]
            running_digest=None,
            detail="",
        )
    did = store.save_deploy(conn, verdict_id=vid, now=T0, outcome="pending_verify", running_digest=None, detail="")
    with pytest.raises(ValueError, match="outcome"):
        store.update_deploy(conn, did, "great", None, "")  # type: ignore[arg-type]
    assert conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 1


def test_check_constraints_reject_bad_values_written_directly(conn: sqlite3.Connection) -> None:
    vid = _verdict(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO deploys (verdict_id, started_at, outcome, detail) VALUES (?, 'x', 'bogus', '')", (vid,)
        )


def test_pending_verifications_skips_and_logs_bad_row(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    vid = _verdict(conn)
    good = store.save_deploy(conn, verdict_id=vid, now=T0, outcome="pending_verify", running_digest=None, detail="")
    with conn:
        cur = conn.execute(
            "INSERT INTO deploys (verdict_id, started_at, outcome, detail)"
            " VALUES (?, 'not-a-date', 'pending_verify', '')",
            (vid,),
        )
    bad = cur.lastrowid
    with caplog.at_level(logging.ERROR, logger="src.upgrades.store"):
        got = store.pending_verifications(conn)
    assert [d.id for d in got] == [good]
    assert any(f"id={bad}" in r.getMessage() for r in caplog.records)


def test_pending_verifications_lists_only_pending(conn: sqlite3.Connection) -> None:
    vid = _verdict(conn)
    pending = store.save_deploy(conn, verdict_id=vid, now=T0, outcome="pending_verify", running_digest=None, detail="")
    done = store.save_deploy(conn, verdict_id=vid, now=T0, outcome="ok", running_digest="sha256:new", detail="")
    assert [d.id for d in store.pending_verifications(conn)] == [pending]
    store.update_deploy(conn, pending, "digest_mismatch", "sha256:old", "still old")
    assert store.pending_verifications(conn) == []
    updated = {d.id: d for d in store.deploys_since(conn, T0)}
    assert updated[pending].outcome == "digest_mismatch"
    assert updated[pending].running_digest == "sha256:old"
    assert updated[done].outcome == "ok"


def test_update_deploy_unknown_id_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(LookupError):
        store.update_deploy(conn, 999, "ok", None, "")


def test_deploys_since_filters_by_start_time(conn: sqlite3.Connection) -> None:
    vid = _verdict(conn)
    old = store.save_deploy(
        conn, verdict_id=vid, now=T0 - timedelta(days=2), outcome="ok", running_digest="d", detail=""
    )
    new = store.save_deploy(conn, verdict_id=vid, now=T0, outcome="failed", running_digest=None, detail="boom")
    got = store.deploys_since(conn, T0 - timedelta(hours=1))
    assert [d.id for d in got] == [new]
    assert got[0].started_at == T0
    assert {d.id for d in store.deploys_since(conn, T0 - timedelta(days=3))} == {old, new}


def test_deploys_since_orders_by_start_time_and_handles_other_zones(conn: sqlite3.Connection) -> None:
    vid = _verdict(conn)
    later = store.save_deploy(
        conn, verdict_id=vid, now=T0 + timedelta(hours=1), outcome="ok", running_digest="d", detail=""
    )
    earlier = store.save_deploy(conn, verdict_id=vid, now=T0, outcome="ok", running_digest="d", detail="")
    cest = timezone(timedelta(hours=2))
    got = store.deploys_since(conn, (T0 - timedelta(minutes=1)).astimezone(cest))
    assert [d.id for d in got] == [earlier, later]
