"""Post-deploy verification: did WUD really deploy the approved digest, and is the service healthy?"""

import asyncio
import html
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src.agent.tools.pbs import _pbs_get
from src.config import get_settings
from src.observability.metrics import UPGRADE_DEPLOYS_TOTAL
from src.report.email import is_email_configured, send_report_email
from src.upgrades import store
from src.upgrades.config import AdvisedContainer, load_allowlist
from src.upgrades.http import ResponseTooLarge, bounded_request
from src.upgrades.models import DeployOutcome, DeployRecord, VerdictRecord
from src.upgrades.textutil import clean_text
from src.upgrades.wud import WudError, get_container

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 10.0
ERROR_WINDOW = "5m"
ERROR_FLOOR = 10
ERROR_BASELINE_FACTOR = 3
BASELINE_OFFSET = timedelta(hours=1)


def _exc_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {clean_text(str(exc))}"


async def _check_readiness(url: str) -> str | None:
    """Return a failure description, or None when the readiness URL answered 2xx."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await bounded_request(client, "GET", url)
    except ResponseTooLarge:
        return "readiness response too large"
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
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
        msg = f"HTTP {response.status_code}"
        raise ValueError(msg)
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
        baseline = await _loki_error_count(base_url, container, now - BASELINE_OFFSET)
    except (httpx.HTTPError, httpx.InvalidURL, ResponseTooLarge, ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("Loki error-log check failed for %s: %s", container, _exc_text(exc))
        return None, f"Loki error-log check skipped: {_exc_text(exc)}"
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
        data = await _pbs_get(f"/admin/datastore/{datastore}/groups")
        groups: Any = data.get("data", [])
        for group in groups:
            btype = group.get("backup-type", group.get("backup_type"))
            bid = str(group.get("backup-id", group.get("backup_id")))
            if btype == kind.lower() and bid == guest_id:
                last = group.get("last-backup", group.get("last_backup"))
                when = datetime.fromtimestamp(int(last), UTC).strftime("%Y-%m-%d %H:%M UTC")
                return f"restore from the latest PBS backup of {pbs_guest} (taken {when})"
    except Exception as exc:  # noqa: BLE001 - PBS trouble must never fail verification
        logger.warning("PBS lookup for %s failed: %s", pbs_guest, _exc_text(exc))
        return f"{fallback} (warning: PBS lookup failed: {_exc_text(exc)})"
    return f"{fallback} (warning: no backup of {pbs_guest} found in datastore {datastore})"


async def _send_failure_email(
    outcome: DeployOutcome, name: str, host: str, pbs_guest: str, detail: str, version: str | None
) -> None:
    label = "Upgrade FAILED" if outcome == "failed" else "Upgrade digest mismatch"
    subject = f"{label}: {name} on {host}"
    restore = await _latest_backup(pbs_guest)
    text = (
        f"{subject}\n\nContainer: {name}\nHost: {host}\nCandidate version: {version or 'unknown'}\n"
        f"Outcome: {outcome}\nDetail: {detail}\n\nRecommended action: {restore}.\n"
    )
    e = html.escape
    body = (
        f"<h2>{e(subject)}</h2><ul><li>Container: {e(name)}</li><li>Host: {e(host)}</li>"
        f"<li>Candidate version: {e(version or 'unknown')}</li><li>Outcome: {e(outcome)}</li>"
        f"<li>Detail: {e(detail)}</li></ul><p>Recommended action: {e(restore)}.</p>"
    )
    if not is_email_configured():
        logger.error("Email not configured; NOT sent. %s", text.replace("\n", " | "))
        return
    sent = await asyncio.to_thread(send_report_email, text, body, subject)
    if not sent:
        logger.error("Failed to send upgrade failure email. %s", text.replace("\n", " | "))


def _find_container(name: str) -> AdvisedContainer | None:
    try:
        allowlist = load_allowlist(get_settings().upgrade_advisor_config_path)
    except Exception:
        logger.exception("Cannot load the upgrade allowlist during verification")
        return None
    return next((c for c in allowlist if c.name == name), None)


async def _evaluate(
    verdict: VerdictRecord, container: AdvisedContainer, now: datetime
) -> tuple[DeployOutcome, str | None, list[str]]:
    """Run the checks in order; returns ``(outcome, running_digest, detail parts)``."""
    try:
        wud = await get_container(container)
    except WudError as exc:
        return "failed", None, [f"could not read WUD state: {clean_text(str(exc))}"]
    if wud.running_digest != verdict.candidate_digest:
        return (
            "digest_mismatch",
            wud.running_digest,
            [f"running digest {wud.running_digest} is not the approved {verdict.candidate_digest}"],
        )
    parts: list[str] = []
    failure = await _check_readiness(container.readiness_url)
    if failure:
        return "failed", wud.running_digest, [failure]
    failure, warning = await _check_logs(container.name, now)
    if warning:
        parts.append(f"warning: {warning}")
    if failure:
        return "failed", wud.running_digest, [failure, *parts]
    return "ok", wud.running_digest, ["digest matches, readiness ok, error logs quiet", *parts]


async def verify_deploy(deploy_id: int) -> DeployRecord:
    """Verify one deploy, record the final outcome and email on failure or digest mismatch."""
    conn = store.connect(get_settings().upgrade_advisor_db_path)
    try:
        deploy = store.get_deploy(conn, deploy_id)
        if deploy is None:
            msg = f"no deploy with id {deploy_id}"
            raise LookupError(msg)
        if deploy.outcome != "pending_verify":
            logger.warning("deploy %s already verified (%s); not re-verifying", deploy_id, deploy.outcome)
            return deploy
        verdict = store.get_verdict(conn, deploy.verdict_id)
        container = _find_container(verdict.container) if verdict else None
        name = verdict.container if verdict else f"verdict {deploy.verdict_id}"
        outcome: DeployOutcome
        if verdict is None or container is None:
            what = "verdict" if verdict is None else f"container {name} in the allowlist"
            outcome, digest, parts = "failed", None, [f"cannot verify: {what} can no longer be found"]
        else:
            outcome, digest, parts = await _evaluate(verdict, container, datetime.now(UTC))
        detail = "; ".join(parts)
        store.update_deploy(conn, deploy_id, outcome, digest, detail)
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
            )
        updated = store.get_deploy(conn, deploy_id)
        assert updated is not None  # noqa: S101 - just written
        return updated
    finally:
        conn.close()


async def verify_pending() -> list[DeployRecord]:
    """Verify every pending deploy older than the verify delay (also picks up ones lost to a restart)."""
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.upgrade_advisor_verify_delay_seconds)
    conn = store.connect(settings.upgrade_advisor_db_path)
    try:
        due = [d for d in store.pending_verifications(conn) if d.started_at <= cutoff]
    finally:
        conn.close()
    results: list[DeployRecord] = []
    for deploy in due:
        try:
            results.append(await verify_deploy(deploy.id))
        except Exception:
            logger.exception("Verification of deploy %s crashed; it stays pending", deploy.id)
    return results
