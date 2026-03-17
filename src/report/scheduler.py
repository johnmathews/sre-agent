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
from src.observability.metrics import REPORT_DURATION, REPORTS_TOTAL
from src.report.email import is_email_configured, send_report_email
from src.report.generator import generate_report

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _scheduled_report_job() -> None:
    """Async job executed by the scheduler: generate report, email, record metrics."""
    start = time.monotonic()
    try:
        result = await generate_report()
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


def start_scheduler() -> None:
    """Start the APScheduler if a cron expression is configured."""
    global _scheduler  # noqa: PLW0603

    settings = get_settings()
    if not settings.report_schedule_cron:
        logger.info("Report scheduler disabled (REPORT_SCHEDULE_CRON not set)")
        return

    trigger = CronTrigger.from_crontab(settings.report_schedule_cron)
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _scheduled_report_job,
        trigger=trigger,
        id="weekly_report",
        name="Weekly Reliability Report",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("Report scheduler started with cron: %s", settings.report_schedule_cron)


def stop_scheduler() -> None:
    """Gracefully shut down the scheduler if it is running."""
    global _scheduler  # noqa: PLW0603

    if _scheduler is not None:
        with contextlib.suppress(Exception):
            _scheduler.shutdown(wait=False)
        logger.info("Report scheduler stopped")
        _scheduler = None
