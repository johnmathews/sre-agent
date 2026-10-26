"""Post-deploy verification: did WUD really deploy the approved digest, and is the service healthy?"""

import asyncio
import html
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src.agent.tools.pbs import pbs_latest_backup_time
from src.config import get_settings
from src.observability.metrics import UPGRADE_DEPLOYS_TOTAL
from src.report.email import is_email_configured, send_report_email
from src.upgrades import store
from src.upgrades.config import AdvisedContainer, load_allowlist
from src.upgrades.http import ResponseTooLarge, bounded_request
from src.upgrades.models import DeployOutcome, DeployRecord, VerdictRecord
from src.upgrades.registry import DIGEST_RE
from src.upgrades.textutil import clean_text
from src.upgrades.wud import TRIGGER_TIMEOUT_SECONDS, WudError, get_container

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 10.0
ERROR_WINDOW = "5m"
ERROR_FLOOR = 10
ERROR_BASELINE_FACTOR = 3
BASELINE_OFFSET = timedelta(hours=1)
# Detail of a deploy whose WUD trigger was cut off by a shutdown or a hard kill (trigger_state "interrupted"; the
# trigger may or may not have taken effect).
INTERRUPTED_IN_FLIGHT = "interrupted during deploy (shutdown); verified next run"
NOT_DEPLOYED = "not deployed (interrupted by shutdown); approve again from the next email"


class _LokiStatusError(Exception):
    """Loki answered with a non-2xx status."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


def _exc_text(exc: Exception) -> str:
    """Safe description for persisted detail and emails: class name (and HTTP status) only, never message text."""
    if isinstance(exc, _LokiStatusError):
        return f"{type(exc).__name__} (HTTP {exc.status})"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{type(exc).__name__} (HTTP {exc.response.status_code})"
    return type(exc).__name__


def _exc_log(exc: Exception) -> str:
    """Full (cleaned) description, for log lines only."""
    return f"{type(exc).__name__}: {clean_text(str(exc))}"


async def _check_readiness(url: str) -> str | None:
    """Return a failure description, or None when the readiness URL answered 2xx."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await bounded_request(client, "GET", url)
    except ResponseTooLarge:
        return "readiness response too large"
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        logger.warning("readiness probe failed: %s", _exc_log(exc))
        return f"readiness probe failed: {_exc_text(exc)}"
    if not response.is_success:
        return f"readiness probe returned HTTP {response.status_code}"
    return None


async def _loki_error_count(base_url: str, container: str, at: datetime) -> int:
    """Error-ish log lines in the 5 minutes ending at ``at`` (instant metric query); raises on any problem."""
    query = f'sum(count_over_time({{container="{container}"}} |~ "(?i)(error|panic|fatal)" [{ERROR_WINDOW}]))'
    params = {"query": query, "time": str(int(at.timestamp() * 1_000_000_000))}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False) as client:
        response = await bounded_request(client, "GET", f"{base_url.rstrip('/')}/loki/api/v1/query", params=params)
    if not response.is_success:
        raise _LokiStatusError(response.status_code)
    body: Any = response.json()
    result = body["data"]["result"]
    if not result:
        return 0
    return int(float(result[0]["value"][1]))


async def _check_logs(container: str, now: datetime) -> tuple[str | None, str | None]:
    """Return ``(failure, warning)``; Loki being unconfigured or unreachable is only a warning."""
    base_url = get_settings().loki_url
    if not base_url:
        return None, "Loki not configured; error-log check skipped"
    try:
        current = await _loki_error_count(base_url, container, now)
    except (
        httpx.HTTPError,
        httpx.InvalidURL,
        ResponseTooLarge,
        _LokiStatusError,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
    ) as exc:
        logger.warning("Loki error-log check failed for %s: %s", container, _exc_log(exc))
        return None, f"Loki error-log check skipped: {_exc_text(exc)}"
    try:
        baseline = await _loki_error_count(base_url, container, now - BASELINE_OFFSET)
    except (
        httpx.HTTPError,
        httpx.InvalidURL,
        ResponseTooLarge,
        _LokiStatusError,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
    ) as exc:
        logger.warning("Loki baseline query failed for %s: %s", container, _exc_log(exc))
        return None, f"Loki baseline query failed, error-log check skipped: {_exc_text(exc)}"
    limit = max(ERROR_FLOOR, ERROR_BASELINE_FACTOR * baseline)
    if current > limit:
        return f"{current} error-like log lines in the last {ERROR_WINDOW} (limit {limit}, baseline {baseline})", None
    return None, None


