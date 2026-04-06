"""LangChain tools for querying the Proxmox VE API."""

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


def _pve_ssl_verify() -> ssl.SSLContext | bool:
    """Build the SSL verification parameter for httpx.

    Returns False (skip verification) by default.
    If verify_ssl is True and a CA cert path is provided, returns an SSLContext.
    If verify_ssl is True with no cert, returns True (system CA bundle).
    """
    settings = get_settings()
    if not settings.proxmox_verify_ssl:
        return False
    if settings.proxmox_ca_cert:
        ctx = ssl.create_default_context(cafile=settings.proxmox_ca_cert)
        return ctx
    return True


def _pve_headers() -> dict[str, str]:
    """Build authorization headers for the Proxmox VE API."""
    return {
        "Authorization": f"PVEAPIToken={get_settings().proxmox_api_token}",
        "Accept": "application/json",
    }


# --- HTTP helper ---


async def _pve_get(path: str, params: dict[str, str] | None = None) -> dict[str, object]:
    """Make an authenticated GET request to the Proxmox VE API.

    The PVE API wraps all responses in ``{"data": ...}``.
    """
    url = f"{get_settings().proxmox_url}/api2/json{path}"
    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS,
        verify=_pve_ssl_verify(),
    ) as client:
        response = await client.get(url, headers=_pve_headers(), params=params)
        _ = response.raise_for_status()
        data: dict[str, object] = response.json()  # pyright: ignore[reportAny]
        return data


# --- Response TypedDicts ---


class PveGuestEntry(TypedDict, total=False):
    vmid: int
    name: str
    status: str
    type: str
    cpus: int
    maxmem: int
    maxdisk: int
    uptime: int
    pid: int
    netin: int
    netout: int
    mem: int
    disk: int
    cpu: float


class PveNodeStatus(TypedDict, total=False):
    cpu: float
    memory: dict[str, int]
    uptime: int
    loadavg: list[str]
    kversion: str
    pveversion: str
    cpuinfo: dict[str, object]
    rootfs: dict[str, int]


class PveTaskEntry(TypedDict, total=False):
    upid: str
    node: str
    type: str
    status: str
    user: str
    starttime: int
    endtime: int
    id: str


class PveGuestConfig(TypedDict, total=False):
    name: str
    memory: int
    cores: int
    sockets: int
    ostype: str
    boot: str
    net0: str
    scsi0: str
    ide2: str
    agent: str
    balloon: int
    onboot: int


# --- Formatting helpers ---


