"""LangChain tools for querying the TrueNAS SCALE REST API."""

import json
import logging
import ssl
from typing import TypedDict

import httpx
from langchain_core.tools import ToolException, tool  # pyright: ignore[reportUnknownVariableType]
from pydantic import BaseModel, Field

from src.agent.tools import HOMELAB_CONTEXT
from src.config import get_settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15
MAX_SNAPSHOTS = 50
MAX_JOBS = 30


# --- SSL helper ---


def _truenas_ssl_verify() -> ssl.SSLContext | bool:
    """Build the SSL verification parameter for httpx.

    Returns False (skip verification) by default.
    If verify_ssl is True and a CA cert path is provided, returns an SSLContext.
    If verify_ssl is True with no cert, returns True (system CA bundle).
    """
    settings = get_settings()
    if not settings.truenas_verify_ssl:
        return False
    if settings.truenas_ca_cert:
        ctx = ssl.create_default_context(cafile=settings.truenas_ca_cert)
        return ctx
    return True


def _truenas_headers() -> dict[str, str]:
    """Build authorization headers for the TrueNAS SCALE API."""
    return {
        "Authorization": f"Bearer {get_settings().truenas_api_key}",
        "Accept": "application/json",
    }


# --- HTTP helper ---


async def _truenas_get(
    path: str,
    params: dict[str, str] | None = None,
    timeout: int | None = None,
) -> object:
    """Make an authenticated GET request to the TrueNAS SCALE API.

    TrueNAS returns plain JSON (arrays or objects), NOT wrapped in ``{"data": ...}``.
    """
    url = f"{get_settings().truenas_url}/api/v2.0{path}"
    async with httpx.AsyncClient(
        timeout=timeout or DEFAULT_TIMEOUT_SECONDS,
        verify=_truenas_ssl_verify(),
    ) as client:
        response = await client.get(url, headers=_truenas_headers(), params=params)
        _ = response.raise_for_status()
        data: object = response.json()  # pyright: ignore[reportAny]
        return data


async def _truenas_post(path: str, body: dict[str, object] | None = None) -> object:
    """Make an authenticated POST request to the TrueNAS SCALE API.

    Some TrueNAS middleware methods (e.g. smb.status) require POST.
    """
    url = f"{get_settings().truenas_url}/api/v2.0{path}"
    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS,
        verify=_truenas_ssl_verify(),
    ) as client:
        response = await client.post(url, headers=_truenas_headers(), json=body or {})
        _ = response.raise_for_status()
        data: object = response.json()  # pyright: ignore[reportAny]
        return data


# --- Response TypedDicts ---


class TruenasPoolEntry(TypedDict, total=False):
    id: int
    name: str
    status: str
    healthy: bool
    path: str
    size: int
    allocated: int
    free: int
    topology: dict[str, object]


class TruenasDatasetEntry(TypedDict, total=False):
    id: str
    name: str
    pool: str
    type: str
    used: dict[str, object]
    available: dict[str, object]
    quota: dict[str, object]
    mountpoint: str


class TruenasNfsShareEntry(TypedDict, total=False):
    id: int
    path: str
    enabled: bool
    networks: list[str]
    hosts: list[str]
    ro: bool
    comment: str


class TruenasSmbShareEntry(TypedDict, total=False):
    id: int
    path: str
    enabled: bool
    name: str
    ro: bool
    comment: str
    purpose: str


class TruenasSmbShareConnection(TypedDict, total=False):
    """A single share connection within an SMB session."""

    service: str
    connected_at: str
    machine: str


class TruenasSmbSessionEntry(TypedDict, total=False):
    """A single active SMB session from POST /smb/status."""

    session_id: str
    uid: int
    username: str
    groupname: str
    remote_machine: str
    hostname: str
    session_dialect: str
    encryption: dict[str, str]
    signing: dict[str, str]
    creation_time: str
    share_connections: list[TruenasSmbShareConnection]


class TruenasSnapshotEntry(TypedDict, total=False):
    id: str
    name: str
    dataset: str
    snapshot_name: str
    properties: dict[str, object]
    type: str


