"""LangChain tool that reports what the Upgrade Advisor decided about container image upgrades.

Two read-only sources, fetched concurrently:

- The Upgrade Advisor's SQLite store, opened read-only (``store.connect_readonly``): the latest verdict per
  container, the deploys of that verdict, other deploys in the last ``days``, and the last successful run.
- The container-status exporter in Prometheus: ``container_image_outdated`` and ``container_image_info``.

Deploy safety: this module imports only ``store`` (and its models) from the advisor package, never the code that
deploys, so the deploy capability stays out of the agent and MCP surfaces.
"""

import asyncio
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from langchain_core.tools import ToolException, tool  # pyright: ignore[reportUnknownVariableType]
from pydantic import BaseModel, Field

from src.agent.tools import HOMELAB_CONTEXT
from src.agent.tools._common import format_age, format_utc, promql_string
from src.agent.tools.prometheus import _query_prometheus  # pyright: ignore[reportPrivateUsage]
from src.config import get_settings
from src.upgrades import store
from src.upgrades.models import DeployRecord, VerdictRecord

logger = logging.getLogger(__name__)

CONTAINER_PATTERN = r"^[A-Za-z0-9_.-]+$"
MAX_VERDICTS_SHOWN = 50
MAX_OTHER_DEPLOYS_SHOWN = 50
MAX_SUMMARY_CHARS = 300
MAX_DETAIL_CHARS = 200


class UpgradeStatusInput(BaseModel):
    """Input for the upgrade status tool."""

    container: str | None = Field(
        default=None,
        description=(
            "Docker container name, e.g. 'homepage' (letters, digits, '_', '.', '-'). "
            "Omit it for every container the advisor has reviewed and every outdated image."
        ),
        max_length=128,
        pattern=CONTAINER_PATTERN,
    )
    days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="How many days back to list deploys (1-365). The latest verdict's own deploys always show.",
    )


# --- Upgrade Advisor store ---


@dataclass
class AdvisorView:
    """What the advisor store holds for the request, read in one worker thread."""

    last_success: datetime | None
    verdicts: list[VerdictRecord]
    total_verdicts: int
    deploys_by_verdict: dict[int, list[DeployRecord]]
    other_deploys: list[tuple[DeployRecord, VerdictRecord | None]] = field(default_factory=list)


def read_advisor(path: str, container: str | None, since: datetime) -> AdvisorView:
    """Read the advisor store read-only. Raises ``FileNotFoundError`` or ``sqlite3.Error``; runs in a thread."""
    if not Path(path).is_file():
        raise FileNotFoundError(f"file not found: {path}")
    conn = store.connect_readonly(path)
    try:
        if container is None:
            verdicts = store.latest_verdicts(conn)
        else:
            latest = store.latest_verdict_for_container(conn, container)
            verdicts = [latest] if latest else []
        total = len(verdicts)
        verdicts = verdicts[:MAX_VERDICTS_SHOWN]
        shown_ids = [v.id for v in verdicts]
        deploys_by_verdict: dict[int, list[DeployRecord]] = {vid: [] for vid in shown_ids}
        for deploy in store.deploys_for_verdicts(conn, shown_ids):
            deploys_by_verdict[deploy.verdict_id].append(deploy)

        verdict_cache: dict[int, VerdictRecord | None] = {v.id: v for v in verdicts}
        other: list[tuple[DeployRecord, VerdictRecord | None]] = []
        for deploy in store.deploys_since(conn, since):
            if deploy.verdict_id in deploys_by_verdict:
                continue  # already listed under its verdict
            if deploy.verdict_id not in verdict_cache:
                verdict_cache[deploy.verdict_id] = store.get_verdict(conn, deploy.verdict_id)
            verdict = verdict_cache[deploy.verdict_id]
            if container is not None and (verdict is None or verdict.container != container):
                continue
            other.append((deploy, verdict))
        return AdvisorView(
            last_success=store.get_last_success(conn),
            verdicts=verdicts,
            total_verdicts=total,
            deploys_by_verdict=deploys_by_verdict,
            other_deploys=other,
        )
    finally:
        conn.close()


