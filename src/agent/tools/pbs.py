"""LangChain tools for querying the Proxmox Backup Server (PBS) API."""

import logging
import ssl
from datetime import UTC, datetime
from typing import TypedDict

import httpx
from langchain_core.tools import ToolException, tool  # pyright: ignore[reportUnknownVariableType]
from pydantic import BaseModel, Field

from src.agent.tools import HOMELAB_CONTEXT
from src.config import get_settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15
MAX_TASKS = 50


# --- SSL helper ---


def _pbs_ssl_verify() -> ssl.SSLContext | bool:
    """Build the SSL verification parameter for httpx.

    Returns False (skip verification) by default.
    If verify_ssl is True and a CA cert path is provided, returns an SSLContext.
    If verify_ssl is True with no cert, returns True (system CA bundle).
    """
    settings = get_settings()
    if not settings.pbs_verify_ssl:
        return False
    if settings.pbs_ca_cert:
        ctx = ssl.create_default_context(cafile=settings.pbs_ca_cert)
        return ctx
    return True


def _pbs_headers() -> dict[str, str]:
    """Build authorization headers for the PBS API."""
    return {
        "Authorization": f"PBSAPIToken={get_settings().pbs_api_token}",
        "Accept": "application/json",
    }


# --- HTTP helper ---


async def _pbs_get(path: str, params: dict[str, str] | None = None) -> dict[str, object]:
    """Make an authenticated GET request to the PBS API.

    The PBS API wraps all responses in ``{"data": ...}``.
    """
    url = f"{get_settings().pbs_url}/api2/json{path}"
    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS,
        verify=_pbs_ssl_verify(),
    ) as client:
        response = await client.get(url, headers=_pbs_headers(), params=params)
        _ = response.raise_for_status()
        data: dict[str, object] = response.json()  # pyright: ignore[reportAny]
        return data


# --- Response TypedDicts ---


class PbsDatastoreStatus(TypedDict, total=False):
    store: str
    total: int
    used: int
    avail: int
    gc_status: dict[str, object]


class PbsBackupGroup(TypedDict, total=False):
    backup_type: str
    backup_id: str
    last_backup: int
    backup_count: int
    files: list[str]
    owner: str
    comment: str


class PbsTaskEntry(TypedDict, total=False):
    upid: str
    node: str
    worker_type: str
    worker_id: str
    status: str
    user: str
    starttime: int
    endtime: int


# --- Formatting helpers ---


