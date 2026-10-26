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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.config import get_settings
from src.observability.metrics import UPGRADE_DEPLOYS_TOTAL
from src.upgrades import store, verify, wud
from src.upgrades.config import AdvisedContainer, is_advisor_configured, load_allowlist
from src.upgrades.models import DeployOutcome, DeployRecord, TriggerState, VerdictRecord
from src.upgrades.registry import DIGEST_RE
from src.upgrades.verify import INTERRUPTED_IN_FLIGHT

logger = logging.getLogger(__name__)

DEPLOYABLE = frozenset({"upgrade", "needs_you"})
SUPERSEDED = "superseded by a newer review"
UPSTREAM_MOVED = "upstream moved since review; re-reviewed next run"
ALREADY_RUNNING = "already running"
TRIGGERING = "deploy triggering"
TRIGGERED = "deploy triggered; verification pending"
INTERRUPTED = "deploy interrupted (shutdown)"

# Strong references to fire-and-forget tasks (deploy runs, delayed verifications) so they are not garbage-collected
# mid-flight; each removes itself when done.
_background_tasks: set[asyncio.Task[Any]] = set()
# The deploy runs among them (not the delayed verifications), which shutdown waits for.
_deploy_runs: set[asyncio.Task[Any]] = set()
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


@dataclass(frozen=True)
class _Ready:
    """Every guard passed: what the trigger needs."""

    container: AdvisedContainer
    wud_id: str
    running: str | None  # the pre-deploy digest, recorded so verification can tell "never deployed"


_Refusal = tuple[DeployOutcome, str | None, str]


async def _check_guards(
    conn: sqlite3.Connection,
    verdict: VerdictRecord,
    containers: dict[str, AdvisedContainer],
    allowlist_error: str | None,
) -> _Ready | _Refusal:
    """``_Ready`` when the deploy may go ahead, else ``(outcome, running digest, detail)`` to record."""
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
    return _Ready(container=container, wud_id=record.id, running=running)


def _save(
    conn: sqlite3.Connection, verdict: VerdictRecord, outcome: DeployOutcome, running: str | None, detail: str
) -> DeployRecord | None:
    """Persist one final (never-triggered) outcome. If that fails, still try to leave a ``failed`` row."""
    try:
        deploy_id = store.save_deploy(
            conn, verdict_id=verdict.id, now=datetime.now(UTC), outcome=outcome, running_digest=running, detail=detail
        )
    except Exception as exc:
        logger.exception("Could not record the %s deploy of %s (verdict %s)", outcome, verdict.container, verdict.id)
        outcome, detail = "failed", f"deploy recorded late ({type(exc).__name__})"
        try:
            deploy_id = store.save_deploy(
                conn, verdict_id=verdict.id, now=datetime.now(UTC), outcome=outcome, running_digest=None, detail=detail
            )
        except Exception:
            logger.exception("Could not record any deploy row for %s (verdict %s)", verdict.container, verdict.id)
            return None
    log = logger.info if outcome == "skipped" else logger.error
    log("Deploy %s of %s (verdict %s): %s, %s", deploy_id, verdict.container, verdict.id, outcome, detail)
    saved = store.get_deploy(conn, deploy_id)
    if saved is not None:  # verification counts verified outcomes; these never reach it
        UPGRADE_DEPLOYS_TOTAL.labels(outcome=saved.outcome).inc()
    return saved


def _settle(conn: sqlite3.Connection, deploy_id: int, state: TriggerState, detail: str) -> bool:
    """Record how the trigger ended. On a DB error the row stays ``triggering`` and ``verify_pending`` settles it as
    interrupted once ``TRIGGER_TIMEOUT_SECONDS`` have passed."""
    try:
        return store.settle_trigger(conn, deploy_id, state, detail)
    except Exception:
        logger.exception(
            "Could not record the trigger result (%s) of deploy %s; settled by a later run", state, deploy_id
        )
        return False


