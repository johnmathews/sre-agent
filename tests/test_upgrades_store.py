"""Tests for the Upgrade Advisor SQLite store."""

import sqlite3
from datetime import UTC, datetime, timedelta
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
    bid = store.create_batch(conn, [_verdict(conn)], "tok", T0, timedelta(hours=48))
    assert store.consume_batch(conn, bid, "tok", T0 + timedelta(hours=1)) == "ok"
    assert store.consume_batch(conn, bid, "tok", T0 + timedelta(hours=2)) == "used"
    batch = store.get_batch(conn, bid)
    assert batch is not None
    assert batch.used_at == T0 + timedelta(hours=1)


def test_batch_token_expires_after_ttl(conn: sqlite3.Connection) -> None:
    bid = store.create_batch(conn, [_verdict(conn)], "tok", T0, timedelta(hours=48))
    assert store.consume_batch(conn, bid, "tok", T0 + timedelta(hours=49)) == "expired"
    batch = store.get_batch(conn, bid)
    assert batch is not None
    assert batch.used_at is None


def test_wrong_token_is_invalid(conn: sqlite3.Connection) -> None:
    bid = store.create_batch(conn, [_verdict(conn)], "tok", T0, timedelta(hours=48))
    assert store.consume_batch(conn, bid, "nope", T0) == "invalid"
    assert store.consume_batch(conn, "unknown", "tok", T0) == "invalid"
    batch = store.get_batch(conn, bid)
    assert batch is not None
    assert batch.used_at is None
    assert store.consume_batch(conn, bid, "tok", T0) == "ok"


def test_token_stored_hashed(conn: sqlite3.Connection) -> None:
    token = "super-secret-raw-token"
    store.create_batch(conn, [_verdict(conn)], token, T0, timedelta(hours=48))
    dump = "\n".join(conn.iterdump())
    assert token not in dump


def test_create_batch_links_verdicts(conn: sqlite3.Connection) -> None:
    a, b = _verdict(conn, "a"), _verdict(conn, "b")
    other = _verdict(conn, "c")
    bid = store.create_batch(conn, [a, b], "tok", T0, timedelta(hours=48))
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


def test_record_decision_overwrites(conn: sqlite3.Connection) -> None:
    vid = _verdict(conn)
    store.record_decision(conn, vid, True, T0)
    store.record_decision(conn, vid, False, T0 + timedelta(hours=1))
    rows = conn.execute("SELECT approved FROM decisions WHERE verdict_id = ?", (vid,)).fetchall()
    assert [r["approved"] for r in rows] == [0]


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
