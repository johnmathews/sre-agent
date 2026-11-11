"""One verdict on a named service, from five sources run concurrently.

Sources: Gatus endpoint results, the container-status exporter (through ``collect_containers``),
blackbox probes, active Grafana alerts and the Loki error count. Each reports "ok", "no match" or
"unavailable: reason". The matching and verdict rules are fixed (see ``docs/tool-reference.md``);
keep them small, or this tool grows into a second incident explainer.
"""

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Literal

import httpx
from langchain_core.tools import ToolException, tool  # pyright: ignore[reportUnknownVariableType]
from pydantic import BaseModel, Field

from src.agent.tools import grafana_alerts, loki, prometheus
from src.agent.tools._common import ERROR_LEVELS, parse_duration, promql_string, regex_literal
from src.agent.tools.containers import SOURCE_LABELS as CONTAINER_SOURCE_LABELS
from src.agent.tools.containers import ContainerReport, Sample, collect_containers
from src.agent.tools.grafana_alerts import GrafanaAlert, GrafanaAlertGroup
from src.config import get_settings

logger = logging.getLogger(__name__)

MIN_WINDOW_SECONDS = 300
MAX_WINDOW_SECONDS = 7 * 86400
WINDOW_RANGE = "5m to 7d"

GATUS_SUCCESS = "gatus_results_endpoint_success"
PROBE_SUCCESS = "probe_success"

type Status = Literal["ok", "no match", "unavailable"]
type Verdict = Literal["DOWN", "DEGRADED", "UP", "UNKNOWN"]

SOURCE_NAMES: dict[str, str] = {
    "gatus": "Gatus",
    "containers": "Containers",
    "blackbox": "Blackbox probes",
    "grafana": "Grafana alerts",
    "loki": "Loki errors",
}
"""Each source's key and its name in the output, in output order."""


class ServiceHealthInput(BaseModel):
    """Input for service_health."""

    name: str = Field(
        description=(
            "The service to check, e.g. 'jellyfin' or 'Home Assistant'. Matched against Gatus endpoint names, "
            "container names, probed URLs, active Grafana alerts and Loki service names."
        ),
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9 ._()-]+$",
    )
    window: str = Field(
        default="1h",
        description=(
            f"How far back the Gatus success ratio and the Loki error count look, from {WINDOW_RANGE}, "
            "e.g. '30m', '1h', '24h', '7d'. Default '1h'."
        ),
    )


@dataclass
class SourceResult:
    """What one source found.

    ``detail`` follows "ok: " or "unavailable: " in the output (for "no match", an optional note).
    ``down`` and ``degraded`` are the reasons this source gives for those verdicts.
    """

    status: Status
    detail: str = ""
    down: list[str] = field(default_factory=lambda: [])
    degraded: list[str] = field(default_factory=lambda: [])

    def text(self) -> str:
        if self.status == "no match":
            return f"no match ({self.detail})" if self.detail else "no match"
        return f"{self.status}: {self.detail}"


class _SourceError(Exception):
    """A source answered, but not with a usable result. The message is safe to show."""


# Failures one source can hit at runtime; anything else is a bug and propagates.
# ValueError: a body that is not JSON. ToolException: the Grafana helper's own HTTP errors.
_EXPECTED_ERRORS: tuple[type[Exception], ...] = (httpx.HTTPError, ValueError, ToolException, _SourceError)


def window_seconds(window: str) -> int:
    """The window in whole seconds; raises ``ToolException`` naming the accepted range."""
    try:
        seconds = parse_duration(window)
    except ValueError:
        raise ToolException(
            f"Invalid window {window!r}: use a duration from {WINDOW_RANGE}, e.g. '30m', '1h', '24h', '7d'."
        ) from None
    if not MIN_WINDOW_SECONDS <= seconds <= MAX_WINDOW_SECONDS:
        raise ToolException(f"Window {window!r} is out of range: use a duration from {WINDOW_RANGE}.")
    return int(seconds)


def _vector_samples(data: object, system: str) -> list[Sample]:
    """The samples of a Prometheus or Loki instant-vector response; raises ``_SourceError`` otherwise."""
    if not isinstance(data, dict):
        raise _SourceError(f"{system} returned an unexpected response")
    if data.get("status") != "success":  # pyright: ignore[reportUnknownMemberType]
        raise _SourceError(f"{system} error: {data.get('error', 'unknown error')}")  # pyright: ignore[reportUnknownMemberType]
    try:
        result = data["data"]["result"]
        return [({str(k): str(v) for k, v in item["metric"].items()}, float(item["value"][1])) for item in result]
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise _SourceError(f"{system} returned an unexpected instant-vector shape") from None


