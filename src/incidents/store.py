"""SQLite store for incidents: one row per outage of one Gatus endpoint.

Datetimes are stored as fixed-width ISO-8601 UTC strings (microseconds), so SQL string comparison orders them
correctly, and returned as timezone-aware UTC datetimes. Naive datetimes are rejected. The connection pattern follows
``src/upgrades/store.py``.

State machine: ``open`` (TRIGGERED seen) -> ``resolved`` (RESOLVED seen) -> ``explaining`` (claimed by exactly one
job) -> ``explained`` or ``failed``. A RESOLVED with no open row creates a ``resolved`` row directly. At most one
``open`` row exists per endpoint key (a partial unique index enforces it). An ``open`` row whose trigger is older
than ``STALE_OPEN_AFTER`` (its RESOLVED was lost) is retired as ``failed``/``lost_resolve`` when the next TRIGGERED
for that key arrives.
"""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import get_args

from pydantic import ValidationError

from src.incidents.models import Incident, OutcomeState, Target

logger = logging.getLogger(__name__)

# A second RESOLVED for the same endpoint within this window, with no TRIGGERED in between, is a duplicate.
DUPLICATE_RESOLVE_WINDOW = timedelta(seconds=60)
# An open row triggered longer ago than this is assumed to have lost its RESOLVED: the next TRIGGERED retires it.
STALE_OPEN_AFTER = timedelta(hours=24)
LOST_RESOLVE_OUTCOME = "lost_resolve"
LOST_RESOLVE_DETAIL = "no RESOLVED within 24 h; a new outage opened a fresh incident"

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS incidents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_key TEXT NOT NULL,
    group_name   TEXT NOT NULL,
    name         TEXT NOT NULL,
    url          TEXT NOT NULL,
    host         TEXT,
    container    TEXT,
    triggered_at TEXT,
    resolved_at  TEXT,
    state        TEXT NOT NULL
        CHECK (state IN ('open','resolved','explaining','explained','failed')),
    outcome      TEXT,
    detail       TEXT,
    created_at   TEXT NOT NULL,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_key ON incidents(endpoint_key, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_incidents_one_open ON incidents(endpoint_key) WHERE state = 'open';
"""


def connect(path: str) -> sqlite3.Connection:
    """Open the store (WAL mode) and create the schema idempotently."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA_SQL)
    return conn


def _to_str(value: datetime) -> str:
    if value.tzinfo is None:
        msg = "naive datetime not allowed; pass a timezone-aware datetime"
        raise ValueError(msg)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _from_str(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


@contextmanager
def _immediate(conn: sqlite3.Connection) -> Iterator[None]:
    """One write transaction taken up front (``BEGIN IMMEDIATE``), so a read-then-write cannot interleave with
    another connection's."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def _row_to_incident(row: sqlite3.Row) -> Incident:
    return Incident(
        id=row["id"],
        endpoint_key=row["endpoint_key"],
        group=row["group_name"],
        name=row["name"],
        url=row["url"],
        host=row["host"],
        container=row["container"],
        triggered_at=_from_str(row["triggered_at"]),
        resolved_at=_from_str(row["resolved_at"]),
        state=row["state"],
        outcome=row["outcome"],
        detail=row["detail"],
    )


def _fetch(conn: sqlite3.Connection, incident_id: int) -> Incident:
    """A row this module just wrote; it must exist and parse."""
    row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    return _row_to_incident(row)


def _insert(
    conn: sqlite3.Connection,
    *,
    endpoint_key: str,
    group: str,
    name: str,
    url: str,
    target: Target,
    triggered_at: str | None,
    resolved_at: str | None,
    state: str,
    now: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO incidents (endpoint_key, group_name, name, url, host, container, triggered_at, resolved_at,"
        " state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (endpoint_key, group, name, url, target.host, target.container, triggered_at, resolved_at, state, now),
    )
    assert cur.lastrowid is not None  # noqa: S101 - set after INSERT
    return cur.lastrowid


def open_incident(
    conn: sqlite3.Connection,
    *,
    endpoint_key: str,
    group: str,
    name: str,
    url: str,
    target: Target,
    now: datetime,
) -> Incident:
    """Record a TRIGGERED event: return the endpoint's open incident, or open a new one at ``now``.

    A repeated TRIGGERED (Gatus retries a failed send on the next failing check) reuses the open row and keeps its
    first trigger time. An open row triggered more than ``STALE_OPEN_AFTER`` before ``now`` (or with no trigger
    time) lost its RESOLVED: it is retired (``failed``/``lost_resolve``, logged at WARNING) and a fresh row opened.
    """
    now_str = _to_str(now)
    stale_before = _to_str(now - STALE_OPEN_AFTER)
    with _immediate(conn):
        row = conn.execute(
            "SELECT id, triggered_at FROM incidents WHERE endpoint_key = ? AND state = 'open'", (endpoint_key,)
        ).fetchone()
        if row is not None and (row["triggered_at"] is None or row["triggered_at"] < stale_before):
            conn.execute(
                "UPDATE incidents SET state = 'failed', outcome = ?, detail = ?, finished_at = ? WHERE id = ?",
                (LOST_RESOLVE_OUTCOME, LOST_RESOLVE_DETAIL, now_str, row["id"]),
            )
            logger.warning(
                "incident %s (%s) stayed open since %s with no RESOLVED; retired as %s, opening a fresh incident",
                row["id"],
                endpoint_key,
                row["triggered_at"] or "an unknown time",
                LOST_RESOLVE_OUTCOME,
            )
            row = None
        if row is not None:
            incident_id: int = row["id"]
        else:
            incident_id = _insert(
                conn,
                endpoint_key=endpoint_key,
                group=group,
                name=name,
                url=url,
                target=target,
                triggered_at=now_str,
                resolved_at=None,
                state="open",
                now=now_str,
            )
    return _fetch(conn, incident_id)


def resolve_incident(
    conn: sqlite3.Connection,
    *,
    endpoint_key: str,
    group: str,
    name: str,
    url: str,
    target: Target,
    now: datetime,
) -> tuple[Incident, bool]:
    """Record a RESOLVED event. Returns the incident and whether this call newly resolved it.

    - An open row is closed (``state='resolved'``, ``resolved_at=now``): ``(incident, True)``.
    - Otherwise, when the endpoint's latest row was resolved within ``DUPLICATE_RESOLVE_WINDOW`` of ``now`` (and no
      TRIGGERED reopened it, or there would be an open row), the event is a duplicate: ``(that incident, False)``.
    - Otherwise the TRIGGERED was never seen (for example, this service was down then): a row is inserted with
      ``triggered_at=None`` and ``state='resolved'``, and the job reads the start from Gatus: ``(incident, True)``.
    """
    now_str = _to_str(now)
    with _immediate(conn):
        row = conn.execute(
            "SELECT id FROM incidents WHERE endpoint_key = ? AND state = 'open'", (endpoint_key,)
        ).fetchone()
        if row is not None:
            incident_id: int = row["id"]
            conn.execute(
                "UPDATE incidents SET state = 'resolved', resolved_at = ? WHERE id = ?", (now_str, incident_id)
            )
            new = True
        else:
            latest = conn.execute(
                "SELECT id, resolved_at FROM incidents WHERE endpoint_key = ? ORDER BY id DESC LIMIT 1",
                (endpoint_key,),
            ).fetchone()
            window_start = _to_str(now - DUPLICATE_RESOLVE_WINDOW)
            if latest is not None and latest["resolved_at"] is not None and latest["resolved_at"] >= window_start:
                incident_id = latest["id"]
                new = False
            else:
                incident_id = _insert(
                    conn,
                    endpoint_key=endpoint_key,
                    group=group,
                    name=name,
                    url=url,
                    target=target,
                    triggered_at=None,
                    resolved_at=now_str,
                    state="resolved",
                    now=now_str,
                )
                new = True
    return _fetch(conn, incident_id), new


def claim_for_explanation(conn: sqlite3.Connection, incident_id: int) -> bool:
    """Atomically move a ``resolved`` incident to ``explaining``. True only for the one caller that moved it, which
    is what guarantees at most one push per incident."""
    with conn:
        cur = conn.execute(
            "UPDATE incidents SET state = 'explaining' WHERE id = ? AND state = 'resolved'", (incident_id,)
        )
    return cur.rowcount == 1


def record_outcome(
    conn: sqlite3.Connection,
    incident_id: int,
    *,
    state: OutcomeState,
    outcome: str,
    detail: str,
    now: datetime,
) -> None:
    """Record how a claimed incident's explanation ended (``explained`` or ``failed``), with a short outcome code and
    a detail line.

    Raises ValueError for any other state or when the incident is not ``explaining`` (only the claimer records an
    outcome, once), and LookupError for an unknown id.
    """
    allowed = get_args(OutcomeState)
    if state not in allowed:
        msg = f"invalid state {state!r}; expected one of {allowed}"
        raise ValueError(msg)
    now_str = _to_str(now)
    with conn:
        cur = conn.execute(
            "UPDATE incidents SET state = ?, outcome = ?, detail = ?, finished_at = ?"
            " WHERE id = ? AND state = 'explaining'",
            (state, outcome, detail, now_str, incident_id),
        )
    if cur.rowcount == 1:
        return
    row = conn.execute("SELECT state FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    if row is None:
        msg = f"no incident with id {incident_id}"
        raise LookupError(msg)
    msg = f"incident {incident_id} is {row['state']!r}, not 'explaining'; outcome not recorded"
    raise ValueError(msg)


def get_incident(conn: sqlite3.Connection, incident_id: int) -> Incident | None:
    """Fetch one incident by id, or None (an unreadable row is logged and treated as missing)."""
    row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    if row is None:
        return None
    try:
        return _row_to_incident(row)
    except (ValidationError, ValueError, TypeError) as exc:
        logger.error("unreadable incident row id=%s: %r", incident_id, exc)  # noqa: TRY400
        return None


def list_unfinished(conn: sqlite3.Connection) -> list[Incident]:
    """Incidents a job has not finished: ``resolved`` (never claimed) and ``explaining`` (claimed, no outcome yet),
    oldest first. Read at startup to recover incidents a restart left behind. Unreadable rows are logged and
    skipped."""
    rows = conn.execute("SELECT * FROM incidents WHERE state IN ('resolved', 'explaining') ORDER BY id").fetchall()
    out: list[Incident] = []
    for row in rows:
        try:
            out.append(_row_to_incident(row))
        except (ValidationError, ValueError, TypeError) as exc:
            logger.error("unreadable incident row id=%s: %r", row["id"], exc)  # noqa: TRY400
    return out


__all__ = [
    "DUPLICATE_RESOLVE_WINDOW",
    "STALE_OPEN_AFTER",
    "claim_for_explanation",
    "connect",
    "get_incident",
    "list_unfinished",
    "open_incident",
    "record_outcome",
    "resolve_incident",
]