class TruenasSnapshotTaskEntry(TypedDict, total=False):
    id: int
    dataset: str
    enabled: bool
    lifetime_value: int
    lifetime_unit: str
    naming_schema: str
    schedule: dict[str, str]
    recursive: bool


class TruenasReplicationEntry(TypedDict, total=False):
    id: int
    name: str
    state: dict[str, object]
    enabled: bool
    direction: str
    transport: str
    source_datasets: list[str]
    target_dataset: str
    auto: bool


class TruenasAlertEntry(TypedDict, total=False):
    id: str
    level: str
    formatted: str
    klass: str
    dismissed: bool


class TruenasJobEntry(TypedDict, total=False):
    id: int
    method: str
    state: str
    progress: dict[str, object]
    time_started: dict[str, object]
    time_finished: dict[str, object]
    error: str


class TruenasDiskEntry(TypedDict, total=False):
    identifier: str
    name: str
    serial: str
    model: str
    type: str
    size: int
    pool: str
    togglesmart: bool
    hddstandby: str


class TruenasSystemInfo(TypedDict, total=False):
    version: str
    hostname: str
    uptime_seconds: float
    system_product: str
    physmem: int
    cores: int
    loadavg: list[float]
    ecc_memory: bool


class TruenasAppEntry(TypedDict, total=False):
    name: str
    state: str
    version: str
    human_version: str
    upgrade_available: bool


# --- Formatting helpers ---