async def _trigger(
    conn: sqlite3.Connection, verdict: VerdictRecord, ready: _Ready, delay_seconds: int
) -> DeployRecord | None:
    """Write the ``triggering`` row, then trigger WUD and settle the row with how that ended.

    The row exists before WUD is asked, so a hard kill mid-trigger still leaves a trace that ``verify_pending``
    settles. Upstream can still move between the guard read and the trigger (WUD deploys whatever it then considers
    the candidate); verification compares the running digest with the reviewed one and emails at once on a mismatch.
    """
    try:
        deploy_id = store.save_deploy(
            conn,
            verdict_id=verdict.id,
            now=datetime.now(UTC),
            outcome="triggering",
            running_digest=ready.running,
            detail=TRIGGERING,
            trigger_state="triggering",
        )
    except Exception as exc:  # no row to track the deploy by: never trigger
        logger.exception(
            "Could not record the deploy of %s (verdict %s); not triggering", verdict.container, verdict.id
        )
        return _save(conn, verdict, "failed", None, f"not deployed: could not record the deploy ({type(exc).__name__})")
    logger.info("Deploy %s of %s (verdict %s): triggering", deploy_id, verdict.container, verdict.id)
    try:
        await wud.trigger_deploy(ready.container, ready.wud_id)
    except asyncio.CancelledError:
        # Shutdown while WUD was deploying: the deploy may well have completed. Leave it pending so the next advisor
        # run's verify_pending settles it against the real running digest, then let the cancellation through.
        _settle(conn, deploy_id, "interrupted", INTERRUPTED_IN_FLIGHT)
        raise
    except Exception as exc:  # WudError (incl. our timeout) or a bug: WUD may or may not have deployed
        kind = type(exc).__name__
        logger.error(  # noqa: TRY400 - the class is the signal; WudError already logged its cleaned message
            "Deploy %s of %s: WUD trigger failed (%s); verifying after the usual delay",
            deploy_id,
            verdict.container,
            kind,
        )
        # Verify after the normal delay, not at once: a deploy WUD completes after our client timed out must be seen.
        _settle(conn, deploy_id, "trigger_error", f"WUD trigger failed: {kind}")
        _keep(_verify_later(deploy_id, delay_seconds))
        return store.get_deploy(conn, deploy_id)
    if _settle(conn, deploy_id, "triggered", TRIGGERED):
        logger.info("Deploy %s of %s (verdict %s): %s", deploy_id, verdict.container, verdict.id, TRIGGERED)
    # WUD said it deployed: always verify, even if the row was already settled (the reaper) or the write failed.
    # verify_deploy is compare-and-set guarded, so a second verification is harmless.
    _keep(_verify_later(deploy_id, delay_seconds))
    return store.get_deploy(conn, deploy_id)


async def _deploy_one(
    conn: sqlite3.Connection,
    verdict: VerdictRecord,
    containers: dict[str, AdvisedContainer],
    allowlist_error: str | None,
    delay_seconds: int,
) -> DeployRecord | None:
    try:
        guard = await _check_guards(conn, verdict, containers, allowlist_error)
    except Exception as exc:
        logger.exception("Deploy of %s (verdict %s) crashed", verdict.container, verdict.id)
        guard = ("failed", None, f"deploy crashed ({type(exc).__name__})")
    if isinstance(guard, _Ready):
        return await _trigger(conn, verdict, guard, delay_seconds)
    return _save(conn, verdict, *guard)


def record_decisions(conn: sqlite3.Connection, batch_id: str, approved_ids: list[int], now: datetime) -> None:
    """Record approved (ticked) or not for every verdict of the batch."""
    approved = set(approved_ids)
    for v in store.get_batch_verdicts(conn, batch_id):
        store.record_decision(conn, v.id, v.id in approved, now)


