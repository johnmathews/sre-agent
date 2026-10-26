"""SQLite store for Upgrade Advisor verdicts, approval batches and deploys.

Datetimes are stored as fixed-width ISO-8601 UTC strings (microseconds), so SQL string
comparison orders them correctly, and returned as timezone-aware UTC datetimes. Naive
datetimes are rejected. Batch tokens are stored only as a SHA-256 hash; the raw token
exists only in the emailed link. Batch membership lives in ``verdicts.batch_id`` alone.
"""

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, get_args

from pydantic import ValidationError

from src.upgrades.models import (
    SETTLED_TRIGGER_STATES,
    Batch,
    DeployOutcome,
    DeployRecord,
    TriggerState,
    Verdict,
    VerdictRecord,
)

logger = logging.getLogger(__name__)


_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS first_seen (
    container  TEXT NOT NULL,
    digest     TEXT NOT NULL,
    seen_at    TEXT NOT NULL,
    PRIMARY KEY (container, digest)
);

CREATE TABLE IF NOT EXISTS verdicts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    container         TEXT NOT NULL,
    running_digest    TEXT NOT NULL,
    candidate_digest  TEXT NOT NULL,
    running_version   TEXT,
    candidate_version TEXT,
    verdict           TEXT NOT NULL CHECK (verdict IN ('upgrade','hold','needs_you')),
    summary           TEXT NOT NULL,
    details           TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    batch_id          TEXT
);
CREATE INDEX IF NOT EXISTS idx_verdicts_candidate ON verdicts(container, candidate_digest);
CREATE INDEX IF NOT EXISTS idx_verdicts_batch ON verdicts(batch_id);