async def _latest_backup(pbs_guest: str) -> str:
    """Describe the latest PBS backup of a guest; never raises (problems become a warning in the text)."""
    fallback = f"restore from the latest PBS backup of {pbs_guest}"
    settings = get_settings()
    if not settings.pbs_url:
        return fallback
    datastore = settings.pbs_default_datastore
    if not datastore:
        return f"{fallback} (warning: PBS_DEFAULT_DATASTORE not set, could not look up the backup time)"
    kind, _, guest_id = pbs_guest.partition(" ")
    try:
        taken = await pbs_latest_backup_time(kind.lower(), guest_id, datastore)
    except Exception as exc:  # noqa: BLE001 - PBS trouble must never fail verification
        logger.warning("PBS lookup for %s failed: %s", pbs_guest, _exc_log(exc))
        return f"{fallback} (warning: PBS lookup failed: {_exc_text(exc)})"
    if taken is None:
        return f"{fallback} (warning: no backup of {pbs_guest} found in datastore {datastore})"
    return f"{fallback} (taken {taken.strftime('%Y-%m-%d %H:%M UTC')})"


async def _send_failure_email(  # noqa: PLR0913
    outcome: DeployOutcome,
    name: str,
    host: str,
    pbs_guest: str,
    detail: str,
    version: str | None,
    *,
    not_deployed: bool = False,
) -> None:
    if not_deployed:  # the service was never touched: nothing to restore
        subject = f"Upgrade not deployed: {name} on {host}"
        action = "approve the update again from the next advisor email"
    else:
        label = "Upgrade FAILED" if outcome == "failed" else "Upgrade digest mismatch"
        subject = f"{label}: {name} on {host}"
        action = await _latest_backup(pbs_guest)
    text = (
        f"{subject}\n\nContainer: {name}\nHost: {host}\nCandidate version: {version or 'unknown'}\n"
        f"Outcome: {outcome}\nDetail: {detail}\n\nRecommended action: {action}.\n"
    )
    e = html.escape
    body = (
        f"<h2>{e(subject)}</h2><ul><li>Container: {e(name)}</li><li>Host: {e(host)}</li>"
        f"<li>Candidate version: {e(version or 'unknown')}</li><li>Outcome: {e(outcome)}</li>"
        f"<li>Detail: {e(detail)}</li></ul><p>Recommended action: {e(action)}.</p>"
    )
    if not is_email_configured():
        logger.error("Email not configured; NOT sent. %s", text.replace("\n", " | "))
        return
    sent = await asyncio.to_thread(send_report_email, text, body, subject)
    if not sent:
        logger.error("Failed to send upgrade failure email. %s", text.replace("\n", " | "))


def _find_container(name: str) -> tuple[AdvisedContainer | None, str]:
    """Look the container up in the allowlist; returns ``(container, reason-if-missing)``."""
    try:
        allowlist = load_allowlist(get_settings().upgrade_advisor_config_path)
    except Exception as exc:
        logger.exception("Cannot load the upgrade allowlist during verification")
        return None, f"allowlist unreadable: {type(exc).__name__}"
    found = next((c for c in allowlist if c.name == name), None)
    return found, "" if found else f"container {name} is no longer in the allowlist"


def _not_deployed_detail(deploy: DeployRecord) -> str:
    if deploy.trigger_state == "interrupted":
        return NOT_DEPLOYED
    return f"not deployed ({deploy.detail})"  # trigger_error: detail is "WUD trigger failed: <ExcType>"


async def _evaluate(
    verdict: VerdictRecord, container: AdvisedContainer, now: datetime, deploy: DeployRecord
) -> tuple[DeployOutcome, str | None, list[str], bool]:
    """Run the checks in order; returns ``(outcome, running_digest, detail parts, not_deployed)``."""
    try:
        wud = await get_container(container)
    except WudError as exc:
        logger.warning("WUD read failed for %s: %s", container.name, _exc_log(exc))
        return "failed", None, ["could not read WUD state (WudError)"], False
    if not isinstance(wud.running_digest, str) or not DIGEST_RE.fullmatch(wud.running_digest):
        return "failed", None, ["WUD reported no valid running digest"], False
    if wud.running_digest != verdict.candidate_digest:
        if (
            deploy.trigger_state in {"interrupted", "trigger_error"}
            and deploy.running_digest is not None
            and wud.running_digest == deploy.running_digest
        ):  # still the pre-deploy image: the unconfirmed trigger never took effect
            return "failed", wud.running_digest, [_not_deployed_detail(deploy)], True
        return (
            "digest_mismatch",
            wud.running_digest,
            [f"running digest {clean_text(wud.running_digest)} is not the approved {verdict.candidate_digest}"],
            False,
        )
    parts: list[str] = []
    failure = await _check_readiness(container.readiness_url)
    if failure:
        return "failed", wud.running_digest, [failure], False
    failure, warning = await _check_logs(container.name, now)
    if warning:
        parts.append(f"warning: {warning}")
    if failure:
        return "failed", wud.running_digest, [failure, *parts], False
    return "ok", wud.running_digest, ["digest matches, readiness ok, error logs quiet", *parts], False