def record_missing(verdict_ids: list[int], detail: str) -> None:
    """Write a ``failed`` row for every given verdict that has no deploy row yet (after a crash)."""
    try:
        conn = store.connect(get_settings().upgrade_advisor_db_path)
        try:
            have = {d.verdict_id for d in store.deploys_for_verdicts(conn, verdict_ids)}
            for vid in verdict_ids:
                if vid in have:
                    continue
                deploy_id = store.save_deploy(
                    conn, verdict_id=vid, now=datetime.now(UTC), outcome="failed", running_digest=None, detail=detail
                )
                UPGRADE_DEPLOYS_TOTAL.labels(outcome="failed").inc()
                logger.error("Deploy %s (verdict %s): failed, %s", deploy_id, vid, detail)
        finally:
            conn.close()
    except Exception:
        logger.exception("Could not record failed deploys for verdicts %s", verdict_ids)


async def deploy_approved(batch_id: str, approved_ids: list[int]) -> list[DeployRecord]:
    """Deploy the ticked verdicts of an already consumed batch, one at a time (decisions are recorded by the caller).

    Re-checks the selection (``InvalidSelection`` before anything is written) and the R23 guards per item. A failure
    in one item is recorded as ``failed`` and the next item still runs.
    """
    settings = get_settings()
    async with _deploy_lock():
        conn = store.connect(settings.upgrade_advisor_db_path)
        try:
            validate_selection(conn, batch_id, approved_ids)
            approved = set(approved_ids)
            verdicts = [v for v in store.get_batch_verdicts(conn, batch_id) if v.id in approved]
            containers, allowlist_error = _load_containers()
            records: list[DeployRecord] = []
            for v in verdicts:
                try:
                    record = await _deploy_one(
                        conn, v, containers, allowlist_error, settings.upgrade_advisor_verify_delay_seconds
                    )
                except Exception as exc:
                    logger.exception("Deploy of %s (verdict %s) crashed", v.container, v.id)
                    record_missing([v.id], f"deploy crashed ({type(exc).__name__})")
                    continue
                if record is not None:
                    records.append(record)
            return records
        finally:
            conn.close()


async def _run(batch_id: str, approved_ids: list[int]) -> list[DeployRecord]:
    try:
        return await deploy_approved(batch_id, approved_ids)
    except asyncio.CancelledError:
        logger.error("Deploy run for batch %s interrupted by shutdown", batch_id)  # noqa: TRY400 - no traceback needed
        record_missing(approved_ids, INTERRUPTED)
        raise
    except Exception as exc:
        logger.exception("Deploy run for batch %s crashed", batch_id)
        record_missing(approved_ids, f"deploy run crashed ({type(exc).__name__})")
        return []


def start_deploy(batch_id: str, approved_ids: list[int]) -> asyncio.Task[list[DeployRecord]]:
    """Deploy in the background (the approval page returns at once). A crash leaves a ``failed`` row per ticked
    verdict that has none."""
    task = _keep(_run(batch_id, approved_ids))
    _deploy_runs.add(task)
    task.add_done_callback(_deploy_runs.discard)
    return task


async def _startup_verify() -> None:
    try:
        await verify.verify_pending()
    except Exception:
        logger.exception("Startup verification of pending deploys failed; the next advisor run retries")


def start_startup_verification() -> asyncio.Task[None] | None:
    """At startup, when the advisor is configured, settle deploys left pending or mid-trigger by the last process
    (in the background; the task reference is kept). Returns the task, or None when the advisor is off."""
    if not is_advisor_configured():
        return None
    return _keep(_startup_verify())


async def wait_for_deploys(timeout_seconds: float) -> int:
    """Wait (bounded) for running deploy runs, e.g. at shutdown; returns how many are still running afterwards.

    Whatever is still running when the event loop then cancels it records itself as interrupted (see ``_run``).
    """
    if not _deploy_runs:
        return 0
    _, pending = await asyncio.wait(set(_deploy_runs), timeout=timeout_seconds)
    if pending:
        logger.error(
            "%d upgrade deploy run(s) still running after %.0fs; they will be interrupted",
            len(pending),
            timeout_seconds,
        )
    return len(pending)
