"""Deploy approved verdicts through WUD, guarded so a stale approval never deploys an unreviewed image.

Reached only from the approval page's POST (``src/api/upgrades.py``); never an agent tool and never on ``/mcp``
(a static test enforces it). Deploys run one at a time across all requests. A ticked verdict is deployed only when it
is still the latest review of its container, WUD still offers exactly the reviewed digest, and that digest is not
already running; otherwise the deploy is recorded as ``skipped`` with the reason.
"""

import asyncio
import logging
import sqlite3
import weakref
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

from src.config import get_settings
from src.observability.metrics import UPGRADE_DEPLOYS_TOTAL
from src.upgrades import store, verify, wud
from src.upgrades.config import AdvisedContainer, load_allowlist
from src.upgrades.models import DeployOutcome, DeployRecord, VerdictRecord
from src.upgrades.registry import DIGEST_RE

logger = logging.getLogger(__name__)

DEPLOYABLE = frozenset({"upgrade", "needs_you"})
SUPERSEDED = "superseded by a newer review"
UPSTREAM_MOVED = "upstream moved since review; re-reviewed next run"
ALREADY_RUNNING = "already running"
TRIGGERED = "deploy triggered; verification pending"

# Strong references to fire-and-forget tasks (deploy runs, delayed verifications) so they are not garbage-collected
# mid-flight; each removes itself when done.
_background_tasks: set[asyncio.Task[Any]] = set()
# One deploy lock per event loop (a module-level Lock would bind to the first loop that contends for it).
_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()


class InvalidSelection(ValueError):
    """The approved ids are not a valid selection from the batch (unknown, foreign, duplicated or not deployable)."""


def _deploy_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        lock = _locks[loop] = asyncio.Lock()
    return lock


def _keep[T](coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def validate_selection(conn: sqlite3.Connection, batch_id: str, approved_ids: list[int]) -> None:
    """Raise ``InvalidSelection`` unless every id is a distinct, deployable (upgrade/needs_you) verdict of the batch."""
    if len(set(approved_ids)) != len(approved_ids):
        raise InvalidSelection("duplicate verdict id")
    try:
        store.assert_batch_members(conn, batch_id, approved_ids)
    except ValueError as exc:
        raise InvalidSelection(str(exc)) from None
    readable = {v.id: v for v in store.get_batch_verdicts(conn, batch_id)}
    refused = sorted(i for i in approved_ids if i not in readable or readable[i].verdict not in DEPLOYABLE)
    if refused:
        raise InvalidSelection(f"verdicts {refused} cannot be deployed")


async def _verify_later(deploy_id: int, delay_seconds: int) -> None:
    await asyncio.sleep(delay_seconds)
    try:
        await verify.verify_deploy(deploy_id)
    except Exception:
        logger.exception("Verification of deploy %s crashed; the next advisor run retries it while pending", deploy_id)


def _load_containers() -> tuple[dict[str, AdvisedContainer], str | None]:
    """Allowlist by name, or ``({}, exception class)`` when it cannot be read."""
    try:
        containers = load_allowlist(get_settings().upgrade_advisor_config_path)
    except Exception as exc:
        logger.exception("Cannot load the upgrade allowlist for a deploy")
        return {}, type(exc).__name__
    return {c.name: c for c in containers}, None


def _digest(value: object) -> str | None:
    """A WUD digest fit to persist, or None."""
    return value if isinstance(value, str) and DIGEST_RE.fullmatch(value) else None


async def _attempt(
    conn: sqlite3.Connection,
    verdict: VerdictRecord,
    containers: dict[str, AdvisedContainer],
    allowlist_error: str | None,
) -> tuple[DeployOutcome, str | None, str]:
    """Check the guards and trigger the deploy; returns ``(outcome, running digest, detail)``."""
    latest = store.latest_verdict_for_container(conn, verdict.container)
    if latest is None or latest.id != verdict.id:
        return "skipped", None, SUPERSEDED
    if allowlist_error is not None:
        return "failed", None, f"allowlist unreadable ({allowlist_error})"
    container = containers.get(verdict.container)
    if container is None:
        return "failed", None, f"container {verdict.container} is no longer in the allowlist"
    try:
        record = await wud.get_container(container)
    except wud.WudError:
        return "failed", None, "could not read WUD state (WudError)"
    running = _digest(record.running_digest)
    if record.running_digest == verdict.candidate_digest:
        return "skipped", running, ALREADY_RUNNING
    if record.candidate_digest != verdict.candidate_digest:
        return "skipped", running, UPSTREAM_MOVED
    try:
        await wud.trigger_deploy(container, record.id)
    except wud.WudError:
        return "failed", running, "WUD deploy trigger failed (WudError)"
    return "pending_verify", running, TRIGGERED


async def _deploy_one(
    conn: sqlite3.Connection,
    verdict: VerdictRecord,
    containers: dict[str, AdvisedContainer],
    allowlist_error: str | None,
    delay_seconds: int,
) -> DeployRecord:
    outcome: DeployOutcome
    try:
        outcome, running, detail = await _attempt(conn, verdict, containers, allowlist_error)
    except Exception as exc:
        logger.exception("Deploy of %s (verdict %s) crashed", verdict.container, verdict.id)
        outcome, running, detail = "failed", None, f"deploy crashed ({type(exc).__name__})"
    deploy_id = store.save_deploy(
        conn, verdict_id=verdict.id, now=datetime.now(UTC), outcome=outcome, running_digest=running, detail=detail
    )
    log = logger.info if outcome in {"pending_verify", "skipped"} else logger.error
    log("Deploy %s of %s (verdict %s): %s, %s", deploy_id, verdict.container, verdict.id, outcome, detail)
    if outcome == "pending_verify":
        _keep(_verify_later(deploy_id, delay_seconds))
    else:  # verification counts the verified outcomes; deploys that never reached WUD are counted here
        UPGRADE_DEPLOYS_TOTAL.labels(outcome=outcome).inc()
    saved = store.get_deploy(conn, deploy_id)
    if saved is None:
        msg = f"deploy {deploy_id} vanished after it was saved"
        raise LookupError(msg)
    return saved


async def deploy_approved(batch_id: str, approved_ids: list[int]) -> list[DeployRecord]:
    """Record a decision for every verdict of the batch and deploy the ticked ones, one at a time.

    The batch must already be consumed by the caller. Raises ``InvalidSelection`` (before writing anything) when
    the ids are not deployable members of the batch.
    """
    settings = get_settings()
    async with _deploy_lock():
        conn = store.connect(settings.upgrade_advisor_db_path)
        try:
            validate_selection(conn, batch_id, approved_ids)
            verdicts = store.get_batch_verdicts(conn, batch_id)
            approved = set(approved_ids)
            decided_at = datetime.now(UTC)
            for v in verdicts:
                store.record_decision(conn, v.id, v.id in approved, decided_at)
            containers, allowlist_error = _load_containers()
            return [
                await _deploy_one(conn, v, containers, allowlist_error, settings.upgrade_advisor_verify_delay_seconds)
                for v in verdicts
                if v.id in approved
            ]
        finally:
            conn.close()


def _log_crash(task: asyncio.Task[list[DeployRecord]]) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        logger.error("Deploy run crashed: %s", type(exc).__name__, exc_info=exc)


def start_deploy(batch_id: str, approved_ids: list[int]) -> asyncio.Task[list[DeployRecord]]:
    """Run ``deploy_approved`` as a task that finishes even if the HTTP request is cancelled (client gone)."""
    task = _keep(deploy_approved(batch_id, approved_ids))
    task.add_done_callback(_log_crash)
    return task
