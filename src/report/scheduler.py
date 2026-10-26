"""APScheduler integration for scheduled report generation.

Uses AsyncIOScheduler with CronTrigger to run weekly reports on a
configurable schedule.  No-ops gracefully if no cron expression is configured.
"""

import asyncio
import contextlib
import logging
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from src.config import get_settings
from src.observability.metrics import REPORT_DURATION, REPORTS_TOTAL, UPGRADE_LAST_SUCCESS, UPGRADE_RUNS_TOTAL
from src.report.email import is_email_configured, send_report_email
from src.report.generator import generate_report

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


_MAX_NARRATIVE_RETRY_SECONDS = 21_600  # 6 hours


async def _scheduled_report_job() -> None:
    """Async job executed by the scheduler: generate report, email, record metrics."""
    start = time.monotonic()
    try:
        result = await generate_report(
            max_narrative_retry_seconds=_MAX_NARRATIVE_RETRY_SECONDS,
        )
        if is_email_configured():
            emailed = await asyncio.to_thread(send_report_email, result.markdown, result.html)
            if emailed:
                logger.info("Scheduled report emailed successfully")
            else:
                logger.warning("Scheduled report generated but email failed")
        else:
            logger.info("Scheduled report generated (email not configured)")

        duration = time.monotonic() - start
        REPORTS_TOTAL.labels(trigger="scheduled", status="success").inc()
        REPORT_DURATION.observe(duration)
    except Exception:
        duration = time.monotonic() - start
        REPORTS_TOTAL.labels(trigger="scheduled", status="error").inc()
        REPORT_DURATION.observe(duration)
        logger.exception("Scheduled report generation failed")


async def _scheduled_advisor_job() -> None:
    """Run the Upgrade Advisor; an unexpected exception is logged and counted, never raised into the scheduler."""
    from src.upgrades.job import run_advisor  # lazy: the weekly report path must not import the advisor stack

    try:
        await run_advisor()
    except Exception:
        UPGRADE_RUNS_TOTAL.labels(status="error").inc()
        logger.exception("Scheduled upgrade advisor run failed")


def _seed_advisor_last_success(db_path: str) -> None:
    """Set the advisor's last-success gauge from the store, so a restart does not reset it to 0 (and page)."""
    from src.upgrades import store  # lazy, see _scheduled_advisor_job

    try:
        conn = store.connect(db_path)
        try:
            last = store.get_last_success(conn)
        finally:
            conn.close()
    except Exception:
        logger.exception("Could not read the advisor's last successful run time; the gauge starts at 0")
        return
    if last is not None:
        UPGRADE_LAST_SUCCESS.set(last.timestamp())


def start_scheduler() -> None:
    """Start the APScheduler if the report and/or advisor cron expression is configured."""
    global _scheduler  # noqa: PLW0603

    from src.upgrades.config import is_advisor_configured  # lazy, see _scheduled_advisor_job

    settings = get_settings()
    if is_advisor_configured():
        _seed_advisor_last_success(settings.upgrade_advisor_db_path)
    advisor_cron = settings.upgrade_advisor_cron
    advisor_enabled = bool(advisor_cron) and is_advisor_configured()
    if advisor_cron and not advisor_enabled:
        logger.warning("UPGRADE_ADVISOR_CRON is set but the advisor is not fully configured; job not scheduled")
    if not settings.report_schedule_cron and not advisor_enabled:
        logger.info("Scheduler disabled (REPORT_SCHEDULE_CRON and UPGRADE_ADVISOR_CRON not usable)")
        return

    _scheduler = AsyncIOScheduler()
    if settings.report_schedule_cron:
        _scheduler.add_job(
            _scheduled_report_job,
            trigger=CronTrigger.from_crontab(settings.report_schedule_cron),
            id="weekly_report",
            name="Weekly Reliability Report",
            replace_existing=True,
        )
        logger.info("Report job scheduled with cron: %s", settings.report_schedule_cron)
    if advisor_enabled:
        _scheduler.add_job(
            _scheduled_advisor_job,
            trigger=CronTrigger.from_crontab(advisor_cron, timezone=settings.user_timezone),
            id="upgrade_advisor",
            name="Upgrade Advisor",
            replace_existing=True,
        )
        logger.info("Upgrade advisor job scheduled with cron: %s", advisor_cron)
    _scheduler.start()


def stop_scheduler() -> None:
    """Gracefully shut down the scheduler if it is running."""
    global _scheduler  # noqa: PLW0603

    if _scheduler is not None:
        with contextlib.suppress(Exception):
            _scheduler.shutdown(wait=False)
        logger.info("Report scheduler stopped")
        _scheduler = None
