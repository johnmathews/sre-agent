"""SQLite-based memory store — connection management, schema init, and CRUD.

All database operations use parameterized queries to prevent SQL injection.
Connections are created per-operation with check_same_thread=False for async
compatibility. The schema is auto-created on first access via CREATE TABLE
IF NOT EXISTS (idempotent).
"""

import json
import logging
import sqlite3
from datetime import UTC, datetime

from src.config import get_settings
from src.memory.models import BaselineRecord, IncidentRecord, QueryPatternRecord, ReportRecord

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at    TEXT NOT NULL,
    lookback_days   INTEGER NOT NULL,
    report_markdown TEXT NOT NULL,
    report_data     TEXT NOT NULL,
    active_alerts   INTEGER DEFAULT 0,
    slo_failures    INTEGER DEFAULT 0,
    total_log_errors INTEGER DEFAULT 0,
    estimated_cost  REAL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_reports_generated ON reports(generated_at);

CREATE TABLE IF NOT EXISTS incidents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    resolved_at  TEXT,
    alert_name   TEXT,
    title        TEXT NOT NULL,
    description  TEXT NOT NULL,
    root_cause   TEXT,
    resolution   TEXT,
    severity     TEXT DEFAULT 'info',
    services     TEXT DEFAULT '',
    session_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_alert ON incidents(alert_name);
CREATE INDEX IF NOT EXISTS idx_incidents_created ON incidents(created_at);

