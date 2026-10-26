"""The daily Upgrade Advisor run: find pending updates, review them, record verdicts, batch and email."""

import asyncio
import logging
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from pydantic import BaseModel

from src.config import get_settings
from src.observability.metrics import UPGRADE_LAST_SUCCESS, UPGRADE_RUNS_TOTAL, UPGRADE_VERDICTS_TOTAL
from src.report.email import is_email_configured, send_report_email
from src.upgrades import email as upgrade_email
from src.upgrades import releases, review, store, verify, wud
from src.upgrades.config import AdvisedContainer, load_allowlist
from src.upgrades.models import DeployRecord, Verdict
from src.upgrades.releases import Release
from src.upgrades.review import Candidate, Review

logger = logging.getLogger(__name__)

BATCH_TTL = timedelta(hours=48)
NON_DIGEST_HOLD = "tag change offered by WUD; digest-only policy"


class ItemResult(BaseModel):
    """What happened to one container in a run (containers with nothing pending are not listed)."""

    container: str
    verdict: Verdict | None = None
    review: Review | None = None
    hold_reason: str | None = None
    error: str | None = None
    verdict_id: int | None = None


class RunSummary(BaseModel):
    """Everything one run produced."""

    items: list[ItemResult] = []
    batch_id: str | None = None
    approve_url: str | None = None
    outcomes: list[DeployRecord] = []
    outcome_containers: dict[int, str] = {}  # deploy id -> container name
    aborted: str | None = None  # class name (or EmailNotSent) when the run aborted or a required email was not sent


async def _send(subject: str, markdown: str, html_body: str) -> bool:
    """Send the email; ``False`` (logged at error level) when it was not configured or not sent."""
    if not is_email_configured():
        logger.error("Upgrade advisor email not configured; NOT sent: %s", subject)
        return False
    if not await asyncio.to_thread(send_report_email, markdown, html_body, subject):
        logger.error("Upgrade advisor email failed to send: %s", subject)
        return False
    return True


class _ReleaseCache:
    """One ``list_releases`` call per GitHub repo per run (a failure is remembered and re-raised)."""

    def __init__(self) -> None:
        self._ok: dict[str, list[Release]] = {}
        self._failed: dict[str, Exception] = {}

    async def get(self, github_repo: str) -> list[Release]:
        if github_repo in self._failed:
            raise self._failed[github_repo]
        if github_repo not in self._ok:
            try:
                self._ok[github_repo] = await releases.list_releases(github_repo)
            except Exception as exc:
                self._failed[github_repo] = exc
                raise
        return self._ok[github_repo]


async def _review_one(
    conn: sqlite3.Connection,
    c: AdvisedContainer,
    now: datetime,
    min_age: timedelta,
    cache: _ReleaseCache,
    *,
    persist: bool = True,
) -> ItemResult | None:
    """Review one container; ``None`` when nothing is pending. With ``persist=False`` (dry run) no verdict row is
    saved: the newest verdict of a container supersedes older approval links, so a dry run must not create one."""
    record = await wud.get_container(c)
    if not record.update_available:
        return None
    if not record.candidate_digest or not record.running_digest:
        logger.error("WUD reported an update without digests for %s", c.name)
        return ItemResult(container=c.name, error="WUD reported an update without digests")
    if record.candidate_digest == record.running_digest:
        return None
    running: str = record.running_digest
    candidate: str = record.candidate_digest

    def save(
        verdict: Verdict,
        summary: str,
        details: dict[str, object],
        running_version: str | None,
        candidate_version: str | None,
    ) -> int | None:
        if not persist:
            return None
        return store.save_verdict(
            conn,
            container=c.name,
            running_digest=running,
            candidate_digest=candidate,
            running_version=running_version,
            candidate_version=candidate_version,
            verdict=verdict,
            summary=summary,
            details=details,
            now=now,
        )

    def hold(reason: str, running_version: str | None = None, candidate_version: str | None = None) -> ItemResult:
        vid = save("hold", reason, {}, running_version, candidate_version)
        return ItemResult(container=c.name, verdict="hold", hold_reason=reason, verdict_id=vid)

    if record.update_kind != "digest":
        return hold(NON_DIGEST_HOLD)

    first_seen_at = store.first_seen(conn, c.name, candidate, now)
    all_releases = await cache.get(c.github_repo)
    running_release = await releases.release_for_digest(c, running, all_releases)
    candidate_release = await releases.release_for_digest(c, candidate, all_releases)
    notes = (
        releases.releases_between(all_releases, running_release.version, candidate_release.version)
        if running_release and candidate_release
        else []
    )
    issues = (
        await releases.regression_issues(c.github_repo, candidate_release.tag, candidate_release.published_at)
        if candidate_release
        else []
    )
    cand = Candidate(
        container=c,
        wud_id=record.id,
        running_digest=running,
        candidate_digest=candidate,
        running_release=running_release,
        candidate_release=candidate_release,
        first_seen_at=first_seen_at,
        notes=notes,
        issues=issues,
    )
    running_version = running_release.tag if running_release else None
    candidate_version = candidate_release.tag if candidate_release else None
    reason = review.gate(cand, now, min_age)
    if reason is not None:
        return hold(reason, running_version, candidate_version)
    result = await review.review(cand)
    vid = save(result.verdict, result.summary, result.model_dump(mode="json"), running_version, candidate_version)
    return ItemResult(
        container=c.name,
        verdict=result.verdict,
        review=result,
        hold_reason=result.summary if result.verdict == "hold" else None,
        verdict_id=vid,
    )