def _format_bytes(n: int) -> str:
    """Format a byte count as a human-readable string."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n = int(n / 1024)
    return f"{n:.1f} PiB"


def _extract_topology_disks(topology: dict[str, object]) -> list[tuple[str, str, str]]:
    """Extract (vdev_category, vdev_type, disk_name) tuples from pool topology.

    TrueNAS topology structure:
      {"data": [{"type": "MIRROR", "children": [{"disk": "sdc", ...}, ...]}, ...],
       "special": [...], "cache": [...], "log": [...], "spare": [...], "dedup": [...]}
    """
    results: list[tuple[str, str, str]] = []
    vdev_categories = ("data", "special", "cache", "log", "spare", "dedup")

    for category in vdev_categories:
        vdevs = topology.get(category)
        if not isinstance(vdevs, list):
            continue
        for vdev in vdevs:
            if not isinstance(vdev, dict):
                continue
            vdev_type = str(vdev.get("type", "UNKNOWN"))  # pyright: ignore[reportAny]
            children = vdev.get("children")
            if isinstance(children, list) and children:
                # Vdev with children (mirror, raidz, etc.)
                for child in children:
                    if isinstance(child, dict):
                        disk = str(child.get("disk", ""))  # pyright: ignore[reportAny]
                        if disk:
                            results.append((category, vdev_type, disk))
            else:
                # Single-disk vdev (stripe) — the vdev itself IS the disk
                disk = str(vdev.get("disk", ""))  # pyright: ignore[reportAny]
                if disk:
                    results.append((category, vdev_type, disk))

    return results


def _format_pools(pools: list[TruenasPoolEntry], datasets: list[TruenasDatasetEntry]) -> str:
    """Format TrueNAS pools and top-level dataset usage into a readable string."""
    if not pools:
        return "No ZFS pools found."

    lines: list[str] = [f"Found {len(pools)} pool(s):\n"]

    for pool in pools:
        name = pool.get("name", "unknown")
        status = pool.get("status", "unknown")
        healthy = pool.get("healthy", False)
        size = pool.get("size", 0)
        allocated = pool.get("allocated", 0)
        free = pool.get("free", 0)

        health_str = "HEALTHY" if healthy else "DEGRADED/UNHEALTHY"
        pct = (allocated / size) * 100 if size > 0 else 0.0

        lines.append(f"  {name} ({status}, {health_str}):")
        lines.append(f"    Size: {_format_bytes(size)}")
        lines.append(f"    Used: {_format_bytes(allocated)} ({pct:.1f}%)")
        lines.append(f"    Free: {_format_bytes(free)}")

        # Show disk topology — which disks are in which vdev category
        topology = pool.get("topology")
        if isinstance(topology, dict):
            disk_info = _extract_topology_disks(topology)
            if disk_info:
                # Group by category
                categories: dict[str, list[tuple[str, str]]] = {}
                for category, vdev_type, disk in disk_info:
                    categories.setdefault(category, []).append((vdev_type, disk))

                lines.append("    Disk topology:")
                for category, members in categories.items():
                    vdev_type = members[0][0] if members else "UNKNOWN"
                    disk_names = [m[1] for m in members]
                    lines.append(f"      {category} ({vdev_type}): {', '.join(disk_names)}")

        # Show top-level datasets for this pool
        pool_datasets = [d for d in datasets if d.get("pool") == name and "/" not in d.get("id", "/")]
        if not pool_datasets:
            # Fallback: datasets whose id starts with the pool name and has no further /
            pool_datasets = [
                d for d in datasets if d.get("id", "").startswith(name + "/") and d.get("id", "").count("/") == 1
            ]

        if pool_datasets:
            lines.append("    Top-level datasets:")
            for ds in sorted(pool_datasets, key=lambda d: d.get("id", "")):
                ds_id = ds.get("id", "unknown")
                used_raw = ds.get("used", {})
                avail_raw = ds.get("available", {})
                used_val = int(used_raw.get("rawvalue", 0)) if isinstance(used_raw, dict) else 0  # type: ignore[call-overload]  # pyright: ignore[reportAny]
                avail_val = int(avail_raw.get("rawvalue", 0)) if isinstance(avail_raw, dict) else 0  # type: ignore[call-overload]  # pyright: ignore[reportAny]
                lines.append(f"      {ds_id}: used={_format_bytes(used_val)}, avail={_format_bytes(avail_val)}")

    return "\n".join(lines)


def _format_shares(
    nfs_shares: list[TruenasNfsShareEntry],
    smb_shares: list[TruenasSmbShareEntry],
    share_type: str | None,
    smb_sessions: list[TruenasSmbSessionEntry] | None = None,
) -> str:
    """Format NFS and SMB shares into a readable string."""
    lines: list[str] = []

    if share_type in (None, "nfs"):
        lines.append(f"NFS shares ({len(nfs_shares)}):")
        if not nfs_shares:
            lines.append("  (none)")
        for share in nfs_shares:
            path = share.get("path", "?")
            enabled = share.get("enabled", False)
            ro = share.get("ro", False)
            networks = share.get("networks", [])
            comment = share.get("comment", "")
            status = "enabled" if enabled else "DISABLED"
            ro_str = ", read-only" if ro else ""
            net_str = f", networks={networks}" if networks else ""
            comment_str = f" — {comment}" if comment else ""
            lines.append(f"  {path} ({status}{ro_str}{net_str}){comment_str}")

    if share_type in (None, "smb"):
        lines.append(f"SMB shares ({len(smb_shares)}):")
        if not smb_shares:
            lines.append("  (none)")
        for smb_share in smb_shares:
            name = smb_share.get("name", "?")
            path = smb_share.get("path", "?")
            enabled = smb_share.get("enabled", False)
            ro = smb_share.get("ro", False)
            comment = smb_share.get("comment", "")
            status = "enabled" if enabled else "DISABLED"
            ro_str = ", read-only" if ro else ""
            comment_str = f" — {comment}" if comment else ""
            lines.append(f"  {name} -> {path} ({status}{ro_str}){comment_str}")

    if smb_sessions is not None:
        lines.append(f"\nActive SMB sessions ({len(smb_sessions)}):")
        if not smb_sessions:
            lines.append("  (none)")
        for session in smb_sessions:
            username = session.get("username", "?")
            remote = session.get("remote_machine", "?")
            dialect = session.get("session_dialect", "")

            # Extract cipher strings from nested dicts
            enc_dict = session.get("encryption", {})
            enc_cipher = enc_dict.get("cipher", "-") if isinstance(enc_dict, dict) else str(enc_dict)
            sign_dict = session.get("signing", {})
            sign_cipher = sign_dict.get("cipher", "-") if isinstance(sign_dict, dict) else str(sign_dict)

            details: list[str] = []
            if dialect:
                details.append(dialect)
            if enc_cipher and enc_cipher != "-":
                details.append(f"encrypt={enc_cipher}")
            if sign_cipher and sign_cipher != "-":
                details.append(f"sign={sign_cipher}")
            detail_str = f" ({', '.join(details)})" if details else ""

            # Show which shares this session is connected to
            share_conns = session.get("share_connections", [])
            share_names = [
                sc.get("service", "?") for sc in share_conns if isinstance(sc, dict) and sc.get("service") != "IPC$"
            ]
            shares_str = f" -> {', '.join(share_names)}" if share_names else ""

            lines.append(f"  {username}@{remote}{detail_str}{shares_str}")

    return "\n".join(lines)


def _format_snapshots(
    snapshots: list[TruenasSnapshotEntry],
    tasks: list[TruenasSnapshotTaskEntry],
    replications: list[TruenasReplicationEntry],
) -> str:
    """Format ZFS snapshots, snapshot tasks, and replication status."""
    lines: list[str] = []

    # Recent snapshots
    lines.append(f"Recent snapshots ({len(snapshots)}):")
    if not snapshots:
        lines.append("  (none)")
    for snap in snapshots[:MAX_SNAPSHOTS]:
        snap_id = snap.get("id", "unknown")
        lines.append(f"  {snap_id}")
    if len(snapshots) > MAX_SNAPSHOTS:
        lines.append(f"  (showing first {MAX_SNAPSHOTS} of {len(snapshots)})")

    # Snapshot schedules
    lines.append(f"\nSnapshot schedules ({len(tasks)}):")
    if not tasks:
        lines.append("  (none)")
    for task in tasks:
        dataset = task.get("dataset", "?")
        enabled = task.get("enabled", False)
        lifetime = f"{task.get('lifetime_value', '?')}{task.get('lifetime_unit', '?')}"
        recursive = task.get("recursive", False)
        schedule = task.get("schedule", {})
        sched_str = _format_cron_schedule(schedule)
        status = "enabled" if enabled else "DISABLED"
        rec_str = ", recursive" if recursive else ""
        lines.append(f"  {dataset} ({status}, keep {lifetime}{rec_str}) — {sched_str}")

    # Replication tasks
    lines.append(f"\nReplication tasks ({len(replications)}):")
    if not replications:
        lines.append("  (none)")
    for repl in replications:
        name = repl.get("name", "?")
        enabled = repl.get("enabled", False)
        direction = repl.get("direction", "?")
        transport = repl.get("transport", "?")
        sources = repl.get("source_datasets", [])
        target = repl.get("target_dataset", "?")
        state = repl.get("state", {})
        state_str = state.get("state", "UNKNOWN") if isinstance(state, dict) else "UNKNOWN"  # pyright: ignore[reportAny]
        status = "enabled" if enabled else "DISABLED"
        src_str = ", ".join(str(s) for s in sources) if sources else "?"
        lines.append(f"  {name} ({status}, {direction}, {transport}): {src_str} -> {target} [state={state_str}]")

    return "\n".join(lines)


def _format_cron_schedule(schedule: dict[str, str]) -> str:
    """Format a TrueNAS cron schedule dict into a human-readable string."""
    if not schedule:
        return "no schedule"
    minute = schedule.get("minute", "*")
    hour = schedule.get("hour", "*")
    dom = schedule.get("dom", "*")
    month = schedule.get("month", "*")
    dow = schedule.get("dow", "*")
    return f"{minute} {hour} {dom} {month} {dow}"


def _format_system_status(
    info: TruenasSystemInfo,
    alerts: list[TruenasAlertEntry],
    jobs: list[TruenasJobEntry],
    disks: list[TruenasDiskEntry],
) -> str:
    """Format TrueNAS system info, alerts, jobs, and disk inventory."""
    lines: list[str] = ["TrueNAS System Status:"]

    # System info
    version = info.get("version", "?")
    hostname = info.get("hostname", "?")
    uptime_secs = info.get("uptime_seconds", 0)
    uptime_days = int(uptime_secs) // 86400 if uptime_secs else 0
    uptime_hours = (int(uptime_secs) % 86400) // 3600 if uptime_secs else 0
    product = info.get("system_product", "?")
    mem = info.get("physmem", 0)
    cores = info.get("cores", 0)
    loadavg = info.get("loadavg", [])
    ecc = info.get("ecc_memory", False)

    lines.append(f"  Version: {version}")
    lines.append(f"  Hostname: {hostname}")
    lines.append(f"  Uptime: {uptime_days}d {uptime_hours}h")
    lines.append(f"  Hardware: {product}")
    lines.append(f"  Memory: {_format_bytes(mem)} ({'ECC' if ecc else 'non-ECC'})")
    if cores:
        lines.append(f"  CPU cores: {cores}")
    if loadavg and isinstance(loadavg, list) and len(loadavg) >= 3:
        lines.append(f"  Load avg: {loadavg[0]:.2f}, {loadavg[1]:.2f}, {loadavg[2]:.2f}")

    # Alerts
    active_alerts = [a for a in alerts if not a.get("dismissed", False)]
    lines.append(f"\nAlerts ({len(active_alerts)} active, {len(alerts)} total):")
    if not active_alerts:
        lines.append("  (none active)")
    for alert in active_alerts:
        level = alert.get("level", "?")
        formatted = alert.get("formatted", alert.get("klass", "?"))
        lines.append(f"  [{level}] {formatted}")

    # Running jobs
    running_jobs = [j for j in jobs if j.get("state") == "RUNNING"]
    lines.append(f"\nRunning jobs ({len(running_jobs)}):")
    if not running_jobs:
        lines.append("  (none)")
    for job in running_jobs[:MAX_JOBS]:
        method = job.get("method", "?")
        progress = job.get("progress", {})
        pct = progress.get("percent", 0) if isinstance(progress, dict) else 0  # pyright: ignore[reportAny]
        desc = progress.get("description", "") if isinstance(progress, dict) else ""  # pyright: ignore[reportAny]
        desc_str = f" — {desc}" if desc else ""
        lines.append(f"  {method} ({pct}%{desc_str})")

    # Disk inventory
    lines.append(f"\nDisks ({len(disks)}):")
    if not disks:
        lines.append("  (none)")
    for disk in sorted(disks, key=lambda d: d.get("name", "")):
        name = disk.get("name", "?")
        model = disk.get("model", "?")
        serial = disk.get("serial", "?")
        dtype = disk.get("type", "?")
        size = disk.get("size", 0)
        pool = disk.get("pool", "")
        standby = disk.get("hddstandby", "")
        pool_str = f", pool={pool}" if pool else ""
        standby_str = f", standby={standby}" if standby and standby != "ALWAYS ON" else ""
        lines.append(f"  {name}: {model} ({dtype}, {_format_bytes(size)}, serial={serial}{pool_str}{standby_str})")

    return "\n".join(lines)


def _format_apps(apps: list[TruenasAppEntry]) -> str:
    """Format TrueNAS apps listing."""
    if not apps:
        return "No apps found."

    running = [a for a in apps if a.get("state") == "RUNNING"]
    stopped = [a for a in apps if a.get("state") != "RUNNING"]

    lines: list[str] = [f"Found {len(apps)} app(s) ({len(running)} running, {len(stopped)} stopped):\n"]

    for app in sorted(apps, key=lambda a: a.get("name", "")):
        name = app.get("name", "?")
        state = app.get("state", "?")
        version = app.get("human_version", app.get("version", "?"))
        upgrade = app.get("upgrade_available", False)
        status_marker = "+" if state == "RUNNING" else "-"
        upgrade_str = " [upgrade available]" if upgrade else ""
        lines.append(f"  {status_marker} {name} ({state}) v{version}{upgrade_str}")

    return "\n".join(lines)


# --- Input schemas ---


class PoolStatusInput(BaseModel):
    """Input for fetching TrueNAS ZFS pool status."""

    pass


class ListSharesInput(BaseModel):
    """Input for listing TrueNAS NFS and SMB shares."""

    share_type: str | None = Field(
        default=None,
        description="Filter by share type: 'nfs' or 'smb'. Omit to list both.",
    )
    include_sessions: bool = Field(
        default=False,
        description=(
            "Include active SMB sessions (connected clients). "
            "Set to true when asking about active connections, who is connected, "
            "or which shares have active sessions."
        ),
    )


class SnapshotsInput(BaseModel):
    """Input for listing TrueNAS ZFS snapshots, schedules, and replication."""

    dataset: str | None = Field(
        default=None,
        description="Filter snapshots by dataset name (e.g. 'tank/media'). Omit to list all.",
    )
    limit: int = Field(
        default=50,
        description="Maximum number of snapshots to return (default 50).",
        ge=1,
        le=200,
    )


class SystemStatusInput(BaseModel):
    """Input for fetching TrueNAS system status."""

    pass


class AppsInput(BaseModel):
    """Input for listing TrueNAS apps."""

    pass


# --- Tool descriptions ---


TOOL_DESCRIPTION_POOL_STATUS = HOMELAB_CONTEXT + (
    "Get ZFS pool health and dataset space usage from TrueNAS. "
    "Use this to answer questions like 'is the tank pool healthy?', "
    "'how much space is left on the NAS?', 'any degraded pools?', "
    "'how big is my photos dataset?', 'how much space does tank/media use?', "
    "or 'show ZFS pool usage'.\n\n"
    "Returns pool status (ONLINE/DEGRADED/FAULTED), health flag, "
    "size/used/free space, disk topology, and per-dataset used/available for "
    "each top-level dataset in each pool."
)

TOOL_DESCRIPTION_LIST_SHARES = HOMELAB_CONTEXT + (
    "List NFS and SMB shares configured on TrueNAS, and optionally show active SMB sessions. "
    "Use this to answer questions like 'what NFS shares exist?', "
    "'is the paperless share enabled?', 'which SMB shares are configured?', "
    "'who is connected to the NAS?', 'which shares have active sessions?', "
    "or 'show all NAS shares'.\n\n"
    "Returns share path, enabled/disabled status, read-only flag, "
    "allowed networks/hosts, and comments. Can filter by NFS or SMB.\n\n"
    "Set include_sessions=true for questions about active connections or connected clients."
)

TOOL_DESCRIPTION_SNAPSHOTS = HOMELAB_CONTEXT + (
    "List ZFS snapshots, snapshot schedules, and replication tasks on TrueNAS. "
    "Use this to answer questions like 'when was the last snapshot of tank/media?', "
    "'is replication running?', 'what snapshot schedules exist?', "
    "or 'how many snapshots does tank have?'.\n\n"
    "Returns recent snapshots (optionally filtered by dataset), "
    "periodic snapshot task schedules with retention policy, "
    "and replication task status with source/target datasets."
)

TOOL_DESCRIPTION_SYSTEM_STATUS = HOMELAB_CONTEXT + (
    "Get TrueNAS system information, alerts, running jobs, and disk inventory. "
    "Use this to answer questions like 'any TrueNAS alerts?', "
    "'what version is TrueNAS running?', 'what disks does TrueNAS have?', "
    "'are any jobs running?', or 'show NAS system info'.\n\n"
    "Returns TrueNAS version, hostname, uptime, hardware info, "
    "active alerts, running jobs with progress, and disk inventory "
    "(model, type, serial, size, pool assignment, standby timer)."
)

TOOL_DESCRIPTION_APPS = HOMELAB_CONTEXT + (
    "List apps installed on TrueNAS SCALE with their running state. "
    "Use this to answer questions like 'what apps are running on TrueNAS?', "
    "'is Alloy running?', 'is the disk-status-exporter deployed?', "
    "or 'any stopped TrueNAS apps?'.\n\n"
    "Returns app name, state (RUNNING/STOPPED/DEPLOYING), version, "
    "and whether an upgrade is available."
)


# --- Tools ---


@tool("truenas_pool_status", args_schema=PoolStatusInput)  # pyright: ignore[reportUnknownParameterType]
async def truenas_pool_status() -> str:
    """Get ZFS pool health and dataset usage. See TOOL_DESCRIPTION_POOL_STATUS."""
    settings = get_settings()
    if not settings.truenas_url:
        raise ToolException("TrueNAS is not configured (TRUENAS_URL is empty).")

    logger.info("Fetching TrueNAS pool status")

    try:
        pools_raw = await _truenas_get("/pool")
        pools: list[TruenasPoolEntry] = pools_raw if isinstance(pools_raw, list) else []

        datasets_raw = await _truenas_get("/pool/dataset")
        datasets: list[TruenasDatasetEntry] = datasets_raw if isinstance(datasets_raw, list) else []
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to TrueNAS at {settings.truenas_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"TrueNAS request timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e
    except httpx.HTTPStatusError as e:
        raise ToolException(f"TrueNAS API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e

    return _format_pools(pools, datasets)


truenas_pool_status.description = TOOL_DESCRIPTION_POOL_STATUS
truenas_pool_status.handle_tool_error = True


@tool("truenas_list_shares", args_schema=ListSharesInput)  # pyright: ignore[reportUnknownParameterType]
async def truenas_list_shares(
    share_type: str | None = None,
    include_sessions: bool = False,
) -> str:
    """List NFS and SMB shares. See TOOL_DESCRIPTION_LIST_SHARES."""
    settings = get_settings()
    if not settings.truenas_url:
        raise ToolException("TrueNAS is not configured (TRUENAS_URL is empty).")

    logger.info("Listing TrueNAS shares (type=%s, include_sessions=%s)", share_type, include_sessions)

    nfs_shares: list[TruenasNfsShareEntry] = []
    smb_shares: list[TruenasSmbShareEntry] = []
    smb_sessions: list[TruenasSmbSessionEntry] | None = None

    try:
        if share_type in (None, "nfs"):
            nfs_raw = await _truenas_get("/sharing/nfs")
            nfs_shares = nfs_raw if isinstance(nfs_raw, list) else []

        if share_type in (None, "smb"):
            smb_raw = await _truenas_get("/sharing/smb")
            smb_shares = smb_raw if isinstance(smb_raw, list) else []

        if include_sessions and share_type != "nfs":
            try:
                sessions_raw = await _truenas_post("/smb/status")
                smb_sessions = sessions_raw if isinstance(sessions_raw, list) else []
            except (httpx.HTTPStatusError, httpx.ConnectError):
                logger.warning("Could not fetch SMB sessions from /smb/status")
                smb_sessions = []
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to TrueNAS at {settings.truenas_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"TrueNAS request timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e
    except httpx.HTTPStatusError as e:
        raise ToolException(f"TrueNAS API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e

    return _format_shares(nfs_shares, smb_shares, share_type, smb_sessions)


truenas_list_shares.description = TOOL_DESCRIPTION_LIST_SHARES
truenas_list_shares.handle_tool_error = True


SNAPSHOT_TIMEOUT_SECONDS = 30


@tool("truenas_snapshots", args_schema=SnapshotsInput)  # pyright: ignore[reportUnknownParameterType]
async def truenas_snapshots(dataset: str | None = None, limit: int = 50) -> str:
    """List ZFS snapshots, schedules, and replication. See TOOL_DESCRIPTION_SNAPSHOTS."""
    settings = get_settings()
    if not settings.truenas_url:
        raise ToolException("TrueNAS is not configured (TRUENAS_URL is empty).")

    logger.info("Fetching TrueNAS snapshots (dataset=%s, limit=%d)", dataset, limit)

    try:
        # Build TrueNAS query-filters and query-options as JSON query params.
        # The /zfs/snapshot endpoint is a query-type method that supports:
        #   query-filters: [[field, op, value], ...]
        #   query-options: {limit, offset, order_by, select, extra}
        # Plain "limit=50" params are ignored; must use the JSON format.
        snap_params: dict[str, str] = {}

        query_filters: list[list[str]] = []
        if dataset:
            query_filters.append(["dataset", "=", dataset])
        if query_filters:
            snap_params["query-filters"] = json.dumps(query_filters)

        query_options: dict[str, object] = {
            "limit": limit,
            "order_by": ["-id"],
            "select": ["id", "name", "dataset", "snapshot_name", "type"],
        }
        snap_params["query-options"] = json.dumps(query_options)

        snaps_raw = await _truenas_get("/zfs/snapshot", params=snap_params, timeout=SNAPSHOT_TIMEOUT_SECONDS)
        snapshots: list[TruenasSnapshotEntry] = snaps_raw if isinstance(snaps_raw, list) else []

        tasks_raw = await _truenas_get("/pool/snapshottask")
        tasks: list[TruenasSnapshotTaskEntry] = tasks_raw if isinstance(tasks_raw, list) else []

        repl_raw = await _truenas_get("/replication")
        replications: list[TruenasReplicationEntry] = repl_raw if isinstance(repl_raw, list) else []
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to TrueNAS at {settings.truenas_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"TrueNAS request timed out after {SNAPSHOT_TIMEOUT_SECONDS}s: {e}") from e
    except httpx.HTTPStatusError as e:
        raise ToolException(f"TrueNAS API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e

    return _format_snapshots(snapshots, tasks, replications)


truenas_snapshots.description = TOOL_DESCRIPTION_SNAPSHOTS
truenas_snapshots.handle_tool_error = True


@tool("truenas_system_status", args_schema=SystemStatusInput)  # pyright: ignore[reportUnknownParameterType]
async def truenas_system_status() -> str:
    """Get TrueNAS system info, alerts, jobs, disks. See TOOL_DESCRIPTION_SYSTEM_STATUS."""
    settings = get_settings()
    if not settings.truenas_url:
        raise ToolException("TrueNAS is not configured (TRUENAS_URL is empty).")

    logger.info("Fetching TrueNAS system status")

    try:
        info_raw = await _truenas_get("/system/info")
        info: TruenasSystemInfo = info_raw if isinstance(info_raw, dict) else {}  # type: ignore[assignment]

        alerts_raw = await _truenas_get("/alert/list")
        alerts: list[TruenasAlertEntry] = alerts_raw if isinstance(alerts_raw, list) else []

        jobs_raw = await _truenas_get("/core/get_jobs")
        jobs: list[TruenasJobEntry] = jobs_raw if isinstance(jobs_raw, list) else []

        disks_raw = await _truenas_get("/disk")
        disks: list[TruenasDiskEntry] = disks_raw if isinstance(disks_raw, list) else []
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to TrueNAS at {settings.truenas_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"TrueNAS request timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e
    except httpx.HTTPStatusError as e:
        raise ToolException(f"TrueNAS API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e

    return _format_system_status(info, alerts, jobs, disks)


truenas_system_status.description = TOOL_DESCRIPTION_SYSTEM_STATUS
truenas_system_status.handle_tool_error = True


@tool("truenas_apps", args_schema=AppsInput)  # pyright: ignore[reportUnknownParameterType]
async def truenas_apps() -> str:
    """List TrueNAS SCALE apps. See TOOL_DESCRIPTION_APPS."""
    settings = get_settings()
    if not settings.truenas_url:
        raise ToolException("TrueNAS is not configured (TRUENAS_URL is empty).")

    logger.info("Listing TrueNAS apps")

    try:
        apps_raw = await _truenas_get("/app")
        apps: list[TruenasAppEntry] = apps_raw if isinstance(apps_raw, list) else []
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to TrueNAS at {settings.truenas_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"TrueNAS request timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e
    except httpx.HTTPStatusError as e:
        raise ToolException(f"TrueNAS API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e

    return _format_apps(apps)


truenas_apps.description = TOOL_DESCRIPTION_APPS
truenas_apps.handle_tool_error = True
