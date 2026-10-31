"""Unit tests for the incident SQLite store (a fresh database file per test)."""

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.incidents import store
from src.incidents.models import Incident, Target

T0 = datetime(2026, 10, 27, 14, 0, tzinfo=UTC)
KEY = "monitoring_dozzle"
TARGET = Target(host="infra", container="dozzle")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return store.connect(str(tmp_path / "incidents.db"))


def _open(conn: sqlite3.Connection, now: datetime = T0, key: str = KEY) -> Incident:
    return store.open_incident(
        conn,
        endpoint_key=key,
        group="monitoring",
        name="Dozzle",
        url="http://dozzle.example.test",
        target=TARGET,
        now=now,
    )


def _resolve(conn: sqlite3.Connection, now: datetime, key: str = KEY) -> tuple[Incident, bool]:
    return store.resolve_incident(
        conn,
        endpoint_key=key,
        group="monitoring",
        name="Dozzle",
        url="http://dozzle.example.test",
        target=TARGET,
        now=now,
    )


def test_connect_is_idempotent_and_enables_wal(tmp_path: Path) -> None:
    path = str(tmp_path / "i.db")
    store.connect(path).close()
    c = store.connect(path)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_open_incident_inserts_an_open_row(conn: sqlite3.Connection) -> None:
    incident = _open(conn)
    assert incident.endpoint_key == KEY
    assert (incident.group, incident.name) == ("monitoring", "Dozzle")
    assert incident.url == "http://dozzle.example.test"
    assert (incident.host, incident.container) == ("infra", "dozzle")
    assert incident.triggered_at == T0
    assert incident.triggered_at.tzinfo is UTC
    assert incident.resolved_at is None
    assert incident.state == "open"
    assert (incident.outcome, incident.detail) == (None, None)
    assert store.get_incident(conn, incident.id) == incident


def test_repeated_triggered_reuses_the_open_row(conn: sqlite3.Connection) -> None:
    first = _open(conn)
    again = _open(conn, now=T0 + timedelta(minutes=2))
    assert again.id == first.id
    assert again.triggered_at == T0  # the first trigger time is kept
    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1


def test_open_rows_are_per_endpoint(conn: sqlite3.Connection) -> None:
    a = _open(conn)
    b = _open(conn, key="monitoring_grafana")
    assert a.id != b.id


def test_triggered_then_resolved_closes_the_row(conn: sqlite3.Connection) -> None:
    opened = _open(conn)
    resolved, new = _resolve(conn, T0 + timedelta(minutes=5))
    assert new is True
    assert resolved.id == opened.id
    assert resolved.state == "resolved"
    assert resolved.triggered_at == T0
    assert resolved.resolved_at == T0 + timedelta(minutes=5)


def test_resolved_without_open_row_inserts_a_resolved_row(conn: sqlite3.Connection) -> None:
    resolved, new = _resolve(conn, T0)
    assert new is True
    assert resolved.state == "resolved"
    assert resolved.triggered_at is None
    assert resolved.resolved_at == T0
    assert (resolved.host, resolved.container) == ("infra", "dozzle")


def test_duplicate_resolved_returns_false(conn: sqlite3.Connection) -> None:
    _open(conn)
    first, new = _resolve(conn, T0 + timedelta(minutes=5))
    assert new is True
    dup, dup_new = _resolve(conn, T0 + timedelta(minutes=5, seconds=30))
    assert dup_new is False
    assert dup.id == first.id
    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1


def test_duplicate_resolved_is_detected_after_the_explanation_started(conn: sqlite3.Connection) -> None:
    first, _ = _resolve(conn, T0)
    assert store.claim_for_explanation(conn, first.id) is True
    dup, new = _resolve(conn, T0 + timedelta(seconds=10))
    assert new is False
    assert dup.id == first.id
    assert dup.state == "explaining"


def test_resolved_after_the_duplicate_window_is_a_new_incident(conn: sqlite3.Connection) -> None:
    first, _ = _resolve(conn, T0)
    later, new = _resolve(conn, T0 + timedelta(seconds=61))
    assert new is True
    assert later.id != first.id