def _cut(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _versions(running: str | None, candidate: str | None) -> str:
    return f"{running or '?'} -> {candidate or '?'}"


def _deploy_text(deploy: DeployRecord) -> str:
    trigger = f" (trigger {deploy.trigger_state})" if deploy.trigger_state else ""
    detail = f": {_cut(deploy.detail, MAX_DETAIL_CHARS)}" if deploy.detail.strip() else ""
    return f"{deploy.outcome}{trigger}{detail}"


def format_advisor(view: AdvisorView, container: str | None, days: int, now: datetime) -> list[str]:
    """The advisor part of the answer: last run, latest verdicts with their deploys, other recent deploys."""
    last = (
        f"{format_utc(view.last_success)} ({format_age(view.last_success, now)})"
        if view.last_success
        else "none recorded"
    )
    lines = [f"Upgrade Advisor: last successful run {last}"]
    if not view.verdicts:
        if container is not None:
            lines.append(f"  No Upgrade Advisor verdict for {container} (not reviewed yet, or not on its allowlist)")
        else:
            lines.append("  No verdicts recorded yet")
    else:
        heading = "Latest verdict:" if container is not None else f"Latest verdicts ({view.total_verdicts} containers):"
        if view.total_verdicts > len(view.verdicts):
            heading += f" showing {len(view.verdicts)} of {view.total_verdicts}"
        lines.append(heading)
        for v in view.verdicts:
            lines.append(
                f"  {v.container}: {v.verdict} ({_versions(v.running_version, v.candidate_version)}), "
                + f"reviewed {format_utc(v.created_at)} ({format_age(v.created_at, now)}): "
                + _cut(v.summary, MAX_SUMMARY_CHARS)
            )
            deploys = view.deploys_by_verdict.get(v.id, [])
            if not deploys:
                lines.append("    not deployed")
            for deploy in deploys:
                lines.append(f"    deploy {format_utc(deploy.started_at)}: {_deploy_text(deploy)}")

    other = view.other_deploys
    lines.append(f"Other deploys in the last {days} days ({len(other)}):")
    if not other:
        lines.append("  none")
    for deploy, verdict in other[-MAX_OTHER_DEPLOYS_SHOWN:]:
        if verdict is None:
            what = f"verdict {deploy.verdict_id} (unreadable)"
        else:
            what = f"{verdict.container} {_versions(verdict.running_version, verdict.candidate_version)}"
        lines.append(f"  {format_utc(deploy.started_at)} {what}: {_deploy_text(deploy)}")
    if len(other) > MAX_OTHER_DEPLOYS_SHOWN:
        lines.append(f"  (showing the newest {MAX_OTHER_DEPLOYS_SHOWN} of {len(other)})")
    return lines


async def _advisor_lines(container: str | None, days: int, now: datetime) -> tuple[list[str], bool]:
    """The advisor part and whether it answered; a missing or unreadable store is a visible line, not a crash."""
    path = get_settings().upgrade_advisor_db_path
    if not path:
        return [
            "Upgrade Advisor not configured (UPGRADE_ADVISOR_DB_PATH is empty): no verdicts or deploy history"
        ], False
    try:
        view = await asyncio.to_thread(read_advisor, path, container, now - timedelta(days=days))
    except (OSError, sqlite3.Error) as e:
        reason = str(e) if isinstance(e, FileNotFoundError) else f"{type(e).__name__}: {e}"
        logger.warning("upgrade_status: advisor DB unavailable: %s", reason)
        return [f"Upgrade Advisor DB unavailable: {reason}"], False
    return format_advisor(view, container, days, now), True


# --- container-status exporter ---


@dataclass(frozen=True)
class ExporterImage:
    """One container's image freshness from the exporter, joined on (real host, container)."""

    host: str
    container: str
    outdated: bool | None
    image: str
    status: str
    current_version: str
    available_version: str
    base_image: str


def _key(metric: dict[str, str]) -> tuple[str, str]:
    # The exporter's own host is in `hostname`; Prometheus moves the container's host to `exported_hostname`.
    return metric.get("exported_hostname") or metric.get("hostname", "?"), metric.get("container_name", "?")


def exporter_images(outdated: Sequence[dict[str, Any]], info: Sequence[dict[str, Any]]) -> list[ExporterImage]:
    """Join ``container_image_outdated`` and ``container_image_info`` series, sorted by container then host."""
    flags: dict[tuple[str, str], bool] = {}
    for series in outdated:
        value = series.get("value", [0, ""])
        flags[_key(series.get("metric", {}))] = str(value[1] if len(value) > 1 else "") == "1"
    labels: dict[tuple[str, str], dict[str, str]] = {_key(s.get("metric", {})): s.get("metric", {}) for s in info}
    images: list[ExporterImage] = []
    for key in sorted(set(flags) | set(labels), key=lambda k: (k[1], k[0])):
        metric = labels.get(key, {})
        images.append(
            ExporterImage(
                host=key[0],
                container=key[1],
                outdated=flags.get(key),
                image=metric.get("image", ""),
                status=metric.get("status", ""),
                current_version=metric.get("current_version", ""),
                available_version=metric.get("available_version", ""),
                base_image=metric.get("base_image", ""),
            )
        )
    return images


def format_exporter_lines(images: Sequence[ExporterImage], *, only_outdated: bool) -> list[str]:
    """One line per image; ``only_outdated`` keeps the ones with a newer image available."""
    lines: list[str] = []
    for img in images:
        if only_outdated and img.outdated is not True:
            continue
        if img.outdated is True:
            flag = f"newer image available ({_versions(img.current_version, img.available_version)})"
        elif img.outdated is False:
            flag = "up to date" + (f" ({img.current_version})" if img.current_version else "")
        else:
            flag = "freshness unknown"
        extras = [
            f"{label} {value}"
            for label, value in (("image", img.image), ("status", img.status), ("base image", img.base_image))
            if value
        ]
        lines.append(f"  {img.container} on {img.host}: {', '.join([flag, *extras])}")
    return lines


async def _vector(query: str) -> list[dict[str, Any]]:
    data = await _query_prometheus("/api/v1/query", {"query": query})
    if data.get("status") != "success":
        raise ValueError(f"Prometheus error: {data.get('error', 'unknown error')}")
    return [dict(series) for series in data.get("data", {}).get("result", [])]


def _describe_http(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, ValueError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


async def _exporter_lines(container: str | None) -> tuple[list[str], bool]:
    """The exporter part and whether it answered; a Prometheus failure is one ``unavailable`` line."""
    heading = "Image freshness (container-status exporter)"
    selector = "{container_name=" + promql_string(container) + "}" if container else ""
    try:
        outdated, info = await asyncio.gather(
            _vector(f"container_image_outdated{selector}"), _vector(f"container_image_info{selector}")
        )
    except (httpx.HTTPError, ValueError) as e:
        reason = _describe_http(e)
        logger.warning("upgrade_status: exporter data unavailable: %s", reason)
        return [f"{heading}: unavailable: {reason}"], False
    images = exporter_images(outdated, info)
    if container is not None:
        if not images:
            return [f"{heading}: no exporter series for container_name={promql_string(container)}"], True
        return [f"{heading}:", *format_exporter_lines(images, only_outdated=False)], True
    stale = sum(1 for img in images if img.outdated is True)
    if stale == 0:
        return [f"{heading}: all {len(images)} containers up to date"], True
    summary = f"{heading}: {stale} of {len(images)} containers have a newer image"
    return [summary, *format_exporter_lines(images, only_outdated=True)], True


TOOL_DESCRIPTION = HOMELAB_CONTEXT + (
    "Report what the Upgrade Advisor decided about container image upgrades, and which images are outdated. "
    "Use this for questions like 'should I upgrade homepage?', 'what did the upgrade advisor say about sonarr?', "
    "'which containers have a newer image?' or 'what was deployed this week?'.\n\n"
    "Shows the advisor's last successful run; the latest verdict per container (upgrade, hold or needs_you, "
    "with versions, review time and its summary) with that verdict's deploys; other deploys in the last `days`; "
    "and the container-status exporter's image freshness (outdated flag, current and available versions). "
    "With `container`, only that container; without it, every reviewed container and only the outdated images.\n\n"
    "Read-only: it never approves or deploys. Upgrades are approved through the advisor's emailed link. "
    "A source that fails prints 'unavailable: reason' and the rest still shows."
)


@tool("upgrade_status", args_schema=UpgradeStatusInput)  # pyright: ignore[reportUnknownParameterType]
async def upgrade_status(container: str | None = None, days: int = 30) -> str:
    """Upgrade Advisor verdicts, deploys and image freshness. See TOOL_DESCRIPTION."""
    now = datetime.now(UTC)
    logger.info("Upgrade status: container=%s days=%d", container or "*", days)
    (advisor, advisor_ok), (exporter, exporter_ok) = await asyncio.gather(
        _advisor_lines(container, days, now), _exporter_lines(container)
    )
    if not advisor_ok and not exporter_ok:
        raise ToolException("No upgrade data: " + "; ".join([*advisor, *exporter]))
    scope = container or "all containers"
    header = f"Upgrade status for {scope} (times UTC; deploys from the last {days} days)"
    return "\n".join([header, "", *advisor, "", *exporter])


upgrade_status.description = TOOL_DESCRIPTION
upgrade_status.handle_tool_error = True