def _reason(exc: BaseException) -> str:
    """A one-line reason for a failed source."""
    if isinstance(exc, httpx.ConnectError):
        return f"cannot connect: {exc}"
    if isinstance(exc, httpx.TimeoutException):
        return "request timed out"
    if isinstance(exc, httpx.HTTPStatusError):
        body = " ".join(exc.response.text.split())[:300]
        return f"HTTP {exc.response.status_code} - {body}"
    if isinstance(exc, httpx.HTTPError):
        return f"request failed ({type(exc).__name__}: {exc})"
    if isinstance(exc, ToolException | _SourceError):
        return " ".join(str(exc).split())[:300]
    if isinstance(exc, ValueError):
        return "response body is not JSON"
    return f"{type(exc).__name__}: {exc}"  # pragma: no cover - unexpected types propagate before this


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


# --- Gatus ---


def _gatus_matches(labels: dict[str, str], needle: str) -> bool:
    """``needle`` (lower case) equals the endpoint name, or its key without the group prefix.

    Gatus builds ``key`` as ``<sanitised group>_<sanitised name>`` (lower case, separators as ``-``; see
    ``src/incidents/gatus.py``), so the raw ``group`` label is not the prefix. A sanitised group has no ``_``, so
    the bare key is everything after the first one (``_home-assistant`` for an empty group).
    """
    key = labels.get("key", "")
    bare_key = key.partition("_")[2] if "_" in key else key
    return needle in (labels.get("name", "").lower(), bare_key.lower())


def gatus_source(now: list[Sample], ratio: list[Sample], name: str, window: str) -> SourceResult:
    """Match Gatus endpoints; failing now is DOWN, a success ratio below 1 over the window is DEGRADED."""
    needle = name.strip().lower()
    current = {labels.get("key", ""): (labels, value) for labels, value in now if _gatus_matches(labels, needle)}
    ratios = {labels.get("key", ""): (labels, value) for labels, value in ratio if _gatus_matches(labels, needle)}
    if not current and not ratios:
        return SourceResult("no match")

    result = SourceResult("ok")
    parts: list[str] = []
    for key in sorted(current.keys() | ratios.keys()):
        labels = (current.get(key) or ratios[key])[0]
        endpoint = labels.get("name") or key
        facts: list[str] = []
        if key in current:
            succeeding = current[key][1] >= 1
            facts.append("succeeding now" if succeeding else "FAILING now")
            if not succeeding:
                result.down.append(f"Gatus endpoint {endpoint} is failing now")
        else:
            facts.append("no current result")
        if key in ratios and math.isfinite(ratios[key][1]):
            share = ratios[key][1]
            facts.append(f"{share:.1%} of checks succeeded over {window}")
            if share < 1:
                result.degraded.append(f"Gatus endpoint {endpoint} succeeded {share:.1%} of checks over {window}")
        parts.append(f"{endpoint} ({key}): {', '.join(facts)}")
    result.detail = "; ".join(parts)
    return result


# --- Containers ---


def container_source(report: ContainerReport, window: str) -> SourceResult:
    """Matched containers; not running is DOWN, unhealthy or restarted in the window is DEGRADED.

    The source is unavailable only when ``container_state`` failed. Other failed queries are noted
    in the line and the containers still decide; a fact whose query failed is simply unknown.
    """
    if not report.state_available:
        reasons = "; ".join(f"{CONTAINER_SOURCE_LABELS[key]}: {why}" for key, why in report.unavailable.items())
        return SourceResult("unavailable", reasons)
    notes = [f"{CONTAINER_SOURCE_LABELS[key]} unavailable: {why}" for key, why in report.unavailable.items()]
    if not report.containers:
        return SourceResult("no match", "; ".join(notes))

    result = SourceResult("ok")
    parts: list[str] = []
    for record in report.containers:
        where = f"{record.host}/{record.name}"
        facts = [record.state]
        if record.health:
            facts.append(record.health)
        if record.restarts_24h is not None:
            facts.append(f"{_plural(record.restarts_24h, 'restart')} in {window}")
        parts.append(f"{where} {', '.join(facts)}")
        if record.state != "running":
            result.down.append(f"container {where} is {record.state}")
        if record.health == "unhealthy":
            result.degraded.append(f"container {where} is unhealthy")
        if record.restarts_24h:
            result.degraded.append(f"container {where} restarted {_plural(record.restarts_24h, 'time')} in {window}")
    if report.exporter.stale:
        parts.append(f"exporter STALE ({'; '.join(report.exporter.reasons)})")
    result.detail = "; ".join(parts + notes)
    return result