def test_reopened_then_resolved_again_is_new(conn: sqlite3.Connection) -> None:
    _open(conn)
    first, _ = _resolve(conn, T0 + timedelta(seconds=10))
    reopened = _open(conn, now=T0 + timedelta(seconds=20))
    assert reopened.id != first.id
    second, new = _resolve(conn, T0 + timedelta(seconds=30))
    assert new is True
    assert second.id == reopened.id
    assert second.triggered_at == T0 + timedelta(seconds=20)


def test_duplicate_window_is_per_endpoint(conn: sqlite3.Connection) -> None:
    _resolve(conn, T0)
    _, new = _resolve(conn, T0 + timedelta(seconds=5), key="monitoring_grafana")
    assert new is True


def test_claim_for_explanation_succeeds_once(conn: sqlite3.Connection) -> None:
    resolved, _ = _resolve(conn, T0)
    assert store.claim_for_explanation(conn, resolved.id) is True
    assert store.claim_for_explanation(conn, resolved.id) is False
    incident = store.get_incident(conn, resolved.id)
    assert incident is not None
    assert incident.state == "explaining"


def test_claim_for_explanation_refuses_open_and_unknown_rows(conn: sqlite3.Connection) -> None:
    opened = _open(conn)
    assert store.claim_for_explanation(conn, opened.id) is False
    assert store.claim_for_explanation(conn, 9999) is False


def test_claim_is_exclusive_across_connections(tmp_path: Path) -> None:
    path = str(tmp_path / "shared.db")
    a, b = store.connect(path), store.connect(path)
    resolved, _ = _resolve(a, T0)
    results = [store.claim_for_explanation(c, resolved.id) for c in (a, b, a, b)]
    assert results == [True, False, False, False]


def test_record_outcome(conn: sqlite3.Connection) -> None:
    resolved, _ = _resolve(conn, T0)
    store.claim_for_explanation(conn, resolved.id)
    store.record_outcome(
        conn, resolved.id, state="explained", outcome="sent", detail="pushover request abc", now=T0 + timedelta(1)
    )
    incident = store.get_incident(conn, resolved.id)
    assert incident is not None
    assert (incident.state, incident.outcome, incident.detail) == ("explained", "sent", "pushover request abc")


def test_record_outcome_failed(conn: sqlite3.Connection) -> None:
    resolved, _ = _resolve(conn, T0)
    store.claim_for_explanation(conn, resolved.id)
    store.record_outcome(conn, resolved.id, state="failed", outcome="pushover_error", detail="HTTP 400", now=T0)
    incident = store.get_incident(conn, resolved.id)
    assert incident is not None
    assert incident.state == "failed"


def test_record_outcome_requires_a_claimed_incident(conn: sqlite3.Connection) -> None:
    resolved, _ = _resolve(conn, T0)
    with pytest.raises(ValueError, match="explaining"):
        store.record_outcome(conn, resolved.id, state="explained", outcome="sent", detail="", now=T0)
    with pytest.raises(LookupError):
        store.record_outcome(conn, 9999, state="explained", outcome="sent", detail="", now=T0)


def test_record_outcome_rejects_an_unknown_state(conn: sqlite3.Connection) -> None:
    resolved, _ = _resolve(conn, T0)
    store.claim_for_explanation(conn, resolved.id)
    with pytest.raises(ValueError, match="state"):
        store.record_outcome(conn, resolved.id, state="open", outcome="x", detail="", now=T0)  # type: ignore[arg-type]


def test_get_incident_unknown_is_none(conn: sqlite3.Connection) -> None:
    assert store.get_incident(conn, 1) is None


def test_naive_datetimes_are_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="naive"):
        _open(conn, now=datetime(2026, 10, 27, 14, 0))  # noqa: DTZ001 - deliberately naive


def test_non_utc_datetimes_are_stored_and_returned_as_utc(conn: sqlite3.Connection) -> None:
    cest = timezone(timedelta(hours=2))
    incident = _open(conn, now=datetime(2026, 10, 27, 16, 0, tzinfo=cest))
    assert incident.triggered_at == T0
    assert incident.triggered_at is not None
    assert incident.triggered_at.utcoffset() == timedelta(0)
