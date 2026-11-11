"""LangChain tool listing Prometheus scrape targets and their health."""

import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from langchain_core.tools import ToolException, tool  # pyright: ignore[reportUnknownVariableType]
from pydantic import BaseModel, Field

from src.agent.tools._common import format_age, format_utc
from src.agent.tools.prometheus import (  # pyright: ignore[reportPrivateUsage]
    DEFAULT_TIMEOUT_SECONDS,
    _get_prometheus_raw,
)
from src.config import get_settings

logger = logging.getLogger(__name__)

MAX_TARGETS_SHOWN = 100
MAX_ERROR_LENGTH = 200
MAX_JOB_LENGTH = 100
_JOB_PATTERN = r"^[A-Za-z0-9_:.-]+$"
_HEALTH_ORDER = {"down": 0, "unknown": 1, "up": 2}


class ScrapeTargetsInput(BaseModel):
    """Input for listing Prometheus scrape targets."""

    job: str | None = Field(
        default=None,
        description="Only targets of this scrape job (the `job` label), e.g. 'node_exporter'. Omit for every job.",
        max_length=MAX_JOB_LENGTH,
        pattern=_JOB_PATTERN,
    )
    only_down: bool = Field(
        default=False,
        description="List only targets whose last scrape failed (health 'down'). The summary still counts all targets.",
    )


def _when(value: object, now: datetime) -> str:
    """A scrape time as ``YYYY-MM-DD HH:MM:SSZ (age)``, or ``never`` / ``unknown`` when it cannot be read."""
    if not isinstance(value, str) or not value:
        return "never"
    try:
        scraped = datetime.fromisoformat(value)
    except ValueError:
        return "unknown"
    if scraped.year < 2000:  # Prometheus reports a target never scraped as year 1
        return "never"
    return f"{format_utc(scraped)} ({format_age(scraped, now)})"


def _line(target: dict[str, Any], now: datetime) -> str:
    labels: dict[str, str] = target.get("labels") or {}
    duration = target.get("lastScrapeDuration")
    parts = [
        f"job={labels.get('job', '?')}",
        f"instance={labels.get('instance', '?')}",
        f"hostname={labels.get('hostname', '-')}",
        f"health={target.get('health', 'unknown')}",
        f"last scrape {_when(target.get('lastScrape'), now)}",
        f"took {duration:.3f}s" if isinstance(duration, int | float) else "took ?",
    ]
    error = str(target.get("lastError") or "")
    if error:
        parts.append(f"error: {error[:MAX_ERROR_LENGTH]}")
    return " | ".join(parts)


def format_targets(targets: list[dict[str, Any]], job: str | None, only_down: bool, now: datetime) -> str:
    """Summary line plus one line per target, down first, capped at ``MAX_TARGETS_SHOWN``.

    ``targets`` are active targets already filtered to ``job``. The summary counts all of them, even
    when ``only_down`` lists only the down ones.
    """
    jobs = {(t.get("labels") or {}).get("job") for t in targets}
    down = [t for t in targets if t.get("health") == "down"]
    scope = f" for job {job}" if job else ""
    summary = f"{len(targets)} targets, {len(down)} down across {len(jobs)} jobs{scope}"
    if not targets and job:
        return f"{summary}\nNo active scrape targets have job {job!r}; check the job name."

    listed = down if only_down else sorted(targets, key=lambda t: _HEALTH_ORDER.get(str(t.get("health")), 1))
    lines = [summary]
    if only_down and not down:
        lines.append("No targets are down.")
    shown = listed[:MAX_TARGETS_SHOWN]
    if len(listed) > len(shown):
        lines.append(f"(showing {len(shown)} of {len(listed)})")
    lines.extend(_line(t, now) for t in shown)
    return "\n".join(lines)


TOOL_DESCRIPTION = (
    "List Prometheus scrape targets and whether Prometheus can scrape them. Use this to find exporters "
    "that are down or failing (a 'Scrape target down' alert, missing metrics for a host) and read the "
    "scrape error. Down targets come first, each with job, instance, hostname, last scrape time, "
    "duration and last error. Filter with `job`, or set `only_down` to list failing targets only."
)


@tool("scrape_targets", args_schema=ScrapeTargetsInput)  # pyright: ignore[reportUnknownParameterType]
async def scrape_targets(job: str | None = None, only_down: bool = False) -> str:
    """List Prometheus scrape targets and their health. See TOOL_DESCRIPTION."""
    # The tool framework validates too; a direct call must fail the same way, before any request.
    if job is not None and (len(job) > MAX_JOB_LENGTH or not re.fullmatch(r"[A-Za-z0-9_:.-]+", job)):
        raise ToolException(
            f"Invalid job {job!r}: use letters, digits and _ : . - only, at most {MAX_JOB_LENGTH} characters"
        )

    params = {"state": "active"}
    if job:
        params["scrapePool"] = job
    logger.info("Prometheus scrape targets: job=%s only_down=%s", job, only_down)
    try:
        payload = await _get_prometheus_raw("/api/v1/targets", params=params)
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to Prometheus at {get_settings().prometheus_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"Prometheus targets request timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e
    except httpx.HTTPStatusError as e:
        raise ToolException(f"Prometheus API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e

    if payload.get("status") != "success":
        raise ToolException(f"Prometheus targets query failed: {payload.get('error', 'unexpected response')}")
    active: list[dict[str, Any]] = (payload.get("data") or {}).get("activeTargets") or []
    if job:
        active = [t for t in active if (t.get("labels") or {}).get("job") == job]
    return format_targets(active, job, only_down, datetime.now(UTC))


scrape_targets.description = TOOL_DESCRIPTION
scrape_targets.handle_tool_error = True