def _format_bytes(n: int) -> str:
    """Format a byte count as a human-readable string."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n = int(n / 1024)
    return f"{n:.1f} PiB"


def _format_guests(guests: list[PveGuestEntry]) -> str:
    """Format a list of PVE guests into a readable string."""
    if not guests:
        return "No guests found on this node."

    running = [g for g in guests if g.get("status") == "running"]
    stopped = [g for g in guests if g.get("status") != "running"]

    lines: list[str] = [f"Found {len(guests)} guest(s) ({len(running)} running, {len(stopped)} stopped):\n"]

    for guest in sorted(guests, key=lambda g: g.get("vmid", 0)):
        vmid = guest.get("vmid", 0)
        name = guest.get("name", "unnamed")
        status = guest.get("status", "unknown")
        gtype = guest.get("type", "unknown")
        cpus = guest.get("cpus", 0)
        maxmem = guest.get("maxmem", 0)
        cpu_pct = guest.get("cpu", 0.0) * 100

        type_label = "VM" if gtype == "qemu" else "CT" if gtype == "lxc" else gtype
        mem_str = _format_bytes(maxmem) if maxmem else "?"

        status_marker = "+" if status == "running" else "-"
        lines.append(
            f"  {status_marker} {vmid} {name} ({type_label}, {status}) "
            f"— {cpus} vCPU, {mem_str} RAM" + (f", CPU {cpu_pct:.0f}%" if status == "running" else "")
        )

    return "\n".join(lines)


def _format_node_status(data: dict[str, object]) -> str:
    """Format PVE node status into a readable string."""
    cpu = data.get("cpu")
    cpu_pct = f"{float(cpu) * 100:.1f}%" if cpu is not None else "?"  # type: ignore[arg-type]  # pyright: ignore[reportAny]

    memory = data.get("memory", {})
    if isinstance(memory, dict):
        mem_used = int(memory.get("used", 0))  # pyright: ignore[reportAny]
        mem_total = int(memory.get("total", 0))  # pyright: ignore[reportAny]
        mem_str = f"{_format_bytes(mem_used)} / {_format_bytes(mem_total)}"
    else:
        mem_str = "?"

    uptime = data.get("uptime", 0)
    uptime_days = int(uptime) // 86400 if isinstance(uptime, (int, float)) else 0  # pyright: ignore[reportAny]

    loadavg = data.get("loadavg", [])
    load_str = ", ".join(str(v) for v in loadavg) if isinstance(loadavg, list) else "?"  # pyright: ignore[reportAny]

    pve_version = data.get("pveversion", "?")
    kernel = data.get("kversion", "?")

    rootfs = data.get("rootfs", {})
    if isinstance(rootfs, dict):
        root_used = int(rootfs.get("used", 0))  # pyright: ignore[reportAny]
        root_total = int(rootfs.get("total", 0))  # pyright: ignore[reportAny]
        root_str = f"{_format_bytes(root_used)} / {_format_bytes(root_total)}"
    else:
        root_str = "?"

    lines: list[str] = [
        "Proxmox Node Status:",
        f"  CPU: {cpu_pct}",
        f"  Memory: {mem_str}",
        f"  Root FS: {root_str}",
        f"  Load average: {load_str}",
        f"  Uptime: {uptime_days} days",
        f"  PVE version: {pve_version}",
        f"  Kernel: {kernel}",
    ]
    return "\n".join(lines)


def _format_tasks(tasks: list[PveTaskEntry]) -> str:
    """Format PVE task list into a readable string."""
    if not tasks:
        return "No recent tasks found."

    lines: list[str] = [f"Found {len(tasks)} recent task(s):\n"]

    for task in tasks[:MAX_TASKS]:
        task_type = task.get("type", "unknown")
        status = task.get("status", "unknown")
        user = task.get("user", "unknown")
        task_id = task.get("id", "")
        starttime = task.get("starttime", 0)
        endtime = task.get("endtime")

        start_str = datetime.fromtimestamp(starttime, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        end_str = ""
        if endtime:
            end_str = datetime.fromtimestamp(endtime, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

        status_marker = "OK" if status == "OK" else status
        id_str = f" ({task_id})" if task_id else ""

        line = f"  [{status_marker}] {task_type}{id_str} by {user} (start={start_str}"
        if end_str:
            line += f", end={end_str}"
        line += ")"
        lines.append(line)

    if len(tasks) > MAX_TASKS:
        lines.append(f"\n(showing first {MAX_TASKS} of {len(tasks)} tasks)")

    return "\n".join(lines)


def _format_guest_config(vmid: int, config: dict[str, object]) -> str:
    """Format PVE guest configuration into a readable string."""
    name = config.get("name", "unnamed")
    lines: list[str] = [f"Configuration for {vmid} ({name}):\n"]

    # Group keys by category
    compute_keys = ("cores", "sockets", "memory", "balloon", "cpu", "numa", "vcpus")
    disk_prefixes = ("scsi", "virtio", "ide", "sata", "efidisk", "rootfs", "mp")
    net_prefix = "net"
    boot_keys = ("boot", "onboot", "startup", "agent", "ostype")

    compute: list[str] = []
    disks: list[str] = []
    networks: list[str] = []
    boot: list[str] = []
    other: list[str] = []

    for key, value in sorted(config.items()):
        if key.startswith("digest") or key.startswith("_"):
            continue
        entry = f"  {key}: {value}"
        if key in compute_keys:
            compute.append(entry)
        elif any(key.startswith(p) for p in disk_prefixes):
            disks.append(entry)
        elif key.startswith(net_prefix):
            networks.append(entry)
        elif key in boot_keys:
            boot.append(entry)
        else:
            other.append(entry)

    if compute:
        lines.append("Compute:")
        lines.extend(compute)
    if disks:
        lines.append("Disks:")
        lines.extend(disks)
    if networks:
        lines.append("Network:")
        lines.extend(networks)
    if boot:
        lines.append("Boot / OS:")
        lines.extend(boot)
    if other:
        lines.append("Other:")
        lines.extend(other)

    return "\n".join(lines)


# --- Input schemas ---


class ListGuestsInput(BaseModel):
    """Input for listing Proxmox guests."""

    guest_type: str | None = Field(
        default=None,
        description="Filter by guest type: 'qemu' (VMs) or 'lxc' (containers). Omit to list both.",
    )


class GetGuestConfigInput(BaseModel):
    """Input for fetching a single guest's configuration."""

    vmid: int | None = Field(
        default=None,
        description="The VM/container ID (e.g. 100, 113). Provide either vmid or name.",
    )
    name: str | None = Field(
        default=None,
        description=(
            "Guest name (e.g. 'immich', 'jellyfin'). If provided, automatically "
            "resolves to the correct VMID and guest type. Preferred when you don't know the VMID."
        ),
    )
    guest_type: str = Field(
        default="qemu",
        description="Guest type: 'qemu' for VMs, 'lxc' for containers. Ignored when using name lookup.",
    )