async def verify_deploy(deploy_id: int) -> DeployRecord:
    """Verify one deploy, record the final outcome and email on failure or digest mismatch."""
    conn = store.connect(get_settings().upgrade_advisor_db_path)
    try:
        deploy = store.get_deploy(conn, deploy_id)
        if deploy is None:
            msg = f"no deploy with id {deploy_id}"
            raise LookupError(msg)
        if deploy.outcome == "triggering":
            logger.warning("deploy %s is still recorded as triggering; verify_pending settles it later", deploy_id)
            return deploy
        if deploy.outcome != "pending_verify":
            logger.warning("deploy %s already verified (%s); not re-verifying", deploy_id, deploy.outcome)
            return deploy
        verdict = store.get_verdict(conn, deploy.verdict_id)
        container, missing = _find_container(verdict.container) if verdict else (None, "")
        name = verdict.container if verdict else f"verdict {deploy.verdict_id}"
        outcome: DeployOutcome
        not_deployed = False
        if verdict is None or container is None:
            why = "its verdict can no longer be found" if verdict is None else missing
            outcome, digest, parts = "failed", None, [f"cannot verify: {why}"]
        else:
            outcome, digest, parts, not_deployed = await _evaluate(verdict, container, datetime.now(UTC), deploy)
        detail = "; ".join(parts)
        if not store.finish_deploy(conn, deploy_id, outcome, digest, detail):
            logger.warning("deploy %s was finished by a concurrent verifier; discarding this result", deploy_id)
        else:
            UPGRADE_DEPLOYS_TOTAL.labels(outcome=outcome).inc()
            logger.info("deploy %s of %s verified: %s (%s)", deploy_id, name, outcome, detail)
            if outcome != "ok":
                await _send_failure_email(
                    outcome,
                    name,
                    container.host if container else "unknown host",
                    container.pbs_guest if container else "its PBS guest",
                    detail,
                    verdict.candidate_version if verdict else None,
                    not_deployed=not_deployed,
                )
        updated = store.get_deploy(conn, deploy_id)
        if updated is None:
            msg = f"deploy {deploy_id} vanished after verification"
            raise LookupError(msg)
        return updated
    finally:
        conn.close()


def _reap_stale_triggers(conn: sqlite3.Connection, cutoff: datetime) -> None:
    """Settle ``triggering`` rows that started strictly before ``cutoff`` as interrupted (each row on its own)."""
    try:
        triggering = store.triggering_deploys(conn)
    except Exception:
        logger.exception("Could not list deploys left mid-trigger; retried on the next run")
        return
    for stale in triggering:
        if stale.started_at >= cutoff:
            continue
        try:
            if store.settle_trigger(conn, stale.id, "interrupted", INTERRUPTED_IN_FLIGHT):
                logger.warning(
                    "deploy %s was left mid-trigger (process killed?); verifying it as interrupted", stale.id
                )
        except Exception:
            logger.exception("Could not settle deploy %s left mid-trigger; retried on the next run", stale.id)


async def verify_pending() -> list[DeployRecord]:
    """Verify every pending deploy older than the verify delay (also picks up ones lost to a restart).

    A ``triggering`` row older than twice WUD's trigger timeout belongs to a process that died mid-trigger (a hard
    kill): it is settled as ``interrupted`` and verified like one. Younger ones may still be in flight and are left
    alone; the margin keeps the reaper clear of a slow trigger that is about to return. A failure on one row is
    logged and never stops the others or the pending verifications.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=settings.upgrade_advisor_verify_delay_seconds)
    trigger_cutoff = now - timedelta(seconds=2 * TRIGGER_TIMEOUT_SECONDS)
    conn = store.connect(settings.upgrade_advisor_db_path)
    try:
        _reap_stale_triggers(conn, trigger_cutoff)
        due = [d for d in store.pending_verifications(conn) if d.started_at <= cutoff]
    finally:
        conn.close()
    results: list[DeployRecord] = []
    for deploy in due:
        try:
            results.append(await verify_deploy(deploy.id))
        except Exception:
            logger.exception(
                "Verification of deploy %s crashed; it is retried on the next run only if still pending", deploy.id
            )
    return results