# --- Blackbox probes ---


def blackbox_source(samples: list[Sample], name: str) -> SourceResult:
    """Probes whose ``instance`` contains the name; a probe failing now is DEGRADED."""
    needle = name.strip().lower()
    matched = sorted(
        (labels.get("instance", ""), labels.get("job", ""), value)
        for labels, value in samples
        if needle in labels.get("instance", "").lower()
    )
    if not matched:
        return SourceResult("no match")
    parts = [
        f"{instance}{f' ({job})' if job else ''} {'succeeding' if value >= 1 else 'FAILING'}"
        for instance, job, value in matched
    ]
    failing = [f"probe {instance} failing now" for instance, _, value in matched if value < 1]
    return SourceResult("ok", "; ".join(parts), degraded=failing)


# --- Grafana alerts ---


def _alert_mentions(alert: GrafanaAlert, needle: str) -> bool:
    labels = alert.get("labels", {})
    texts = [value for key, value in labels.items() if not key.startswith("__")]
    texts.append(alert.get("annotations", {}).get("summary", ""))
    return any(needle in text.lower() for text in texts)


def grafana_source(alerts: list[tuple[str, GrafanaAlert]], name: str) -> SourceResult:
    """Active alerts whose name, a label value or summary contains the name; each is DEGRADED."""
    needle = name.strip().lower()
    matched = [alert for _, alert in alerts if _alert_mentions(alert, needle)]
    if not matched:
        return SourceResult("no match")
    result = SourceResult("ok")
    parts: list[str] = []
    for alert in matched:
        labels = alert.get("labels", {})
        alertname = labels.get("alertname", "unnamed")
        parts.append(f"{alertname} (severity={labels.get('severity', 'none')}) active")
        result.degraded.append(f"Grafana alert {alertname} is active")
    result.detail = "; ".join(parts)
    return result


# --- Loki ---


def loki_error_query(name: str, seconds: int) -> str:
    """Error lines per service whose ``service_name`` contains the name (escaped, any case) over the window."""
    pattern = promql_string(f"(?i).*{regex_literal(name.strip())}.*")
    selector = f"{{service_name=~{pattern}, detected_level=~{promql_string(ERROR_LEVELS)}}}"
    return f"sum by (service_name) (count_over_time({selector}[{seconds}s]))"


def loki_source(samples: list[Sample], name: str, window: str) -> SourceResult:
    """Error line counts per matching service; any error line is DEGRADED. No error lines is "no match"."""
    counts = sorted(
        ((labels.get("service_name", "unknown"), round(value)) for labels, value in samples if value > 0),
        key=lambda item: (-item[1], item[0]),
    )
    if not counts:
        return SourceResult("no match", f"no error lines from a service_name containing '{name.strip()}' over {window}")
    total = sum(count for _, count in counts)
    detail = f"{_plural(total, 'error line')} over {window}: " + ", ".join(f"{svc} {count}" for svc, count in counts)
    return SourceResult("ok", detail, degraded=[f"{_plural(total, 'error log line')} in Loki over {window}"])


# --- Verdict and output ---


def decide(results: dict[str, SourceResult]) -> tuple[Verdict, list[str]]:
    """Apply the fixed verdict rules: DOWN, then DEGRADED, then UP if anything matched, else UNKNOWN."""
    down = [reason for result in results.values() for reason in result.down]
    if down:
        return "DOWN", down
    degraded = [reason for result in results.values() for reason in result.degraded]
    if degraded:
        return "DEGRADED", degraded

    unavailable = [SOURCE_NAMES[key] for key, result in results.items() if result.status == "unavailable"]
    note = [f"unavailable: {', '.join(unavailable)}"] if unavailable else []
    matched = [SOURCE_NAMES[key] for key, result in results.items() if result.status == "ok"]
    if matched:
        return "UP", [f"matched by {', '.join(matched)}", *note]
    return "UNKNOWN", ["no source matched", *note]


def format_health(name: str, window: str, results: dict[str, SourceResult]) -> str:
    """The verdict line with its reasons, then one line per source in ``SOURCE_NAMES`` order."""
    verdict, reasons = decide(results)
    lines = [f"{verdict}: {name.strip()} over {window}: {'; '.join(reasons)}"]
    lines.extend(f"{SOURCE_NAMES[key]}: {results[key].text()}" for key in SOURCE_NAMES if key in results)
    return "\n".join(lines)


# --- Fetching ---


