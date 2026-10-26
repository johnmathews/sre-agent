"""SQLite store for Upgrade Advisor verdicts, approval batches and deploys.

Datetimes are stored as ISO-8601 UTC strings and returned as timezone-aware UTC
datetimes. Naive datetimes are rejected. Batch tokens are stored only as a
SHA-256 hash; the raw token exists only in the emailed link.
"""

import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Literal

from src.upgrades.models import Batch, DeployOutcome, DeployRecord, Verdict, VerdictRecord

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
    verdict           TEXT NOT NULL,
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
    verdict_ids TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    verdict_id INTEGER PRIMARY KEY,
    approved   INTEGER NOT NULL,
    decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deploys (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id     INTEGER NOT NULL,
    started_at     TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    running_digest TEXT,
    detail         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deploys_started ON deploys(started_at);
CREATE INDEX IF NOT EXISTS idx_deploys_outcome ON deploys(outcome);
"""

ConsumeResult = Literal["ok", "invalid", "used", "expired"]


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
    return value.astimezone(UTC).isoformat()


def _from_str(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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
                _to_str(now),
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


def create_batch(conn: sqlite3.Connection, verdict_ids: list[int], token: str, now: datetime, ttl: timedelta) -> str:
    """Create an approval batch (storing only the token's SHA-256) and link its verdicts."""
    batch_id = secrets.token_urlsafe(16)
    with conn:
        conn.execute(
            "INSERT INTO batches (id, token_hash, verdict_ids, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (batch_id, _hash_token(token), json.dumps(verdict_ids), _to_str(now), _to_str(now + ttl)),
        )
        conn.executemany("UPDATE verdicts SET batch_id = ? WHERE id = ?", [(batch_id, vid) for vid in verdict_ids])
    return batch_id


def get_batch(conn: sqlite3.Connection, batch_id: str) -> Batch | None:
    """Fetch a batch by id, or None."""
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        return None
    return Batch(
        id=row["id"],
        verdict_ids=json.loads(row["verdict_ids"]),
        created_at=_from_str(row["created_at"]),
        expires_at=_from_str(row["expires_at"]),
        used_at=_from_str(row["used_at"]) if row["used_at"] else None,
    )


def get_batch_verdicts(conn: sqlite3.Connection, batch_id: str) -> list[VerdictRecord]:
    """Verdicts linked to a batch, in id order."""
    rows = conn.execute("SELECT * FROM verdicts WHERE batch_id = ? ORDER BY id", (batch_id,)).fetchall()
    return [_row_to_verdict(r) for r in rows]


def consume_batch(conn: sqlite3.Connection, batch_id: str, token: str, now: datetime) -> ConsumeResult:
    """Atomically mark a batch used. Returns ok, invalid (unknown id or wrong token), used or expired."""
    row = conn.execute("SELECT token_hash, expires_at FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None or not hmac.compare_digest(row["token_hash"], _hash_token(token)):
        return "invalid"
    if _from_str(row["expires_at"]) <= now:
        # Report "used" in preference to "expired" if both apply.
        used = conn.execute("SELECT used_at FROM batches WHERE id = ?", (batch_id,)).fetchone()["used_at"]
        return "used" if used else "expired"
    with conn:
        cur = conn.execute("UPDATE batches SET used_at = ? WHERE id = ? AND used_at IS NULL", (_to_str(now), batch_id))
    return "ok" if cur.rowcount == 1 else "used"


def record_decision(conn: sqlite3.Connection, verdict_id: int, approved: bool, now: datetime) -> None:  # noqa: FBT001
    """Record the approve/reject decision for a verdict; a repeat call overwrites."""
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO decisions (verdict_id, approved, decided_at) VALUES (?, ?, ?)",
            (verdict_id, int(approved), _to_str(now)),
        )


def save_deploy(
    conn: sqlite3.Connection,
    *,
    verdict_id: int,
    now: datetime,
    outcome: DeployOutcome,
    running_digest: str | None,
    detail: str,
) -> int:
    """Insert a deploy record and return its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO deploys (verdict_id, started_at, outcome, running_digest, detail) VALUES (?, ?, ?, ?, ?)",
            (verdict_id, _to_str(now), outcome, running_digest, detail),
        )
    assert cur.lastrowid is not None  # noqa: S101 - set after INSERT
    return cur.lastrowid


def update_deploy(
    conn: sqlite3.Connection, deploy_id: int, outcome: DeployOutcome, running_digest: str | None, detail: str
) -> None:
    """Update a deploy's outcome after verification. Raises LookupError if the id is unknown."""
    with conn:
        cur = conn.execute(
            "UPDATE deploys SET outcome = ?, running_digest = ?, detail = ? WHERE id = ?",
            (outcome, running_digest, detail, deploy_id),
        )
    if cur.rowcount != 1:
        msg = f"no deploy with id {deploy_id}"
        raise LookupError(msg)


def _row_to_deploy(row: sqlite3.Row) -> DeployRecord:
    return DeployRecord(
        id=row["id"],
        verdict_id=row["verdict_id"],
        started_at=_from_str(row["started_at"]),
        outcome=row["outcome"],
        running_digest=row["running_digest"],
        detail=row["detail"],
    )


def pending_verifications(conn: sqlite3.Connection) -> list[DeployRecord]:
    """Deploys still awaiting verification, oldest first."""
    rows = conn.execute("SELECT * FROM deploys WHERE outcome = 'pending_verify' ORDER BY id").fetchall()
    return [_row_to_deploy(r) for r in rows]


def deploys_since(conn: sqlite3.Connection, since: datetime) -> list[DeployRecord]:
    """Deploys started at or after `since`, oldest first."""
    rows = conn.execute("SELECT * FROM deploys ORDER BY id").fetchall()
    return [d for d in (_row_to_deploy(r) for r in rows) if d.started_at >= since.astimezone(UTC)]


__all__ = [
    "ConsumeResult",
    "connect",
    "consume_batch",
    "create_batch",
    "deploys_since",
    "first_seen",
    "get_batch",
    "get_batch_verdicts",
    "latest_verdict",
    "pending_verifications",
    "record_decision",
    "save_deploy",
    "save_verdict",
    "update_deploy",
]