class NodeStatusInput(BaseModel):
    """Input for fetching Proxmox node status."""

    pass


class ListTasksInput(BaseModel):
    """Input for listing recent Proxmox tasks."""

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


TOOL_DESCRIPTION_LIST_GUESTS = HOMELAB_CONTEXT + (
    "List all VMs and containers on the Proxmox node with their status, "
    "resource allocation, and current CPU usage. "
    "Use this to answer questions like 'what VMs are running?', "
    "'list all containers', 'how many guests are there?', "
    "or 'which VMs are stopped?'.\n\n"
    "Returns VMID, name, type (VM/CT), status, vCPUs, RAM, and CPU usage.\n\n"
    "For detailed configuration of a specific guest (disks, network, boot order), "
    "use proxmox_get_guest_config instead."
)

TOOL_DESCRIPTION_GET_CONFIG = HOMELAB_CONTEXT + (
    "Get the full configuration of a specific VM or container. "
    "Use this to answer questions like 'what disks does VM 100 have?', "
    "'show the config for jellyfin', 'how much RAM is allocated to container 101?', "
    "or 'what network interface does VM 102 use?'.\n\n"
    "Returns compute settings (CPU, RAM), disk devices, network interfaces, "
    "boot/OS settings, and other configuration parameters.\n\n"
    "You can identify the guest by either:\n"
    "- `name` (preferred) — e.g. name='immich'. Automatically resolves the VMID and type.\n"
    "- `vmid` + `guest_type` — e.g. vmid=113, guest_type='lxc'. Use when you know the ID."
)

TOOL_DESCRIPTION_NODE_STATUS = HOMELAB_CONTEXT + (
    "Get the overall status of the Proxmox host node. "
    "Use this to answer questions like 'how is the Proxmox server doing?', "
    "'what is the host CPU usage?', 'how much RAM is available on the hypervisor?', "
    "or 'what PVE version is running?'.\n\n"
    "Returns CPU usage, memory usage, root filesystem usage, load average, "
    "uptime, PVE version, and kernel version.\n\n"
    "For per-VM/container metrics use Prometheus tools or proxmox_list_guests."
)