CREATE TABLE IF NOT EXISTS metric_baselines (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name  TEXT NOT NULL,
    labels       TEXT DEFAULT '{}',
    avg_value    REAL NOT NULL,
    p95_value    REAL,
    min_value    REAL,
    max_value    REAL,
    sample_count INTEGER NOT NULL,
    window_days  INTEGER NOT NULL,
    computed_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_baselines_lookup ON metric_baselines(metric_name, computed_at);

CREATE TABLE IF NOT EXISTS query_patterns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    question     TEXT NOT NULL,
    tool_names   TEXT DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_patterns_created ON query_patterns(created_at);
"""


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode for concurrent reads.

    Args:
        db_path: Explicit path to the database file. If None, reads from settings.
                 Pass ":memory:" for in-memory databases (tests).

    Returns:
        A new sqlite3.Connection with row_factory set to sqlite3.Row.

    Raises:
        ValueError: If memory is not configured (empty db path).
    """
    if db_path is None:
        settings = get_settings()
        db_path = settings.memory_db_path
    if not db_path:
        msg = "Memory store not configured (MEMORY_DB_PATH is empty)"
        raise ValueError(msg)

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables and indexes if they don't exist (idempotent)."""
    conn.executescript(_SCHEMA_SQL)


def is_memory_configured() -> bool:
    """Check whether the memory store is configured (non-empty db path)."""
    try:
        settings = get_settings()
        return bool(settings.memory_db_path)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Reports CRUD
# ---------------------------------------------------------------------------


def save_report(
    conn: sqlite3.Connection,
    *,
    generated_at: str,
    lookback_days: int,
    report_markdown: str,
    report_data: str,
    active_alerts: int = 0,
    slo_failures: int = 0,
    total_log_errors: int = 0,
    estimated_cost: float = 0.0,
) -> int:
    """Save a report to the archive. Returns the new row ID."""
    cursor = conn.execute(
        """INSERT INTO reports
           (generated_at, lookback_days, report_markdown, report_data,
            active_alerts, slo_failures, total_log_errors, estimated_cost)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            generated_at,
            lookback_days,
            report_markdown,
            report_data,
            active_alerts,
            slo_failures,
            total_log_errors,
            estimated_cost,
        ),
    )
    conn.commit()
    return cursor.lastrowid or 0


def get_latest_report(conn: sqlite3.Connection) -> ReportRecord | None:
    """Retrieve the most recently generated report."""
    row = conn.execute("SELECT * FROM reports ORDER BY generated_at DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return _row_to_report(row)


def get_reports(conn: sqlite3.Connection, limit: int = 10) -> list[ReportRecord]:
    """Retrieve the N most recent reports."""
    rows = conn.execute("SELECT * FROM reports ORDER BY generated_at DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_report(r) for r in rows]


def _row_to_report(row: sqlite3.Row) -> ReportRecord:
    return ReportRecord(
        id=row["id"],
        generated_at=row["generated_at"],
        lookback_days=row["lookback_days"],
        report_markdown=row["report_markdown"],
        report_data=row["report_data"],
        active_alerts=row["active_alerts"],
        slo_failures=row["slo_failures"],
        total_log_errors=row["total_log_errors"],
        estimated_cost=row["estimated_cost"],
    )


# ---------------------------------------------------------------------------
# Incidents CRUD
# ---------------------------------------------------------------------------


def save_incident(
    conn: sqlite3.Connection,
    *,
    title: str,
    description: str,
    alert_name: str | None = None,
    root_cause: str | None = None,
    resolution: str | None = None,
    severity: str = "info",
    services: str = "",
    session_id: str | None = None,
) -> int:
    """Record a new incident. Returns the new row ID."""
    now = datetime.now(UTC).isoformat()
    cursor = conn.execute(
        """INSERT INTO incidents
           (created_at, alert_name, title, description, root_cause,
            resolution, severity, services, session_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (now, alert_name, title, description, root_cause, resolution, severity, services, session_id),
    )
    conn.commit()
    return cursor.lastrowid or 0


def update_incident(
    conn: sqlite3.Connection,
    incident_id: int,
    *,
    resolved_at: str | None = None,
    root_cause: str | None = None,
    resolution: str | None = None,
) -> None:
    """Update fields on an existing incident (e.g. mark resolved)."""
    updates: list[str] = []
    params: list[object] = []
    if resolved_at is not None:
        updates.append("resolved_at = ?")
        params.append(resolved_at)
    if root_cause is not None:
        updates.append("root_cause = ?")
        params.append(root_cause)
    if resolution is not None:
        updates.append("resolution = ?")
        params.append(resolution)
    if not updates:
        return
    params.append(incident_id)
    conn.execute(f"UPDATE incidents SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()


def search_incidents(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    alert_name: str | None = None,
    service: str | None = None,
    limit: int = 20,
) -> list[IncidentRecord]:
    """Search incidents by keyword, alert name, or service.

    Args:
        query: Free-text search across title, description, root_cause, resolution.
        alert_name: Exact match on alert_name.
        service: Substring match on the comma-separated services field.
        limit: Maximum results to return.

    Returns:
        List of matching incidents, most recent first.
    """
    conditions: list[str] = []
    params: list[object] = []

    if query:
        conditions.append("(title LIKE ? OR description LIKE ? OR root_cause LIKE ? OR resolution LIKE ?)")
        like = f"%{query}%"
        params.extend([like, like, like, like])
    if alert_name:
        conditions.append("alert_name = ?")
        params.append(alert_name)
    if service:
        conditions.append("services LIKE ?")
        params.append(f"%{service}%")

    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM incidents{where} ORDER BY created_at DESC LIMIT ?",
        params,
    ).fetchall()
    return [_row_to_incident(r) for r in rows]


def get_open_incidents(conn: sqlite3.Connection) -> list[IncidentRecord]:
    """Retrieve all incidents that have not been marked as resolved."""
    rows = conn.execute("SELECT * FROM incidents WHERE resolved_at IS NULL ORDER BY created_at DESC").fetchall()
    return [_row_to_incident(r) for r in rows]


def _row_to_incident(row: sqlite3.Row) -> IncidentRecord:
    return IncidentRecord(
        id=row["id"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
        alert_name=row["alert_name"],
        title=row["title"],
        description=row["description"],
        root_cause=row["root_cause"],
        resolution=row["resolution"],
        severity=row["severity"],
        services=row["services"],
        session_id=row["session_id"],
    )


# ---------------------------------------------------------------------------
# Baselines CRUD
# ---------------------------------------------------------------------------


def save_baselines(conn: sqlite3.Connection, baselines: list[BaselineRecord]) -> None:
    """Bulk-insert computed metric baselines."""
    for b in baselines:
        conn.execute(
            """INSERT INTO metric_baselines
               (metric_name, labels, avg_value, p95_value, min_value, max_value,
                sample_count, window_days, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                b["metric_name"],
                b["labels"],
                b["avg_value"],
                b["p95_value"],
                b["min_value"],
                b["max_value"],
                b["sample_count"],
                b["window_days"],
                b["computed_at"],
            ),
        )
    conn.commit()


def get_baseline(
    conn: sqlite3.Connection,
    metric_name: str,
    labels: str | None = None,
) -> BaselineRecord | None:
    """Get the most recent baseline for a metric (optionally filtered by labels)."""
    if labels:
        row = conn.execute(
            "SELECT * FROM metric_baselines WHERE metric_name = ? AND labels = ? ORDER BY computed_at DESC LIMIT 1",
            (metric_name, labels),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM metric_baselines WHERE metric_name = ? ORDER BY computed_at DESC LIMIT 1",
            (metric_name,),
        ).fetchone()
    if row is None:
        return None
    return _row_to_baseline(row)


def get_baselines_for_metric(
    conn: sqlite3.Connection,
    metric_name: str,
    limit: int = 10,
) -> list[BaselineRecord]:
    """Get recent baselines for a metric across all label sets."""
    rows = conn.execute(
        "SELECT * FROM metric_baselines WHERE metric_name = ? ORDER BY computed_at DESC LIMIT ?",
        (metric_name, limit),
    ).fetchall()
    return [_row_to_baseline(r) for r in rows]


def _row_to_baseline(row: sqlite3.Row) -> BaselineRecord:
    return BaselineRecord(
        id=row["id"],
        metric_name=row["metric_name"],
        labels=row["labels"],
        avg_value=row["avg_value"],
        p95_value=row["p95_value"],
        min_value=row["min_value"],
        max_value=row["max_value"],
        sample_count=row["sample_count"],
        window_days=row["window_days"],
        computed_at=row["computed_at"],
    )


# ---------------------------------------------------------------------------
# Query Patterns CRUD
# ---------------------------------------------------------------------------


def save_query_pattern(
    conn: sqlite3.Connection,
    *,
    question: str,
    tool_names: str,
) -> int:
    """Save a query pattern (question + tools used). Returns the new row ID."""
    now = datetime.now(UTC).isoformat()
    cursor = conn.execute(
        "INSERT INTO query_patterns (question, tool_names, created_at) VALUES (?, ?, ?)",
        (question[:200], tool_names, now),
    )
    conn.commit()
    return cursor.lastrowid or 0


def get_recent_query_patterns(
    conn: sqlite3.Connection,
    limit: int = 10,
) -> list[QueryPatternRecord]:
    """Retrieve the N most recent query patterns."""
    rows = conn.execute(
        "SELECT * FROM query_patterns ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_query_pattern(r) for r in rows]


def cleanup_old_query_patterns(conn: sqlite3.Connection, keep: int = 100) -> int:
    """Delete old query patterns, keeping only the most recent N. Returns rows deleted."""
    cursor = conn.execute(
        "DELETE FROM query_patterns WHERE id NOT IN (SELECT id FROM query_patterns ORDER BY created_at DESC LIMIT ?)",
        (keep,),
    )
    conn.commit()
    return cursor.rowcount


def _row_to_query_pattern(row: sqlite3.Row) -> QueryPatternRecord:
    return QueryPatternRecord(
        id=row["id"],
        question=row["question"],
        tool_names=row["tool_names"],
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# Convenience: auto-init on connect
# ---------------------------------------------------------------------------


def get_initialized_connection(db_path: str | None = None) -> sqlite3.Connection:
    """Get a connection with schema already initialized. Convenience wrapper."""
    conn = get_connection(db_path)
    init_schema(conn)
    return conn


def _extract_report_metrics(report_data_json: str) -> dict[str, int | float]:
    """Extract summary metrics from a JSON-serialized ReportData for storage."""
    try:
        data = json.loads(report_data_json)
    except (json.JSONDecodeError, TypeError):
        return {"active_alerts": 0, "slo_failures": 0, "total_log_errors": 0, "estimated_cost": 0.0}

    active_alerts = 0
    alerts = data.get("alerts")
    if isinstance(alerts, dict):
        active_alerts = alerts.get("active_alerts", 0)

    slo_failures = 0
    slo = data.get("slo_status")
    if isinstance(slo, dict):
        # p95 latency is tracked as an SLI but no longer has a fixed target — see readme "Self-Observability".
        if slo.get("tool_success_rate") is not None and slo["tool_success_rate"] < 0.99:
            slo_failures += 1
        if slo.get("llm_error_rate") is not None and slo["llm_error_rate"] > 0.01:
            slo_failures += 1
        if slo.get("availability") is not None and slo["availability"] < 0.995:
            slo_failures += 1

    total_log_errors = 0
    loki = data.get("loki_errors")
    if isinstance(loki, dict):
        total_log_errors = loki.get("total_errors", 0)

    estimated_cost = 0.0
    cost = data.get("cost")
    if isinstance(cost, dict):
        estimated_cost = cost.get("estimated_cost_usd", 0.0)

    return {
        "active_alerts": active_alerts,
        "slo_failures": slo_failures,
        "total_log_errors": total_log_errors,
        "estimated_cost": estimated_cost,
    }