def _format_bytes(n: int) -> str:
    """Format a byte count as a human-readable string."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n = int(n / 1024)
    return f"{n:.1f} PiB"


def _format_datastore_status(stores: list[PbsDatastoreStatus]) -> str:
    """Format PBS datastore usage into a readable string."""
    if not stores:
        return "No datastores found."

    lines: list[str] = [f"Found {len(stores)} datastore(s):\n"]

    for store in stores:
        name = store.get("store", "unknown")
        total = store.get("total", 0)
        used = store.get("used", 0)
        avail = store.get("avail", 0)

        pct = (used / total) * 100 if total > 0 else 0.0

        lines.append(f"  {name}:")
        lines.append(f"    Used: {_format_bytes(used)} / {_format_bytes(total)} ({pct:.1f}%)")
        lines.append(f"    Available: {_format_bytes(avail)}")

        # PBS API uses hyphenated keys
        gc = store.get("gc-status") or store.get("gc_status")
        if isinstance(gc, dict):
            gc_status = gc.get("last-run-state", "unknown")  # pyright: ignore[reportAny]
            lines.append(f"    Last GC: {gc_status}")

    return "\n".join(lines)


def _format_backup_groups(groups: list[PbsBackupGroup], datastore: str) -> str:
    """Format PBS backup groups into a readable string."""
    if not groups:
        return f"No backup groups found in datastore '{datastore}'."

    lines: list[str] = [f"Found {len(groups)} backup group(s) in '{datastore}':\n"]

    def _g(d: PbsBackupGroup, key: str, default: str = "") -> str:
        """Get a string value trying hyphenated key first (PBS API format), then underscored."""
        hyphenated = key.replace("_", "-")
        val = d.get(hyphenated) or d.get(key)
        return str(val) if val is not None else default

    # PBS API uses hyphenated keys (backup-type, backup-id, etc.)
    for group in sorted(groups, key=lambda g: _g(g, "backup_id")):
        backup_type = _g(group, "backup_type", "unknown")
        backup_id = _g(group, "backup_id", "unknown")
        count_str = _g(group, "backup_count", "0")
        last = _g(group, "last_backup", "0")
        owner = _g(group, "owner")
        comment = _g(group, "comment")

        type_label = {"vm": "VM", "ct": "CT", "host": "Host"}.get(backup_type, backup_type)
        lines.append(f"  {type_label}/{backup_id}: {count_str} backup(s), last={last}")
        if owner:
            lines.append(f"    Owner: {owner}")
        if comment:
            lines.append(f"    Comment: {comment}")

    return "\n".join(lines)


def _format_pbs_tasks(tasks: list[PbsTaskEntry]) -> str:
    """Format PBS task list into a readable string."""
    if not tasks:
        return "No recent PBS tasks found."

    lines: list[str] = [f"Found {len(tasks)} recent PBS task(s):\n"]

    for task in tasks[:MAX_TASKS]:
        worker_type = task.get("worker_type", "unknown")
        worker_id = task.get("worker_id", "")
        status = task.get("status", "unknown")
        user = task.get("user", "unknown")
        starttime = task.get("starttime", 0)
        endtime = task.get("endtime")

        start_str = datetime.fromtimestamp(starttime, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        end_str = ""
        if endtime:
            end_str = datetime.fromtimestamp(endtime, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

        status_marker = "OK" if status == "OK" else status
        id_str = f" ({worker_id})" if worker_id else ""

        line = f"  [{status_marker}] {worker_type}{id_str} by {user} (start={start_str}"
        if end_str:
            line += f", end={end_str}"
        line += ")"
        lines.append(line)

    if len(tasks) > MAX_TASKS:
        lines.append(f"\n(showing first {MAX_TASKS} of {len(tasks)} tasks)")

    return "\n".join(lines)


# --- Input schemas ---


class DatastoreStatusInput(BaseModel):
    """Input for fetching PBS datastore usage."""

    pass


class ListBackupsInput(BaseModel):
    """Input for listing backup groups in a PBS datastore."""

    datastore: str | None = Field(
        default=None,
        description=(
            "Name of the PBS datastore to list backups from. Omit to use the default datastore from configuration."
        ),
    )


class ListPbsTasksInput(BaseModel):
    """Input for listing recent PBS tasks."""

    limit: int = Field(
        default=20,
        description="Maximum number of tasks to return (default 20, max 50).",
        ge=1,
        le=MAX_TASKS,
    )
    errors_only: bool = Field(
        default=False,
        description="If true, only return tasks that did not complete successfully.",
    )


# --- Tool descriptions ---


TOOL_DESCRIPTION_DATASTORE_STATUS = HOMELAB_CONTEXT + (
    "Get storage usage for all PBS datastores. "
    "Use this to answer questions like 'how much backup space is left?', "
    "'is the backup store full?', or 'show PBS datastore usage'.\n\n"
    "Returns datastore name, total/used/available space, usage percentage, "
    "and last garbage collection status."
)

TOOL_DESCRIPTION_LIST_BACKUPS = HOMELAB_CONTEXT + (
    "List backup groups in a PBS datastore. Each group represents a "
    "backed-up guest (VM/CT) or host, showing the number of snapshots "
    "and when the last backup was taken.\n\n"
    "Use this to answer questions like 'when was VM 100 last backed up?', "
    "'list all backups', or 'which VMs are being backed up?'.\n\n"
    "Returns backup type (VM/CT/Host), ID, snapshot count, last backup time, "
    "and owner."
)

TOOL_DESCRIPTION_PBS_TASKS = HOMELAB_CONTEXT + (
    "List recent PBS tasks (backup jobs, garbage collection, verification, etc). "
    "Use this to answer questions like 'did last night's backup succeed?', "
    "'any failed backup tasks?', or 'what PBS jobs ran recently?'.\n\n"
    "Returns task type, status (OK/error), user, start/end time, and worker ID. "
    "Can filter to show only failed tasks."
)


# --- Tools ---


@tool("pbs_datastore_status", args_schema=DatastoreStatusInput)  # pyright: ignore[reportUnknownParameterType]
async def pbs_datastore_status() -> str:
    """Get PBS datastore usage. See TOOL_DESCRIPTION_DATASTORE_STATUS."""
    settings = get_settings()
    if not settings.pbs_url:
        raise ToolException("Proxmox Backup Server is not configured (PBS_URL is empty).")

    logger.info("Fetching PBS datastore status")

    try:
        data = await _pbs_get("/status/datastore-usage")
        stores: list[PbsDatastoreStatus] = data.get("data", [])  # type: ignore[assignment]  # pyright: ignore[reportAssignmentType]
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to PBS at {settings.pbs_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"PBS request timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e
    except httpx.HTTPStatusError as e:
        raise ToolException(f"PBS API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e

    return _format_datastore_status(stores)


pbs_datastore_status.description = TOOL_DESCRIPTION_DATASTORE_STATUS
pbs_datastore_status.handle_tool_error = True


@tool("pbs_list_backups", args_schema=ListBackupsInput)  # pyright: ignore[reportUnknownParameterType]
async def pbs_list_backups(datastore: str | None = None) -> str:
    """List backup groups in a PBS datastore. See TOOL_DESCRIPTION_LIST_BACKUPS."""
    settings = get_settings()
    if not settings.pbs_url:
        raise ToolException("Proxmox Backup Server is not configured (PBS_URL is empty).")

    store = datastore or settings.pbs_default_datastore
    if not store:
        raise ToolException(
            "No datastore specified and PBS_DEFAULT_DATASTORE is not configured. "
            "Provide a datastore name or set PBS_DEFAULT_DATASTORE in .env."
        )

    logger.info("Listing PBS backup groups (datastore=%s)", store)

    try:
        data = await _pbs_get(f"/admin/datastore/{store}/groups")
        groups: list[PbsBackupGroup] = data.get("data", [])  # type: ignore[assignment]  # pyright: ignore[reportAssignmentType]
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to PBS at {settings.pbs_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"PBS request timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e
    except httpx.HTTPStatusError as e:
        raise ToolException(f"PBS API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e

    return _format_backup_groups(groups, store)


pbs_list_backups.description = TOOL_DESCRIPTION_LIST_BACKUPS
pbs_list_backups.handle_tool_error = True


@tool("pbs_list_tasks", args_schema=ListPbsTasksInput)  # pyright: ignore[reportUnknownParameterType]
async def pbs_list_tasks(limit: int = 20, errors_only: bool = False) -> str:
    """List recent PBS tasks. See TOOL_DESCRIPTION_PBS_TASKS."""
    settings = get_settings()
    if not settings.pbs_url:
        raise ToolException("Proxmox Backup Server is not configured (PBS_URL is empty).")

    node = settings.pbs_node
    logger.info("Listing PBS tasks (limit=%d, errors_only=%s, node=%s)", limit, errors_only, node)

    params: dict[str, str] = {"limit": str(limit)}
    if errors_only:
        params["errors"] = "1"

    try:
        data = await _pbs_get(f"/nodes/{node}/tasks", params=params)
        tasks: list[PbsTaskEntry] = data.get("data", [])  # type: ignore[assignment]  # pyright: ignore[reportAssignmentType]
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to PBS at {settings.pbs_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"PBS request timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e
    except httpx.HTTPStatusError as e:
        raise ToolException(f"PBS API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e

    return _format_pbs_tasks(tasks)


pbs_list_tasks.description = TOOL_DESCRIPTION_PBS_TASKS
pbs_list_tasks.handle_tool_error = True