TOOL_DESCRIPTION_LIST_TASKS = HOMELAB_CONTEXT + (
    "List recent Proxmox tasks (migrations, backups, snapshot operations, etc). "
    "Use this to answer questions like 'any recent failed tasks?', "
    "'is a migration running?', 'did the backup task complete?', "
    "or 'what tasks ran today?'.\n\n"
    "Returns task type, status (OK/error), user, start/end time, and guest ID. "
    "Can filter to show only failed tasks."
)


# --- Tools ---


@tool("proxmox_list_guests", args_schema=ListGuestsInput)  # pyright: ignore[reportUnknownParameterType]
async def proxmox_list_guests(guest_type: str | None = None) -> str:
    """List VMs and containers on the Proxmox node. See TOOL_DESCRIPTION_LIST_GUESTS."""
    settings = get_settings()
    if not settings.proxmox_url:
        raise ToolException("Proxmox VE is not configured (PROXMOX_URL is empty).")

    node = settings.proxmox_node
    logger.info("Listing Proxmox guests (type=%s, node=%s)", guest_type, node)

    guests: list[PveGuestEntry] = []
    types_to_fetch = [guest_type] if guest_type in ("qemu", "lxc") else ["qemu", "lxc"]

    try:
        for gtype in types_to_fetch:
            data = await _pve_get(f"/nodes/{node}/{gtype}")
            raw_list: list[dict[str, object]] = data.get("data", [])  # type: ignore[assignment]  # pyright: ignore[reportAssignmentType]
            for entry in raw_list:
                typed: PveGuestEntry = {**entry, "type": gtype}  # type: ignore[typeddict-item]
                guests.append(typed)
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to Proxmox at {settings.proxmox_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"Proxmox request timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e
    except httpx.HTTPStatusError as e:
        raise ToolException(f"Proxmox API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e

    return _format_guests(guests)


proxmox_list_guests.description = TOOL_DESCRIPTION_LIST_GUESTS
proxmox_list_guests.handle_tool_error = True


async def _resolve_guest_by_name(name: str) -> tuple[int, str]:
    """Resolve a guest name to (vmid, guest_type) by listing all guests.

    Raises ToolException if the name is not found or matches multiple guests.
    """
    settings = get_settings()
    node = settings.proxmox_node
    name_lower = name.lower()

    matches: list[tuple[int, str, str]] = []  # (vmid, guest_type, actual_name)

    for gtype in ("qemu", "lxc"):
        data = await _pve_get(f"/nodes/{node}/{gtype}")
        raw_list: list[dict[str, object]] = data.get("data", [])  # type: ignore[assignment]  # pyright: ignore[reportAssignmentType]
        for entry in raw_list:
            entry_name = str(entry.get("name", ""))
            if entry_name.lower() == name_lower:
                vmid_val = entry.get("vmid", 0)
                vmid_raw: int = int(vmid_val)  # type: ignore[call-overload]
                matches.append((vmid_raw, gtype, entry_name))

    if not matches:
        raise ToolException(
            f"No guest found with name '{name}'. Use proxmox_list_guests to see available guests and their VMIDs."
        )
    if len(matches) > 1:
        match_strs = [f"{m[0]} ({m[1]})" for m in matches]
        raise ToolException(
            f"Multiple guests match name '{name}': {', '.join(match_strs)}. Use vmid instead to specify which one."
        )

    return matches[0][0], matches[0][1]


@tool("proxmox_get_guest_config", args_schema=GetGuestConfigInput)  # pyright: ignore[reportUnknownParameterType]
async def proxmox_get_guest_config(
    vmid: int | None = None,
    name: str | None = None,
    guest_type: str = "qemu",
) -> str:
    """Get configuration for a specific VM or container. See TOOL_DESCRIPTION_GET_CONFIG."""
    settings = get_settings()
    if not settings.proxmox_url:
        raise ToolException("Proxmox VE is not configured (PROXMOX_URL is empty).")

    if vmid is None and name is None:
        raise ToolException("Provide either vmid or name to identify the guest.")

    # Resolve name to vmid if needed
    if name is not None and vmid is None:
        try:
            vmid, guest_type = await _resolve_guest_by_name(name)
        except httpx.ConnectError as e:
            raise ToolException(f"Cannot connect to Proxmox at {settings.proxmox_url}: {e}") from e
        except httpx.TimeoutException as e:
            raise ToolException(f"Proxmox request timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e
        except httpx.HTTPStatusError as e:
            raise ToolException(f"Proxmox API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e

    assert vmid is not None  # satisfied by either direct input or name resolution

    node = settings.proxmox_node
    logger.info("Fetching Proxmox guest config (vmid=%d, type=%s, node=%s)", vmid, guest_type, node)

    try:
        data = await _pve_get(f"/nodes/{node}/{guest_type}/{vmid}/config")
        config: dict[str, object] = data.get("data", {})  # type: ignore[assignment]  # pyright: ignore[reportAssignmentType]
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to Proxmox at {settings.proxmox_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"Proxmox request timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 500:
            raise ToolException(
                f"Guest {vmid} not found as {guest_type}. "
                "Try the other type (qemu/lxc) or check the VMID with proxmox_list_guests."
            ) from e
        raise ToolException(f"Proxmox API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e

    return _format_guest_config(vmid, config)


proxmox_get_guest_config.description = TOOL_DESCRIPTION_GET_CONFIG
proxmox_get_guest_config.handle_tool_error = True


@tool("proxmox_node_status", args_schema=NodeStatusInput)  # pyright: ignore[reportUnknownParameterType]
async def proxmox_node_status() -> str:
    """Get Proxmox host node status. See TOOL_DESCRIPTION_NODE_STATUS."""
    settings = get_settings()
    if not settings.proxmox_url:
        raise ToolException("Proxmox VE is not configured (PROXMOX_URL is empty).")

    node = settings.proxmox_node
    logger.info("Fetching Proxmox node status (node=%s)", node)

    try:
        data = await _pve_get(f"/nodes/{node}/status")
        status: dict[str, object] = data.get("data", {})  # type: ignore[assignment]  # pyright: ignore[reportAssignmentType]
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to Proxmox at {settings.proxmox_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"Proxmox request timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e
    except httpx.HTTPStatusError as e:
        raise ToolException(f"Proxmox API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e

    return _format_node_status(status)


proxmox_node_status.description = TOOL_DESCRIPTION_NODE_STATUS
proxmox_node_status.handle_tool_error = True


@tool("proxmox_list_tasks", args_schema=ListTasksInput)  # pyright: ignore[reportUnknownParameterType]
async def proxmox_list_tasks(limit: int = 20, errors_only: bool = False) -> str:
    """List recent Proxmox tasks. See TOOL_DESCRIPTION_LIST_TASKS."""
    settings = get_settings()
    if not settings.proxmox_url:
        raise ToolException("Proxmox VE is not configured (PROXMOX_URL is empty).")

    node = settings.proxmox_node
    logger.info("Listing Proxmox tasks (limit=%d, errors_only=%s, node=%s)", limit, errors_only, node)

    params: dict[str, str] = {"limit": str(limit)}
    if errors_only:
        params["errors"] = "1"

    try:
        data = await _pve_get(f"/nodes/{node}/tasks", params=params)
        tasks: list[PveTaskEntry] = data.get("data", [])  # type: ignore[assignment]  # pyright: ignore[reportAssignmentType]
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to Proxmox at {settings.proxmox_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"Proxmox request timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e
    except httpx.HTTPStatusError as e:
        raise ToolException(f"Proxmox API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e

    return _format_tasks(tasks)


proxmox_list_tasks.description = TOOL_DESCRIPTION_LIST_TASKS
proxmox_list_tasks.handle_tool_error = True