def _persist_last_success(db_path: str, at: datetime) -> None:
    """Store the success time so a restart can seed the gauge; failure is logged, the run still counts."""
    try:
        conn = store.connect(db_path)
        try:
            store.set_last_success(conn, at)
        finally:
            conn.close()
    except Exception:
        logger.exception("Could not persist the advisor's last successful run time")


def _abort_metric() -> None:
    UPGRADE_RUNS_TOTAL.labels(status="error").inc()


async def run_advisor(now: datetime | None = None, send_email: bool = True) -> RunSummary:  # noqa: FBT001, FBT002
    """Run the advisor once. With ``send_email=False`` (dry run) nothing is sent, no verdict row, batch or token is
    created (first-seen times are still recorded) and pending deploy verifications are left alone."""
    started = time.monotonic()
    settings = get_settings()
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        msg = "now must be timezone-aware"
        raise ValueError(msg)
    try:
        containers = load_allowlist(settings.upgrade_advisor_config_path)
    except Exception as exc:
        kind = type(exc).__name__
        logger.exception("Upgrade advisor allowlist could not be loaded: %s", kind)
        _abort_metric()
        if send_email:
            await _send(*upgrade_email.render_abort(kind))
        return RunSummary(aborted=kind)

    items: list[ItemResult] = []
    if send_email:
        try:
            await verify.verify_pending()
        except Exception as exc:
            logger.exception("verify_pending failed; continuing with the review")
            items.append(ItemResult(container="deploy verification", error=type(exc).__name__))

    min_age = timedelta(hours=settings.upgrade_advisor_min_age_hours)
    cache = _ReleaseCache()
    conn = store.connect(settings.upgrade_advisor_db_path)
    try:
        for c in containers:
            try:
                item = await _review_one(conn, c, now, min_age, cache, persist=send_email)
            except Exception as exc:
                logger.exception("Upgrade review of %s failed", c.name)
                items.append(ItemResult(container=c.name, error=type(exc).__name__))
                continue
            if item is not None:
                items.append(item)
                if item.verdict:
                    UPGRADE_VERDICTS_TOTAL.labels(verdict=item.verdict).inc()

        summary = RunSummary(items=items)
        batchable = [i.verdict_id for i in items if i.verdict in {"upgrade", "needs_you"} and i.verdict_id is not None]
        if batchable and send_email:
            batch_id, token = store.create_batch(conn, batchable, now, BATCH_TTL)
            base = settings.upgrade_advisor_public_base_url.rstrip("/")
            summary.batch_id = batch_id
            # The token goes in the fragment: browsers never send it, so it never reaches a request log.
            summary.approve_url = f"{base}/upgrades/batches/{batch_id}#token={quote(token, safe='')}"
        summary.outcomes = store.deploys_since(conn, now - timedelta(hours=24))
        for deploy in summary.outcomes:
            verdict = store.get_verdict(conn, deploy.verdict_id)
            if verdict:
                summary.outcome_containers[deploy.id] = verdict.container
    finally:
        conn.close()

    if send_email and (summary.items or summary.outcomes) and not await _send(*upgrade_email.render(summary)):
        _abort_metric()
        summary.aborted = "EmailNotSent"
        return summary
    UPGRADE_RUNS_TOTAL.labels(status="success").inc()
    finished = datetime.now(UTC)
    UPGRADE_LAST_SUCCESS.set(finished.timestamp())
    if send_email:  # a dry run is not a run the staleness alert should count after a restart
        _persist_last_success(settings.upgrade_advisor_db_path, finished)
    logger.info("Upgrade advisor run finished in %.1fs: %d items", time.monotonic() - started, len(items))
    return summary