async def _prometheus_vector(promql: str) -> list[Sample]:
    data = await prometheus._query_prometheus("/api/v1/query", {"query": promql})  # pyright: ignore[reportPrivateUsage]
    return _vector_samples(data, "Prometheus")


async def _fetch_gatus(name: str, seconds: int, window: str) -> SourceResult:
    now, ratio = await asyncio.gather(
        _prometheus_vector(GATUS_SUCCESS), _prometheus_vector(f"avg_over_time({GATUS_SUCCESS}[{seconds}s])")
    )
    return gatus_source(now, ratio, name, window)


async def _fetch_containers(name: str, seconds: int, window: str) -> SourceResult:
    report = await collect_containers(name=name.strip(), restart_window_seconds=seconds)
    return container_source(report, window)


async def _fetch_blackbox(name: str) -> SourceResult:
    return blackbox_source(await _prometheus_vector(PROBE_SUCCESS), name)


async def _fetch_grafana(name: str) -> SourceResult:
    payload = await grafana_alerts._grafana_get(grafana_alerts.ALERT_GROUPS_PATH)  # pyright: ignore[reportPrivateUsage]
    if not isinstance(payload, list):
        raise _SourceError(f"Grafana alerts API returned {type(payload).__name__}, expected a list")
    groups: list[GrafanaAlertGroup] = payload  # pyright: ignore[reportUnknownVariableType]
    return grafana_source(grafana_alerts._select_alerts(groups, "active"), name)  # pyright: ignore[reportPrivateUsage]


async def _fetch_loki(name: str, seconds: int, window: str) -> SourceResult:
    if not get_settings().loki_url:
        return SourceResult("unavailable", "LOKI_URL not set")
    data = await loki._query_loki("/loki/api/v1/query", {"query": loki_error_query(name, seconds)})  # pyright: ignore[reportPrivateUsage]
    return loki_source(_vector_samples(data, "Loki"), name, window)


async def check_service_health(name: str, window: str) -> dict[str, SourceResult]:
    """Run the five sources concurrently, keyed as ``SOURCE_NAMES``.

    An HTTP, response or helper (``ToolException``) failure makes that source "unavailable" and is
    logged at WARNING; any other exception propagates. Raises ``ToolException`` for a bad window.
    """
    seconds = window_seconds(window)
    raw = await asyncio.gather(
        _fetch_gatus(name, seconds, window),
        _fetch_containers(name, seconds, window),
        _fetch_blackbox(name),
        _fetch_grafana(name),
        _fetch_loki(name, seconds, window),
        return_exceptions=True,
    )
    results: dict[str, SourceResult] = {}
    for key, outcome in zip(SOURCE_NAMES, raw, strict=True):
        if isinstance(outcome, SourceResult):
            results[key] = outcome
        elif isinstance(outcome, _EXPECTED_ERRORS):
            results[key] = SourceResult("unavailable", _reason(outcome))
        else:
            raise outcome
    for key, result in results.items():
        if result.status == "unavailable":
            logger.warning("service_health source %s unavailable: %s", SOURCE_NAMES[key], result.detail)
    return results


TOOL_DESCRIPTION = (
    "Answer 'is this service up?' in one call. Checks five sources at once for the named service: Gatus endpoint "
    "results (matched on the endpoint name or key), Docker containers (name contains it), blackbox probes (probed "
    "URL contains it), active Grafana alerts that mention it, and Loki error lines from services whose name "
    "contains it.\n\n"
    "Returns a verdict line first, with the reasons that decided it: DOWN (Gatus failing now, or a container not "
    "running), DEGRADED (container unhealthy or restarted in the window, Gatus success below 100% over the window, "
    "a probe failing now, an active alert, or Loki error lines), UP (something matched and none of those), UNKNOWN "
    "(nothing matched). Then one line per source: 'ok: ...', 'no match' or 'unavailable: reason'. window (5m to 7d, "
    "default 1h) sets the restart, Gatus ratio and Loki error range. Drill down with container_status, "
    "grafana_get_alerts or loki_query_logs."
)


@tool("service_health", args_schema=ServiceHealthInput)
async def service_health(name: str, window: str = "1h") -> str:
    """One health verdict for a named service. See TOOL_DESCRIPTION."""
    logger.info("Service health: name=%s window=%s", name, window)
    if not name.strip():
        raise ToolException("name is blank: give a service name, e.g. 'jellyfin'.")
    results = await check_service_health(name, window)
    return format_health(name, window, results)


service_health.description = TOOL_DESCRIPTION
service_health.handle_tool_error = True