CREATE TABLE IF NOT EXISTS batches (
    id          TEXT PRIMARY KEY,
    token_hash  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    verdict_id INTEGER NOT NULL PRIMARY KEY REFERENCES verdicts(id),
    approved   INTEGER NOT NULL,
    decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deploys (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id     INTEGER NOT NULL REFERENCES verdicts(id),
    started_at     TEXT NOT NULL,
    outcome        TEXT NOT NULL
        CHECK (outcome IN ('triggering','pending_verify','ok','failed','digest_mismatch','skipped')),
    running_digest TEXT,
    detail         TEXT NOT NULL,
    trigger_state  TEXT
        CHECK (trigger_state IS NULL OR trigger_state IN ('triggering','triggered','trigger_error','interrupted'))
);
CREATE INDEX IF NOT EXISTS idx_deploys_started ON deploys(started_at);

CREATE TABLE IF NOT EXISTS advisor_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    last_success_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deploys_outcome ON deploys(outcome);
"""

ConsumeResult = Literal["ok", "invalid", "used", "expired"]


def connect(path: str) -> sqlite3.Connection:
    """Open the store (WAL mode) and create the schema idempotently."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA_SQL)
    return conn


def _to_str(value: datetime) -> str:
    _require_aware(value)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _from_str(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None:
        msg = "naive datetime not allowed; pass a timezone-aware datetime"
        raise ValueError(msg)


def _check_choice(name: str, value: str, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        msg = f"invalid {name} {value!r}; expected one of {allowed}"
        raise ValueError(msg)


def _parse_rows[T](rows: list[sqlite3.Row], build: Callable[[sqlite3.Row], T], kind: str) -> list[T]:
    """Build a model from each row; a row that fails is logged with its id and skipped."""
    out: list[T] = []
    for row in rows:
        try:
            out.append(build(row))
        except (ValidationError, ValueError, TypeError) as exc:
            logger.error("skipping unreadable %s row id=%s: %r", kind, row["id"], exc)  # noqa: TRY400
    return out


def first_seen(conn: sqlite3.Connection, container: str, digest: str, now: datetime) -> datetime:
    """Record when a digest was first seen for a container; later calls return the first time."""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO first_seen (container, digest, seen_at) VALUES (?, ?, ?)",
            (container, digest, _to_str(now)),
        )
    row = conn.execute(
        "SELECT seen_at FROM first_seen WHERE container = ? AND digest = ?", (container, digest)
    ).fetchone()
    return _from_str(row["seen_at"])


def save_verdict(
    conn: sqlite3.Connection,
    *,
    container: str,
    running_digest: str,
    candidate_digest: str,
    running_version: str | None,
    candidate_version: str | None,
    verdict: Verdict,
    summary: str,
    details: dict[str, object],
    now: datetime,
) -> int:
    """Insert a verdict and return its id."""
    _check_choice("verdict", verdict, get_args(Verdict))
    created_at = _to_str(now)
    with conn:
        cur = conn.execute(
            "INSERT INTO verdicts (container, running_digest, candidate_digest, running_version,"
            " candidate_version, verdict, summary, details, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                container,
                running_digest,
                candidate_digest,
                running_version,
                candidate_version,
                verdict,
                summary,
                json.dumps(details),
                created_at,
            ),
        )
    assert cur.lastrowid is not None  # noqa: S101 - set after INSERT
    return cur.lastrowid


def _row_to_verdict(row: sqlite3.Row) -> VerdictRecord:
    return VerdictRecord(
        id=row["id"],
        container=row["container"],
        running_digest=row["running_digest"],
        candidate_digest=row["candidate_digest"],
        running_version=row["running_version"],
        candidate_version=row["candidate_version"],
        verdict=row["verdict"],
        summary=row["summary"],
        details=json.loads(row["details"]),
        created_at=_from_str(row["created_at"]),
        batch_id=row["batch_id"],
    )


def latest_verdict(conn: sqlite3.Connection, container: str, digest: str) -> VerdictRecord | None:
    """Most recent verdict for a container's candidate digest, or None."""
    row = conn.execute(
        "SELECT * FROM verdicts WHERE container = ? AND candidate_digest = ? ORDER BY id DESC LIMIT 1",
        (container, digest),
    ).fetchone()
    return _row_to_verdict(row) if row else None


def latest_verdict_for_container(conn: sqlite3.Connection, container: str) -> VerdictRecord | None:
    """Most recent verdict of any kind for a container across all candidate digests, or None.

    A newer verdict supersedes every older approval link for the container, including a later ``hold`` (for example
    a regression found since). Dry runs save no verdicts, so they cannot supersede. An unreadable newest row is
    logged and yields None (callers treat that as "not this verdict").
    """
    row = conn.execute("SELECT * FROM verdicts WHERE container = ? ORDER BY id DESC LIMIT 1", (container,)).fetchone()
    if row is None:
        return None
    parsed = _parse_rows([row], _row_to_verdict, "verdict")
    return parsed[0] if parsed else None


def get_verdict(conn: sqlite3.Connection, verdict_id: int) -> VerdictRecord | None:
    """Fetch one verdict by id, or None (an unreadable row is logged and treated as missing)."""
    row = conn.execute("SELECT * FROM verdicts WHERE id = ?", (verdict_id,)).fetchone()
    if row is None:
        return None
    parsed = _parse_rows([row], _row_to_verdict, "verdict")
    return parsed[0] if parsed else None


def create_batch(conn: sqlite3.Connection, verdict_ids: list[int], now: datetime, ttl: timedelta) -> tuple[str, str]:
    """Create an approval batch and link its verdicts; returns ``(batch_id, token)``.

    The token is generated here and only its SHA-256 is stored. Linking only succeeds for
    verdicts that exist and are not yet in a batch; otherwise nothing is written and
    ValueError is raised.
    """
    if not verdict_ids:
        msg = "a batch needs at least one verdict"
        raise ValueError(msg)
    if len(set(verdict_ids)) != len(verdict_ids):
        msg = f"duplicate verdict id in {verdict_ids}"
        raise ValueError(msg)
    created_at, expires_at = _to_str(now), _to_str(now + ttl)
    batch_id = secrets.token_urlsafe(16)
    token = secrets.token_urlsafe(32)
    with conn:
        conn.execute(
            "INSERT INTO batches (id, token_hash, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (batch_id, _hash_token(token), created_at, expires_at),
        )
        for vid in verdict_ids:
            cur = conn.execute("UPDATE verdicts SET batch_id = ? WHERE id = ? AND batch_id IS NULL", (batch_id, vid))
            if cur.rowcount != 1:
                msg = f"verdict {vid} does not exist or is already in a batch"
                raise ValueError(msg)
    return batch_id, token


def get_batch(conn: sqlite3.Connection, batch_id: str) -> Batch | None:
    """Fetch a batch by id (members derived from the verdicts), or None."""
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        return None
    return Batch(
        id=row["id"],
        verdict_ids=[v.id for v in get_batch_verdicts(conn, batch_id)],
        created_at=_from_str(row["created_at"]),
        expires_at=_from_str(row["expires_at"]),
        used_at=_from_str(row["used_at"]) if row["used_at"] else None,
    )


def get_batch_verdicts(conn: sqlite3.Connection, batch_id: str) -> list[VerdictRecord]:
    """Verdicts linked to a batch, in id order (unreadable rows are logged and skipped)."""
    rows = conn.execute("SELECT * FROM verdicts WHERE batch_id = ? ORDER BY id", (batch_id,)).fetchall()
    return _parse_rows(rows, _row_to_verdict, "verdict")


def assert_batch_members(conn: sqlite3.Connection, batch_id: str, verdict_ids: list[int]) -> None:
    """Raise ValueError unless every id is a verdict of the batch."""
    rows = conn.execute("SELECT id FROM verdicts WHERE batch_id = ?", (batch_id,)).fetchall()
    outsiders = sorted(set(verdict_ids) - {r["id"] for r in rows})
    if outsiders:
        msg = f"verdicts {outsiders} are not part of batch {batch_id}"
        raise ValueError(msg)


def _batch_state(conn: sqlite3.Connection, batch_id: str, token: str, now: datetime) -> ConsumeResult:
    """Why a batch cannot be consumed (or ok if nothing is wrong right now). Read-only."""
    row = conn.execute("SELECT token_hash, expires_at, used_at FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None or not hmac.compare_digest(row["token_hash"], _hash_token(token)):
        return "invalid"
    if row["used_at"]:
        return "used"  # used wins over expired
    if row["expires_at"] <= _to_str(now):
        return "expired"
    return "ok"


def check_batch(conn: sqlite3.Connection, batch_id: str, token: str, now: datetime) -> ConsumeResult:
    """Same verdict as ``consume_batch`` without writing anything (for the GET page)."""
    _require_aware(now)
    return _batch_state(conn, batch_id, token, now)


def consume_batch(conn: sqlite3.Connection, batch_id: str, token: str, now: datetime) -> ConsumeResult:
    """Atomically mark a batch used. Returns ok, invalid (unknown id or wrong token), used or expired."""
    now_str = _to_str(now)
    with conn:
        cur = conn.execute(
            "UPDATE batches SET used_at = ? WHERE id = ? AND used_at IS NULL AND expires_at > ? AND token_hash = ?",
            (now_str, batch_id, now_str, _hash_token(token)),
        )
    if cur.rowcount == 1:
        return "ok"
    state = _batch_state(conn, batch_id, token, now)
    # The gate failed, so "ok" here can only mean a concurrent consumer won the race.
    return "used" if state == "ok" else state


def record_decision(conn: sqlite3.Connection, verdict_id: int, approved: bool, now: datetime) -> None:  # noqa: FBT001
    """Record the approve/reject decision for a verdict.

    Repeating the same decision is a no-op; a different one raises ValueError.
    """
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO decisions (verdict_id, approved, decided_at) VALUES (?, ?, ?)",
            (verdict_id, int(approved), _to_str(now)),
        )
        row = conn.execute("SELECT approved FROM decisions WHERE verdict_id = ?", (verdict_id,)).fetchone()
        if row["approved"] != int(approved):
            msg = f"verdict {verdict_id} already has the opposite decision recorded"
            raise ValueError(msg)


def save_deploy(
    conn: sqlite3.Connection,
    *,
    verdict_id: int,
    now: datetime,
    outcome: DeployOutcome,
    running_digest: str | None,
    detail: str,
    trigger_state: TriggerState | None = None,
) -> int:
    """Insert a deploy record and return its id. A row written just before the WUD trigger is
    ``outcome="triggering", trigger_state="triggering"`` and is settled with ``settle_trigger``."""
    _check_choice("outcome", outcome, get_args(DeployOutcome))
    if trigger_state is not None:
        _check_choice("trigger_state", trigger_state, get_args(TriggerState))
    started_at = _to_str(now)
    with conn:
        cur = conn.execute(
            "INSERT INTO deploys (verdict_id, started_at, outcome, running_digest, detail, trigger_state)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (verdict_id, started_at, outcome, running_digest, detail, trigger_state),
        )
    assert cur.lastrowid is not None  # noqa: S101 - set after INSERT
    return cur.lastrowid


def update_deploy(
    conn: sqlite3.Connection, deploy_id: int, outcome: DeployOutcome, running_digest: str | None, detail: str
) -> None:
    """Update a deploy's outcome after verification. Raises LookupError if the id is unknown."""
    _check_choice("outcome", outcome, get_args(DeployOutcome))
    with conn:
        cur = conn.execute(
            "UPDATE deploys SET outcome = ?, running_digest = ?, detail = ? WHERE id = ?",
            (outcome, running_digest, detail, deploy_id),
        )
    if cur.rowcount != 1:
        msg = f"no deploy with id {deploy_id}"
        raise LookupError(msg)


def settle_trigger(conn: sqlite3.Connection, deploy_id: int, trigger_state: TriggerState, detail: str) -> bool:
    """Compare-and-set a ``triggering`` deploy to ``pending_verify`` with how its trigger ended.

    Returns True when this call made the change, False when the row was no longer ``triggering`` (already settled).
    Raises LookupError if the id is unknown, ValueError for a state that is not an end state.
    """
    _check_choice("trigger_state", trigger_state, SETTLED_TRIGGER_STATES)
    with conn:
        cur = conn.execute(
            "UPDATE deploys SET outcome = 'pending_verify', trigger_state = ?, detail = ?"
            " WHERE id = ? AND outcome = 'triggering'",
            (trigger_state, detail, deploy_id),
        )
        if cur.rowcount == 1:
            return True
    if conn.execute("SELECT 1 FROM deploys WHERE id = ?", (deploy_id,)).fetchone() is None:
        msg = f"no deploy with id {deploy_id}"
        raise LookupError(msg)
    return False


def finish_deploy(
    conn: sqlite3.Connection, deploy_id: int, outcome: DeployOutcome, running_digest: str | None, detail: str
) -> bool:
    """Compare-and-set a verification result: only a still-``pending_verify`` deploy is updated.

    Returns True when this call made the change, False when the deploy was already finished (so a concurrent
    verifier lost the race). Raises LookupError if the id is unknown.
    """
    _check_choice("outcome", outcome, get_args(DeployOutcome))
    with conn:
        cur = conn.execute(
            "UPDATE deploys SET outcome = ?, running_digest = ?, detail = ?"
            " WHERE id = ? AND outcome = 'pending_verify'",
            (outcome, running_digest, detail, deploy_id),
        )
        if cur.rowcount == 1:
            return True
    if conn.execute("SELECT 1 FROM deploys WHERE id = ?", (deploy_id,)).fetchone() is None:
        msg = f"no deploy with id {deploy_id}"
        raise LookupError(msg)
    return False


def _row_to_deploy(row: sqlite3.Row) -> DeployRecord:
    return DeployRecord(
        id=row["id"],
        verdict_id=row["verdict_id"],
        started_at=_from_str(row["started_at"]),
        outcome=row["outcome"],
        running_digest=row["running_digest"],
        detail=row["detail"],
        trigger_state=row["trigger_state"],
    )


def get_deploy(conn: sqlite3.Connection, deploy_id: int) -> DeployRecord | None:
    """Fetch one deploy by id, or None (an unreadable row is logged and treated as missing)."""
    row = conn.execute("SELECT * FROM deploys WHERE id = ?", (deploy_id,)).fetchone()
    if row is None:
        return None
    parsed = _parse_rows([row], _row_to_deploy, "deploy")
    return parsed[0] if parsed else None


def pending_verifications(conn: sqlite3.Connection) -> list[DeployRecord]:
    """Deploys still awaiting verification, oldest first (unreadable rows are logged and skipped)."""
    rows = conn.execute("SELECT * FROM deploys WHERE outcome = 'pending_verify' ORDER BY id").fetchall()
    return _parse_rows(rows, _row_to_deploy, "deploy")


def triggering_deploys(conn: sqlite3.Connection) -> list[DeployRecord]:
    """Deploys whose WUD trigger has not been settled, oldest first (unreadable rows are logged and skipped)."""
    rows = conn.execute("SELECT * FROM deploys WHERE outcome = 'triggering' ORDER BY id").fetchall()
    return _parse_rows(rows, _row_to_deploy, "deploy")


def deploys_for_verdicts(conn: sqlite3.Connection, verdict_ids: list[int]) -> list[DeployRecord]:
    """Deploys of the given verdicts, oldest first (unreadable rows are logged and skipped)."""
    if not verdict_ids:
        return []
    marks = ",".join("?" * len(verdict_ids))
    rows = conn.execute(f"SELECT * FROM deploys WHERE verdict_id IN ({marks}) ORDER BY id", verdict_ids).fetchall()
    return _parse_rows(rows, _row_to_deploy, "deploy")


def deploys_since(conn: sqlite3.Connection, since: datetime) -> list[DeployRecord]:
    """Deploys started at or after `since`, oldest first (unreadable rows are logged and skipped)."""
    rows = conn.execute(
        "SELECT * FROM deploys WHERE started_at >= ? ORDER BY started_at, id", (_to_str(since),)
    ).fetchall()
    return _parse_rows(rows, _row_to_deploy, "deploy")


def get_last_success(conn: sqlite3.Connection) -> datetime | None:
    """When the last real (non-dry) advisor run completed, or None if none has."""
    row = conn.execute("SELECT last_success_at FROM advisor_state WHERE id = 1").fetchone()
    return _from_str(row["last_success_at"]) if row else None


def set_last_success(conn: sqlite3.Connection, at: datetime) -> None:
    """Record the completion time of a successful run (one row, overwritten)."""
    value = _to_str(at)
    with conn:
        conn.execute(
            "INSERT INTO advisor_state (id, last_success_at) VALUES (1, ?)"
            " ON CONFLICT(id) DO UPDATE SET last_success_at = excluded.last_success_at",
            (value,),
        )


__all__ = [
    "ConsumeResult",
    "assert_batch_members",
    "check_batch",
    "connect",
    "consume_batch",
    "create_batch",
    "deploys_for_verdicts",
    "deploys_since",
    "finish_deploy",
    "first_seen",
    "get_batch",
    "get_batch_verdicts",
    "get_deploy",
    "get_last_success",
    "get_verdict",
    "latest_verdict",
    "latest_verdict_for_container",
    "pending_verifications",
    "record_decision",
    "save_deploy",
    "save_verdict",
    "set_last_success",
    "settle_trigger",
    "triggering_deploys",
    "update_deploy",
]
