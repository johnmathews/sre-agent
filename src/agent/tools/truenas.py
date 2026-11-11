"""LangChain tools for querying the TrueNAS SCALE REST API."""

import asyncio
import logging
import ssl
from datetime import UTC, datetime
from typing import TypedDict
from urllib.parse import urlsplit

import httpx
from langchain_core.tools import ToolException, tool  # pyright: ignore[reportUnknownVariableType]
from pydantic import BaseModel, Field

from src.agent.tools import HOMELAB_CONTEXT
from src.agent.tools._common import format_age, format_bytes, format_utc
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

HTTPS_REQUIRED_MESSAGE = "TrueNAS URL must be https; refusing to send the API key over plain http"


def _truenas_api_url(path: str) -> str:
    """Build the REST v2.0 URL, refusing any base URL that is not https.

    The API key is a bearer token: sent over plain http (or ws://) it can be read on the wire, and
    TrueNAS revokes a key it sees used insecurely. The check runs before any client is built.
    """
    base = get_settings().truenas_url
    if urlsplit(base).scheme.lower() != "https":
        raise ToolException(HTTPS_REQUIRED_MESSAGE)
    return f"{base}/api/v2.0{path}"


async def _truenas_get(
    path: str,
    params: dict[str, str] | None = None,
    timeout: int | None = None,
) -> object:
    """Make an authenticated GET request to the TrueNAS SCALE API.

    TrueNAS returns plain JSON (arrays, objects or, for ``count=true``, a bare integer),
    NOT wrapped in ``{"data": ...}``. Raises ``ToolException`` for a non-https URL.
    """
    url = _truenas_api_url(path)
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

    Some TrueNAS middleware methods (e.g. smb.status) require POST. Raises ``ToolException``
    for a non-https URL.
    """
    url = _truenas_api_url(path)
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
    """One entry of GET /zfs/snapshot. ``properties`` maps a ZFS property name to
    ``{"value": str, "rawvalue": str, ...}``; ``createtxg`` is a string, never sort on it."""

    id: str
    name: str
    dataset: str
    pool: str
    snapshot_name: str
    createtxg: str
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
    state: dict[str, object]
    """Last run: ``{"state": "FINISHED", "datetime": {"$date": <ms epoch>}}``; may be absent."""


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
        lines.append(f"    Size: {format_bytes(size)}")
        lines.append(f"    Used: {format_bytes(allocated)} ({pct:.1f}%)")
        lines.append(f"    Free: {format_bytes(free)}")

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
                lines.append(f"      {ds_id}: used={format_bytes(used_val)}, avail={format_bytes(avail_val)}")

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


SnapshotCount = int | str
"""A snapshot count, or the reason it is unavailable."""


def _state_datetime(state: dict[str, object]) -> datetime | None:
    """The time in a TrueNAS job state: ``{"datetime": {"$date": <ms epoch>}}``."""
    raw = state.get("datetime")
    millis = raw.get("$date") if isinstance(raw, dict) else None  # pyright: ignore[reportUnknownMemberType]
    if isinstance(millis, int | float) and not isinstance(millis, bool):
        return datetime.fromtimestamp(millis / 1000, tz=UTC)
    return None


def _format_state(state: object, now: datetime) -> str:
    """``FINISHED at 2026-11-11 00:00:05Z (13h ago)``, or say that TrueNAS did not report it."""
    if not isinstance(state, dict) or not state.get("state"):
        return "not reported by TrueNAS"
    text = str(state.get("state"))  # pyright: ignore[reportUnknownArgumentType]
    when = _state_datetime(state)  # pyright: ignore[reportUnknownArgumentType]
    text += f" at {format_utc(when)} ({format_age(when, now)})" if when else " (time not reported)"
    error = state.get("error")  # pyright: ignore[reportUnknownMemberType]
    if error:
        text += f" — error: {str(error)[:200]}"  # pyright: ignore[reportUnknownArgumentType]
    return text


def _format_count(count: SnapshotCount) -> str:
    return f"{count} snapshots" if isinstance(count, int) else f"unavailable: {count}"


def _property_raw(snapshot: TruenasSnapshotEntry, name: str) -> str | None:
    """``properties[name].rawvalue`` of a snapshot entry, if present."""
    prop = snapshot.get("properties", {}).get(name)
    raw = prop.get("rawvalue") if isinstance(prop, dict) else None  # pyright: ignore[reportUnknownMemberType]
    return None if raw is None else str(raw)  # pyright: ignore[reportUnknownArgumentType]


def _creation_epoch(snapshot: TruenasSnapshotEntry) -> int | None:
    """Creation time in epoch seconds from ``properties.creation.rawvalue`` (a string)."""
    raw = _property_raw(snapshot, "creation")
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def _snapshot_name(snapshot: TruenasSnapshotEntry) -> str:
    return snapshot.get("name") or snapshot.get("id") or "unknown"


def _sort_newest_first(snapshots: list[TruenasSnapshotEntry]) -> list[TruenasSnapshotEntry]:
    """Newest first by creation time; entries without one go last, by name descending.

    Never sorts on ``createtxg``: TrueNAS returns it as a string, so "99" would sort above "987614".
    """

    def key(snapshot: TruenasSnapshotEntry) -> tuple[bool, int, str]:
        created = _creation_epoch(snapshot)
        return (created is not None, created or 0, _snapshot_name(snapshot))

    return sorted(snapshots, key=key, reverse=True)


def _format_snapshot_listing(dataset: str, snapshots: list[TruenasSnapshotEntry] | str, limit: int) -> list[str]:
    """The ``list_snapshots`` section: newest first, capped at ``limit``."""
    if isinstance(snapshots, str):
        return [f"Snapshots of {dataset}: unavailable: {snapshots}"]
    total = len(snapshots)
    shown = _sort_newest_first(snapshots)[:limit]
    header = f"showing newest {len(shown)} of {total}" if len(shown) < total else f"all {total}"
    lines = [f"Snapshots of {dataset} ({header}):"]
    if not shown:
        lines.append("  (none)")
    for snap in shown:
        created = _creation_epoch(snap)
        created_str = f"created {format_utc(created)}" if created is not None else "creation time not reported"
        used_raw = _property_raw(snap, "used")
        used_str = f", used {format_bytes(int(used_raw))}" if used_raw and used_raw.isdigit() else ""
        lines.append(f"  {_snapshot_name(snap)} ({created_str}{used_str})")
    return lines


def _format_snapshots(
    tasks: list[TruenasSnapshotTaskEntry],
    replications: list[TruenasReplicationEntry],
    pool_counts: dict[str, SnapshotCount],
    dataset_counts: dict[str, SnapshotCount],
    now: datetime,
    dataset: str | None = None,
    listing: list[str] | None = None,
) -> str:
    """Format snapshot counts, periodic snapshot tasks with their last run, and replication.

    ``pool_counts`` and ``dataset_counts`` come from ``GET /zfs/snapshot?count=true``; a dataset
    count covers that dataset only, not its children.
    """
    lines: list[str] = ["Snapshot counts (from TrueNAS count queries):"]
    if not pool_counts and not dataset_counts:
        lines.append("  (no pools found)")
    for pool, count in pool_counts.items():
        lines.append(f"  pool {pool}: {_format_count(count)}")
    if dataset and dataset in dataset_counts:
        lines.append(
            f"  dataset {dataset} (this dataset only, not its children): {_format_count(dataset_counts[dataset])}"
        )

    # Periodic snapshot tasks
    lines.append(f"\nPeriodic snapshot tasks ({len(tasks)}):")
    if not tasks:
        lines.append("  (none)")
    for task in tasks:
        task_dataset = task.get("dataset", "?")
        status = "enabled" if task.get("enabled", False) else "DISABLED"
        rec_str = ", recursive" if task.get("recursive", False) else ""
        retention = f"{task.get('lifetime_value', '?')} {task.get('lifetime_unit', '?')}"
        sched_str = _format_cron_schedule(task.get("schedule", {}))
        lines.append(f"  {task_dataset} ({status}{rec_str}, keep {retention}, schedule {sched_str})")
        lines.append(f"    last run: {_format_state(task.get('state'), now)}")
        if task_dataset in dataset_counts:
            lines.append(f"    snapshots of {task_dataset} itself: {_format_count(dataset_counts[task_dataset])}")

    # Replication tasks
    lines.append(f"\nReplication tasks ({len(replications)}):")
    if not replications:
        lines.append("  (none)")
    for repl in replications:
        name = repl.get("name", "?")
        status = "enabled" if repl.get("enabled", False) else "DISABLED"
        direction = repl.get("direction", "?")
        transport = repl.get("transport", "?")
        sources = repl.get("source_datasets", [])
        target = repl.get("target_dataset", "?")
        src_str = ", ".join(str(s) for s in sources) if sources else "?"
        lines.append(f"  {name} ({status}, {direction}, {transport}): {src_str} -> {target}")
        lines.append(f"    last run: {_format_state(repl.get('state'), now)}")

    if listing is not None:
        lines.append("")
        lines.extend(listing)

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
    lines.append(f"  Memory: {format_bytes(mem)} ({'ECC' if ecc else 'non-ECC'})")
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
        lines.append(f"  {name}: {model} ({dtype}, {format_bytes(size)}, serial={serial}{pool_str}{standby_str})")

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
    """Input for TrueNAS snapshot counts, snapshot tasks, replication and an optional listing."""

    dataset: str | None = Field(
        default=None,
        description=(
            "Dataset name (e.g. 'tank/media'). Adds that dataset's own snapshot count and its pool's. "
            "Required when list_snapshots is true."
        ),
    )
    list_snapshots: bool = Field(
        default=False,
        description=(
            "List the dataset's snapshots, newest first (needs dataset). Slow: about 20 s. "
            "Counts and last-run times are fast and are always returned without it."
        ),
    )
    limit: int = Field(
        default=50,
        description="With list_snapshots, how many of the newest snapshots to show (default 50).",
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
    "'is the document-store share enabled?', 'which SMB shares are configured?', "
    "'who is connected to the NAS?', 'which shares have active sessions?', "
    "or 'show all NAS shares'.\n\n"
    "Returns share path, enabled/disabled status, read-only flag, "
    "allowed networks/hosts, and comments. Can filter by NFS or SMB.\n\n"
    "Set include_sessions=true for questions about active connections or connected clients."
)

TOOL_DESCRIPTION_SNAPSHOTS = HOMELAB_CONTEXT + (
    "Report ZFS snapshot counts, periodic snapshot tasks with their last run, and replication tasks on "
    "TrueNAS. Use this to answer questions like 'when did the tank/media snapshot task last run?', "
    "'how many snapshots does tank have?', 'is replication running?' or 'what snapshot schedules exist?'.\n\n"
    "Always returns (fast): the snapshot count per pool; each periodic snapshot task's last run (state and "
    "time), schedule, retention, recursive flag and the snapshot count of its dataset; and each replication "
    "task's state with its time. Give dataset (e.g. 'tank/media') to add that dataset's own count and its "
    "pool's.\n\n"
    "Set list_snapshots=true together with dataset to also list that dataset's snapshots, newest first, "
    "up to limit. The listing takes about 20 s."
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


SNAPSHOT_COUNT_TIMEOUT_SECONDS = 30
SNAPSHOT_LIST_TIMEOUT_SECONDS = 45


def _describe_error(exc: Exception, timeout: int) -> str:
    """A short reason for a failed TrueNAS request, for an 'unavailable: <reason>' line."""
    if isinstance(exc, httpx.TimeoutException):
        return f"TrueNAS request timed out after {timeout}s"
    if isinstance(exc, httpx.ConnectError):
        return f"cannot connect to TrueNAS: {exc}"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"TrueNAS API error: HTTP {exc.response.status_code} - {exc.response.text[:200]}"
    if isinstance(exc, httpx.HTTPError):
        return f"TrueNAS request failed ({type(exc).__name__}: {exc})"
    if isinstance(exc, ValueError):
        return "TrueNAS response was not JSON"
    return str(exc)


async def _snapshot_count(params: dict[str, str]) -> SnapshotCount:
    """``GET /zfs/snapshot?count=true&<field>=<value>``: TrueNAS answers with a bare integer.

    Returns the count, or the reason it is unavailable (also logged).
    """
    try:
        raw = await _truenas_get(
            "/zfs/snapshot", params={"count": "true", **params}, timeout=SNAPSHOT_COUNT_TIMEOUT_SECONDS
        )
    except (httpx.HTTPError, ToolException, ValueError) as e:
        reason = _describe_error(e, SNAPSHOT_COUNT_TIMEOUT_SECONDS)
        logger.warning("TrueNAS snapshot count failed (%s): %s", params, reason)
        return reason
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    logger.warning("TrueNAS snapshot count (%s) returned %r, not an integer", params, raw)
    return f"unexpected TrueNAS response {str(raw)[:80]!r} (expected an integer)"


async def _snapshot_listing(dataset: str) -> list[TruenasSnapshotEntry] | str:
    """``GET /zfs/snapshot?dataset=<d>`` with no limit (sorted client-side). Slow: about 20 s.

    Returns the entries, or the reason the listing is unavailable (also logged).
    """
    try:
        raw = await _truenas_get("/zfs/snapshot", params={"dataset": dataset}, timeout=SNAPSHOT_LIST_TIMEOUT_SECONDS)
    except (httpx.HTTPError, ToolException, ValueError) as e:
        reason = _describe_error(e, SNAPSHOT_LIST_TIMEOUT_SECONDS)
        logger.warning("TrueNAS snapshot listing for %s failed: %s", dataset, reason)
        return reason
    if not isinstance(raw, list):
        logger.warning("TrueNAS snapshot listing for %s returned %s, not a list", dataset, type(raw).__name__)
        return "unexpected TrueNAS response (expected a list)"
    entries: list[TruenasSnapshotEntry] = [entry for entry in raw if isinstance(entry, dict)]  # type: ignore[misc]  # pyright: ignore[reportUnknownVariableType]
    return entries


async def _no_listing() -> None:
    return None


@tool("truenas_snapshots", args_schema=SnapshotsInput)  # pyright: ignore[reportUnknownParameterType]
async def truenas_snapshots(dataset: str | None = None, list_snapshots: bool = False, limit: int = 50) -> str:
    """Snapshot counts, snapshot tasks and replication. See TOOL_DESCRIPTION_SNAPSHOTS."""
    settings = get_settings()
    if not settings.truenas_url:
        raise ToolException("TrueNAS is not configured (TRUENAS_URL is empty).")
    if list_snapshots and not dataset:
        raise ToolException(
            "list_snapshots needs dataset (e.g. 'tank/media'). "
            + "Leave list_snapshots off for counts and last-run times across all pools."
        )

    logger.info(
        "Fetching TrueNAS snapshot status (dataset=%s, list_snapshots=%s, limit=%d)", dataset, list_snapshots, limit
    )

    try:
        tasks_raw = await _truenas_get("/pool/snapshottask")
        repl_raw = await _truenas_get("/replication")
        pools_raw = await _truenas_get("/pool")
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to TrueNAS at {settings.truenas_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"TrueNAS request timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e
    except httpx.HTTPStatusError as e:
        raise ToolException(f"TrueNAS API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e

    tasks: list[TruenasSnapshotTaskEntry] = tasks_raw if isinstance(tasks_raw, list) else []
    replications: list[TruenasReplicationEntry] = repl_raw if isinstance(repl_raw, list) else []
    pools: list[TruenasPoolEntry] = pools_raw if isinstance(pools_raw, list) else []

    pool_names: list[str] = [p["name"] for p in pools if isinstance(p, dict) and p.get("name")]
    count_datasets: list[str] = [t["dataset"] for t in tasks if isinstance(t, dict) and t.get("dataset")]
    if dataset:
        pool_of_dataset = dataset.split("/", 1)[0]
        if pool_of_dataset not in pool_names:
            pool_names.append(pool_of_dataset)
        count_datasets.append(dataset)
    pool_names = list(dict.fromkeys(pool_names))
    count_datasets = list(dict.fromkeys(count_datasets))

    count_jobs = [_snapshot_count({"pool": p}) for p in pool_names]
    count_jobs += [_snapshot_count({"dataset": d}) for d in count_datasets]
    listing_job = _snapshot_listing(dataset) if list_snapshots and dataset else _no_listing()
    counts, listing_raw = await asyncio.gather(asyncio.gather(*count_jobs), listing_job)

    pool_counts = dict(zip(pool_names, counts[: len(pool_names)], strict=True))
    dataset_counts = dict(zip(count_datasets, counts[len(pool_names) :], strict=True))
    listing = _format_snapshot_listing(dataset, listing_raw, limit) if dataset and listing_raw is not None else None

    return _format_snapshots(
        tasks, replications, pool_counts, dataset_counts, datetime.now(UTC), dataset=dataset, listing=listing
    )


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
