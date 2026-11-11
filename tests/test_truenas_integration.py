"""Integration tests for TrueNAS SCALE tools with mocked HTTP responses."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx
from langchain_core.tools import ToolException

from src.agent.tools.truenas import (
    _truenas_get,
    _truenas_post,
    truenas_apps,
    truenas_list_shares,
    truenas_pool_status,
    truenas_snapshots,
    truenas_system_status,
)


@pytest.fixture(autouse=True)
def _use_mock_settings(mock_settings: Any) -> None:
    """Automatically use mock settings for all tests in this module."""


BASE = "https://truenas.test/api/v2.0"


@pytest.mark.integration
class TestTruenasPoolStatus:
    @respx.mock
    async def test_healthy_pool_with_topology(self) -> None:
        respx.get(f"{BASE}/pool").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "name": "tank",
                        "status": "ONLINE",
                        "healthy": True,
                        "size": 16 * 1024**4,
                        "allocated": 8 * 1024**4,
                        "free": 8 * 1024**4,
                        "topology": {
                            "data": [
                                {
                                    "type": "MIRROR",
                                    "children": [
                                        {"disk": "sdf", "status": "ONLINE"},
                                        {"disk": "sdh", "status": "ONLINE"},
                                    ],
                                }
                            ],
                            "special": [
                                {
                                    "type": "MIRROR",
                                    "children": [
                                        {"disk": "sdb", "status": "ONLINE"},
                                        {"disk": "sdd", "status": "ONLINE"},
                                    ],
                                }
                            ],
                        },
                    }
                ],
            )
        )
        respx.get(f"{BASE}/pool/dataset").mock(return_value=httpx.Response(200, json=[]))

        result = await truenas_pool_status.ainvoke({})
        assert "1 pool(s)" in result
        assert "tank" in result
        assert "ONLINE" in result
        assert "HEALTHY" in result
        assert "data (MIRROR): sdf, sdh" in result
        assert "special (MIRROR): sdb, sdd" in result

    @respx.mock
    async def test_connect_error(self) -> None:
        respx.get(f"{BASE}/pool").mock(side_effect=httpx.ConnectError("Connection refused"))

        result = await truenas_pool_status.ainvoke({})
        assert "Cannot connect" in result

    @respx.mock
    async def test_timeout(self) -> None:
        respx.get(f"{BASE}/pool").mock(side_effect=httpx.ReadTimeout("Read timed out"))

        result = await truenas_pool_status.ainvoke({})
        assert "timed out" in result

    @respx.mock
    async def test_auth_error(self) -> None:
        respx.get(f"{BASE}/pool").mock(return_value=httpx.Response(401, text="Not authenticated"))

        result = await truenas_pool_status.ainvoke({})
        assert "401" in result

    @respx.mock
    async def test_sends_bearer_auth(self) -> None:
        route = respx.get(f"{BASE}/pool").mock(return_value=httpx.Response(200, json=[]))
        respx.get(f"{BASE}/pool/dataset").mock(return_value=httpx.Response(200, json=[]))

        await truenas_pool_status.ainvoke({})
        assert route.called
        auth_header = route.calls.last.request.headers["authorization"]
        assert auth_header.startswith("Bearer ")


@pytest.mark.integration
class TestTruenasListShares:
    @respx.mock
    async def test_lists_nfs_and_smb(self) -> None:
        respx.get(f"{BASE}/sharing/nfs").mock(
            return_value=httpx.Response(
                200,
                json=[{"path": "/mnt/tank/media", "enabled": True, "ro": False}],
            )
        )
        respx.get(f"{BASE}/sharing/smb").mock(
            return_value=httpx.Response(
                200,
                json=[{"name": "TimeMachine", "path": "/mnt/tank/tm", "enabled": True}],
            )
        )

        result = await truenas_list_shares.ainvoke({})
        assert "NFS shares (1)" in result
        assert "/mnt/tank/media" in result
        assert "SMB shares (1)" in result
        assert "TimeMachine" in result

    @respx.mock
    async def test_filter_nfs_only(self) -> None:
        respx.get(f"{BASE}/sharing/nfs").mock(return_value=httpx.Response(200, json=[]))

        result = await truenas_list_shares.ainvoke({"share_type": "nfs"})
        assert "NFS shares" in result
        assert "SMB shares" not in result

    @respx.mock
    async def test_filter_smb_only(self) -> None:
        respx.get(f"{BASE}/sharing/smb").mock(return_value=httpx.Response(200, json=[]))

        result = await truenas_list_shares.ainvoke({"share_type": "smb"})
        assert "SMB shares" in result
        assert "NFS shares" not in result

    @respx.mock
    async def test_connect_error(self) -> None:
        respx.get(f"{BASE}/sharing/nfs").mock(side_effect=httpx.ConnectError("Connection refused"))

        result = await truenas_list_shares.ainvoke({})
        assert "Cannot connect" in result

    @respx.mock
    async def test_timeout(self) -> None:
        respx.get(f"{BASE}/sharing/nfs").mock(side_effect=httpx.ReadTimeout("Read timed out"))

        result = await truenas_list_shares.ainvoke({})
        assert "timed out" in result

    @respx.mock
    async def test_sends_bearer_auth(self) -> None:
        route = respx.get(f"{BASE}/sharing/nfs").mock(return_value=httpx.Response(200, json=[]))
        respx.get(f"{BASE}/sharing/smb").mock(return_value=httpx.Response(200, json=[]))

        await truenas_list_shares.ainvoke({})
        assert route.called
        auth_header = route.calls.last.request.headers["authorization"]
        assert auth_header.startswith("Bearer ")

    @respx.mock
    async def test_include_sessions(self) -> None:
        """include_sessions=true fetches active SMB sessions via POST."""
        respx.get(f"{BASE}/sharing/nfs").mock(return_value=httpx.Response(200, json=[]))
        respx.get(f"{BASE}/sharing/smb").mock(
            return_value=httpx.Response(
                200,
                json=[{"name": "media", "path": "/mnt/tank/media", "enabled": True}],
            )
        )
        respx.post(f"{BASE}/smb/status").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "session_id": "123",
                        "username": "john",
                        "remote_machine": "192.0.2.50",
                        "hostname": "ipv4:192.0.2.50:49226",
                        "session_dialect": "SMB3_11",
                        "encryption": {"cipher": "-", "degree": "none"},
                        "signing": {"cipher": "AES-128-GMAC", "degree": "partial"},
                        "share_connections": [
                            {"service": "photos", "machine": "192.0.2.50"},
                            {"service": "IPC$", "machine": "192.0.2.50"},
                        ],
                    },
                ],
            )
        )

        result = await truenas_list_shares.ainvoke({"include_sessions": True})
        assert "Active SMB sessions (1)" in result
        assert "john@192.0.2.50" in result
        assert "SMB3_11" in result
        assert "photos" in result
        # IPC$ should be filtered out
        assert "IPC$" not in result

    @respx.mock
    async def test_sessions_without_flag_omitted(self) -> None:
        """Sessions are not fetched when include_sessions is false (default)."""
        respx.get(f"{BASE}/sharing/nfs").mock(return_value=httpx.Response(200, json=[]))
        respx.get(f"{BASE}/sharing/smb").mock(return_value=httpx.Response(200, json=[]))

        result = await truenas_list_shares.ainvoke({})
        assert "Active SMB sessions" not in result

    @respx.mock
    async def test_sessions_endpoint_failure_graceful(self) -> None:
        """If /smb/status returns an error, sessions degrade gracefully."""
        respx.get(f"{BASE}/sharing/nfs").mock(return_value=httpx.Response(200, json=[]))
        respx.get(f"{BASE}/sharing/smb").mock(return_value=httpx.Response(200, json=[]))
        respx.post(f"{BASE}/smb/status").mock(return_value=httpx.Response(404, text="Not Found"))

        result = await truenas_list_shares.ainvoke({"include_sessions": True})
        assert "Active SMB sessions (0)" in result


# --- TrueNAS REST shapes for the snapshot tool ---
# GET /zfs/snapshot?count=true&<field>=<value> answers with a bare integer.
# /pool/snapshottask entries carry an optional state {"state": ..., "datetime": {"$date": <ms epoch>}}.
# /zfs/snapshot entries carry createtxg as a string and properties.<name>.rawvalue as strings.

TASK_LAST_RUN_MS = 1794268805000  # 2026-11-10 00:00:05Z


def _pool(name: str) -> dict[str, Any]:
    return {"id": 1, "name": name, "status": "ONLINE", "healthy": True}


def _task(dataset: str, state: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": 1,
        "dataset": dataset,
        "recursive": True,
        "lifetime_value": 14,
        "lifetime_unit": "DAY",
        "enabled": True,
        "exclude": [],
        "naming_schema": "auto-%Y-%m-%d_%H-%M",
        "allow_empty": True,
        "schedule": {
            "minute": "0",
            "hour": "0",
            "dom": "*",
            "month": "*",
            "dow": "*",
            "begin": "00:00",
            "end": "23:59",
        },
    }
    if state is not None:
        task["state"] = state
    task.update(overrides)
    return task


def _snapshot(dataset: str, name: str, createtxg: str, creation: int, used: int = 1258291) -> dict[str, Any]:
    full = f"{dataset}@{name}"
    return {
        "id": full,
        "name": full,
        "pool": dataset.split("/")[0],
        "dataset": dataset,
        "snapshot_name": name,
        "type": "SNAPSHOT",
        "createtxg": createtxg,
        "properties": {
            "creation": {
                "value": "-",
                "rawvalue": str(creation),
                "source": "NONE",
                "parsed": {"$date": creation * 1000},
            },
            "used": {"value": "-", "rawvalue": str(used), "source": "NONE", "parsed": used},
        },
    }


def _zfs_snapshot_router(
    counts: dict[tuple[str, str], int | httpx.Response] | None = None,
    listing: list[dict[str, Any]] | httpx.Response | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Answer GET /zfs/snapshot like TrueNAS REST v2.0: ``count=true`` gives a bare integer
    (0 for a filter that matches nothing), otherwise the listing."""

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if params.get("count") == "true":
            key = next(((field, params[field]) for field in ("pool", "dataset") if field in params), ("", ""))
            value = (counts or {}).get(key, 0)
            return value if isinstance(value, httpx.Response) else httpx.Response(200, json=value)
        if isinstance(listing, httpx.Response):
            return listing
        return httpx.Response(200, json=listing or [])

    return handler


def _mock_snapshot_basics(
    tasks: list[dict[str, Any]] | None = None,
    replications: list[dict[str, Any]] | None = None,
    pools: list[str] | None = None,
) -> None:
    respx.get(f"{BASE}/pool/snapshottask").mock(return_value=httpx.Response(200, json=tasks or []))
    respx.get(f"{BASE}/replication").mock(return_value=httpx.Response(200, json=replications or []))
    respx.get(f"{BASE}/pool").mock(return_value=httpx.Response(200, json=[_pool(p) for p in (pools or ["tank"])]))


def _snapshot_requests() -> list[httpx.Request]:
    return [call.request for call in respx.calls if call.request.url.path.endswith("/zfs/snapshot")]


@pytest.mark.integration
class TestTruenasSnapshots:
    @respx.mock
    async def test_snapshots_never_sends_query_options(self) -> None:
        """query-filters/query-options are not REST v2.0 parameters: TrueNAS treats them as a
        filter that matches nothing and answers "0 snapshots" (F1). No request may carry them."""
        snap_route = respx.get(f"{BASE}/zfs/snapshot").mock(return_value=httpx.Response(200, json=7))
        _mock_snapshot_basics(tasks=[_task("tank/media")])

        await truenas_snapshots.ainvoke({"dataset": "tank/media"})
        await truenas_snapshots.ainvoke({"dataset": "tank/media", "list_snapshots": True})

        assert snap_route.called
        for call in respx.calls:
            params = call.request.url.params
            assert "query-options" not in params, call.request.url
            assert "query-filters" not in params, call.request.url

    @respx.mock
    async def test_snapshots_reports_counts_from_count_endpoint(self) -> None:
        respx.get(f"{BASE}/zfs/snapshot").mock(
            side_effect=_zfs_snapshot_router({("pool", "tank"): 3535, ("pool", "backup"): 12})
        )
        _mock_snapshot_basics(pools=["tank", "backup"])

        result = await truenas_snapshots.ainvoke({})

        assert "pool tank: 3535 snapshots" in result
        assert "pool backup: 12 snapshots" in result
        counted = {(r.url.params.get("count"), r.url.params.get("pool")) for r in _snapshot_requests()}
        assert counted == {("true", "tank"), ("true", "backup")}

    @respx.mock
    async def test_snapshots_shows_task_last_run(self) -> None:
        state = {"state": "FINISHED", "datetime": {"$date": TASK_LAST_RUN_MS}}
        respx.get(f"{BASE}/zfs/snapshot").mock(
            side_effect=_zfs_snapshot_router({("pool", "tank"): 3535, ("dataset", "tank/media"): 120})
        )
        _mock_snapshot_basics(tasks=[_task("tank/media", state=state)])

        result = await truenas_snapshots.ainvoke({})

        assert "tank/media (enabled, recursive, keep 14 DAY, schedule 0 0 * * *)" in result
        assert "last run: FINISHED at 2026-11-10 00:00:05Z (" in result
        assert " ago)" in result
        assert "snapshots of tank/media itself: 120 snapshots" in result

    @respx.mock
    async def test_snapshots_task_without_state(self) -> None:
        respx.get(f"{BASE}/zfs/snapshot").mock(side_effect=_zfs_snapshot_router())
        _mock_snapshot_basics(tasks=[_task("tank/photos")])

        result = await truenas_snapshots.ainvoke({})

        assert "last run: not reported by TrueNAS" in result

    @respx.mock
    async def test_snapshots_task_error_state_shows_error(self) -> None:
        state = {"state": "ERROR", "datetime": {"$date": TASK_LAST_RUN_MS}, "error": "dataset is busy"}
        respx.get(f"{BASE}/zfs/snapshot").mock(side_effect=_zfs_snapshot_router())
        _mock_snapshot_basics(tasks=[_task("tank/media", state=state)])

        result = await truenas_snapshots.ainvoke({})

        assert "last run: ERROR at 2026-11-10 00:00:05Z" in result
        assert "error: dataset is busy" in result

    @respx.mock
    async def test_replication_state_with_datetime(self) -> None:
        repl = {
            "id": 1,
            "name": "tank-to-backup",
            "direction": "PUSH",
            "transport": "LOCAL",
            "source_datasets": ["tank/media"],
            "target_dataset": "backup/media",
            "enabled": True,
            "auto": True,
            "state": {"state": "FINISHED", "datetime": {"$date": TASK_LAST_RUN_MS}, "last_snapshot": "x"},
        }
        respx.get(f"{BASE}/zfs/snapshot").mock(side_effect=_zfs_snapshot_router())
        _mock_snapshot_basics(replications=[repl])

        result = await truenas_snapshots.ainvoke({})

        assert "tank-to-backup (enabled, PUSH, LOCAL): tank/media -> backup/media" in result
        assert "last run: FINISHED at 2026-11-10 00:00:05Z" in result

    @respx.mock
    async def test_dataset_adds_exact_and_pool_counts(self) -> None:
        respx.get(f"{BASE}/zfs/snapshot").mock(
            side_effect=_zfs_snapshot_router({("pool", "tank"): 3535, ("dataset", "tank/media"): 120})
        )
        _mock_snapshot_basics(pools=["tank"])

        result = await truenas_snapshots.ainvoke({"dataset": "tank/media"})

        assert "pool tank: 3535 snapshots" in result
        assert "dataset tank/media (this dataset only, not its children): 120 snapshots" in result
        requested = {tuple(sorted(r.url.params.items())) for r in _snapshot_requests()}
        assert (("count", "true"), ("dataset", "tank/media")) in requested
        # counts only: no listing request without list_snapshots
        assert all(r.url.params.get("count") == "true" for r in _snapshot_requests())

    @respx.mock
    async def test_dataset_on_pool_missing_from_pool_list_is_still_counted(self) -> None:
        respx.get(f"{BASE}/zfs/snapshot").mock(side_effect=_zfs_snapshot_router({("pool", "backup"): 9}))
        _mock_snapshot_basics(pools=["tank"])

        result = await truenas_snapshots.ainvoke({"dataset": "backup/media"})

        assert "pool backup: 9 snapshots" in result

    @respx.mock
    async def test_count_failure_is_visible(self) -> None:
        respx.get(f"{BASE}/zfs/snapshot").mock(
            side_effect=_zfs_snapshot_router(
                {("pool", "tank"): httpx.Response(500, text="boom"), ("pool", "backup"): 12}
            )
        )
        _mock_snapshot_basics(pools=["tank", "backup"])

        result = await truenas_snapshots.ainvoke({})

        assert "pool tank: unavailable: TrueNAS API error: HTTP 500 - boom" in result
        assert "pool backup: 12 snapshots" in result

    @respx.mock
    async def test_count_timeout_is_visible(self) -> None:
        respx.get(f"{BASE}/zfs/snapshot").mock(side_effect=httpx.ReadTimeout("Read timed out"))
        _mock_snapshot_basics()

        result = await truenas_snapshots.ainvoke({})

        assert "pool tank: unavailable: TrueNAS request timed out after 30s" in result

    @respx.mock
    async def test_count_that_is_not_an_integer_is_visible(self) -> None:
        respx.get(f"{BASE}/zfs/snapshot").mock(return_value=httpx.Response(200, json=[]))
        _mock_snapshot_basics()

        result = await truenas_snapshots.ainvoke({})

        assert "pool tank: unavailable: unexpected TrueNAS response '[]' (expected an integer)" in result

    @respx.mock
    async def test_list_snapshots_requires_dataset(self) -> None:
        route = respx.route(url__startswith=BASE).mock(return_value=httpx.Response(200, json=[]))

        result = await truenas_snapshots.ainvoke({"list_snapshots": True})

        assert "list_snapshots needs dataset" in result
        assert not route.called

    @respx.mock
    async def test_list_snapshots_sorts_by_creation_not_createtxg(self) -> None:
        # createtxg is a string: as strings "99" sorts above "987614", as integers below "5000".
        # The creation order below matches neither, nor the name order.
        listing = [
            _snapshot("tank/media", "snap-2", createtxg="987614", creation=1794200000),  # oldest
            _snapshot("tank/media", "snap-1", createtxg="5000", creation=1794300000),  # newest
            _snapshot("tank/media", "snap-3", createtxg="99", creation=1794250000),  # middle
        ]
        respx.get(f"{BASE}/zfs/snapshot").mock(
            side_effect=_zfs_snapshot_router({("dataset", "tank/media"): 3}, listing=listing)
        )
        _mock_snapshot_basics()

        result = await truenas_snapshots.ainvoke({"dataset": "tank/media", "list_snapshots": True})

        newest = result.index("tank/media@snap-1 (created 2026-11-10 08:40:00Z")
        middle = result.index("tank/media@snap-3 (created")
        oldest = result.index("tank/media@snap-2 (created")
        assert newest < middle < oldest
        assert "Snapshots of tank/media (all 3):" in result
        assert "used 1.2 MiB" in result

    @respx.mock
    async def test_list_snapshots_request_has_no_limit_and_long_timeout(self) -> None:
        respx.get(f"{BASE}/zfs/snapshot").mock(side_effect=_zfs_snapshot_router())
        _mock_snapshot_basics()

        await truenas_snapshots.ainvoke({"dataset": "tank/media", "list_snapshots": True, "limit": 5})

        listing_requests = [r for r in _snapshot_requests() if "count" not in r.url.params]
        assert len(listing_requests) == 1
        request = listing_requests[0]
        assert dict(request.url.params) == {"dataset": "tank/media"}
        assert request.extensions["timeout"]["read"] == 45

    @respx.mock
    async def test_list_snapshots_says_newest_n_of_m(self) -> None:
        listing = [_snapshot("tank/media", f"auto-{i:02d}", str(i), 1794200000 + i) for i in range(10)]
        respx.get(f"{BASE}/zfs/snapshot").mock(side_effect=_zfs_snapshot_router(listing=listing))
        _mock_snapshot_basics()

        result = await truenas_snapshots.ainvoke({"dataset": "tank/media", "list_snapshots": True, "limit": 3})

        assert "Snapshots of tank/media (showing newest 3 of 10):" in result
        assert "tank/media@auto-09" in result
        assert "tank/media@auto-06" not in result

    @respx.mock
    async def test_list_snapshots_failure_is_visible_and_counts_still_shown(self) -> None:
        respx.get(f"{BASE}/zfs/snapshot").mock(
            side_effect=_zfs_snapshot_router(
                {("pool", "tank"): 3535}, listing=httpx.Response(500, text="middleware busy")
            )
        )
        _mock_snapshot_basics()

        result = await truenas_snapshots.ainvoke({"dataset": "tank/media", "list_snapshots": True})

        assert "pool tank: 3535 snapshots" in result
        assert "Snapshots of tank/media: unavailable: TrueNAS API error: HTTP 500 - middleware busy" in result

    @respx.mock
    async def test_connect_error(self) -> None:
        respx.get(f"{BASE}/pool/snapshottask").mock(side_effect=httpx.ConnectError("Connection refused"))

        result = await truenas_snapshots.ainvoke({})
        assert "Cannot connect" in result

    @respx.mock
    async def test_timeout(self) -> None:
        respx.get(f"{BASE}/pool/snapshottask").mock(side_effect=httpx.ReadTimeout("Read timed out"))

        result = await truenas_snapshots.ainvoke({})
        assert "timed out" in result

    @respx.mock
    async def test_auth_error(self) -> None:
        respx.get(f"{BASE}/pool/snapshottask").mock(return_value=httpx.Response(401, text="Not authenticated"))

        result = await truenas_snapshots.ainvoke({})
        assert "401" in result


@pytest.mark.integration
class TestTruenasSystemStatus:
    @respx.mock
    async def test_full_system_status(self) -> None:
        respx.get(f"{BASE}/system/info").mock(
            return_value=httpx.Response(
                200,
                json={
                    "version": "TrueNAS-SCALE-24.04.2",
                    "hostname": "truenas",
                    "uptime_seconds": 864000.0,
                    "system_product": "Supermicro",
                    "physmem": 64 * 1024**3,
                    "cores": 4,
                    "loadavg": [0.5, 0.3, 0.2],
                    "ecc_memory": False,
                },
            )
        )
        respx.get(f"{BASE}/alert/list").mock(
            return_value=httpx.Response(
                200,
                json=[{"level": "WARNING", "formatted": "Pool 80% full", "dismissed": False}],
            )
        )
        respx.get(f"{BASE}/core/get_jobs").mock(return_value=httpx.Response(200, json=[]))
        respx.get(f"{BASE}/disk").mock(
            return_value=httpx.Response(
                200,
                json=[{"name": "sda", "model": "WDC", "serial": "ABC", "type": "HDD", "size": 8 * 1024**4}],
            )
        )

        result = await truenas_system_status.ainvoke({})
        assert "TrueNAS-SCALE-24.04.2" in result
        assert "10d 0h" in result
        assert "WARNING" in result
        assert "sda" in result

    @respx.mock
    async def test_connect_error(self) -> None:
        respx.get(f"{BASE}/system/info").mock(side_effect=httpx.ConnectError("Connection refused"))

        result = await truenas_system_status.ainvoke({})
        assert "Cannot connect" in result

    @respx.mock
    async def test_timeout(self) -> None:
        respx.get(f"{BASE}/system/info").mock(side_effect=httpx.ReadTimeout("Read timed out"))

        result = await truenas_system_status.ainvoke({})
        assert "timed out" in result

    @respx.mock
    async def test_auth_error(self) -> None:
        respx.get(f"{BASE}/system/info").mock(return_value=httpx.Response(401, text="Not authenticated"))

        result = await truenas_system_status.ainvoke({})
        assert "401" in result

    @respx.mock
    async def test_sends_bearer_auth(self) -> None:
        route = respx.get(f"{BASE}/system/info").mock(return_value=httpx.Response(200, json={}))
        respx.get(f"{BASE}/alert/list").mock(return_value=httpx.Response(200, json=[]))
        respx.get(f"{BASE}/core/get_jobs").mock(return_value=httpx.Response(200, json=[]))
        respx.get(f"{BASE}/disk").mock(return_value=httpx.Response(200, json=[]))

        await truenas_system_status.ainvoke({})
        assert route.called
        auth_header = route.calls.last.request.headers["authorization"]
        assert auth_header.startswith("Bearer ")


@pytest.mark.integration
class TestTruenasApps:
    @respx.mock
    async def test_lists_apps(self) -> None:
        respx.get(f"{BASE}/app").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"name": "alloy", "state": "RUNNING", "human_version": "1.5.0"},
                    {"name": "disk-status-exporter", "state": "STOPPED", "version": "0.3.0"},
                ],
            )
        )

        result = await truenas_apps.ainvoke({})
        assert "2 app(s)" in result
        assert "alloy" in result
        assert "disk-status-exporter" in result
        assert "RUNNING" in result
        assert "STOPPED" in result

    @respx.mock
    async def test_empty_apps(self) -> None:
        respx.get(f"{BASE}/app").mock(return_value=httpx.Response(200, json=[]))

        result = await truenas_apps.ainvoke({})
        assert "No apps found" in result

    @respx.mock
    async def test_connect_error(self) -> None:
        respx.get(f"{BASE}/app").mock(side_effect=httpx.ConnectError("Connection refused"))

        result = await truenas_apps.ainvoke({})
        assert "Cannot connect" in result

    @respx.mock
    async def test_timeout(self) -> None:
        respx.get(f"{BASE}/app").mock(side_effect=httpx.ReadTimeout("Read timed out"))

        result = await truenas_apps.ainvoke({})
        assert "timed out" in result

    @respx.mock
    async def test_auth_error(self) -> None:
        respx.get(f"{BASE}/app").mock(return_value=httpx.Response(401, text="Not authenticated"))

        result = await truenas_apps.ainvoke({})
        assert "401" in result


@pytest.mark.integration
class TestTruenasNotConfigured:
    async def test_pool_status_not_configured(self, mock_settings: Any) -> None:
        mock_settings.truenas_url = ""
        result = await truenas_pool_status.ainvoke({})
        assert "not configured" in result.lower()

    async def test_list_shares_not_configured(self, mock_settings: Any) -> None:
        mock_settings.truenas_url = ""
        result = await truenas_list_shares.ainvoke({})
        assert "not configured" in result.lower()

    async def test_snapshots_not_configured(self, mock_settings: Any) -> None:
        mock_settings.truenas_url = ""
        result = await truenas_snapshots.ainvoke({})
        assert "not configured" in result.lower()

    async def test_system_status_not_configured(self, mock_settings: Any) -> None:
        mock_settings.truenas_url = ""
        result = await truenas_system_status.ainvoke({})
        assert "not configured" in result.lower()

    async def test_apps_not_configured(self, mock_settings: Any) -> None:
        mock_settings.truenas_url = ""
        result = await truenas_apps.ainvoke({})
        assert "not configured" in result.lower()


HTTP_BASE = "http://truenas.test/api/v2.0"


@pytest.mark.integration
class TestTruenasHttpsOnly:
    """The API key goes only to an https URL (NB-2). The guard runs before any client is built."""

    @respx.mock
    async def test_http_url_refuses_to_send_key(self, mock_settings: Any) -> None:
        mock_settings.truenas_url = "http://truenas.test"
        route = respx.get(f"{HTTP_BASE}/pool").mock(return_value=httpx.Response(200, json=[]))

        with pytest.raises(ToolException, match="must be https; refusing to send the API key"):
            await _truenas_get("/pool")

        assert not route.called
        assert respx.calls.call_count == 0

    @respx.mock
    async def test_http_url_refused_for_post(self, mock_settings: Any) -> None:
        mock_settings.truenas_url = "http://truenas.test"
        route = respx.post(f"{HTTP_BASE}/smb/status").mock(return_value=httpx.Response(200, json=[]))

        with pytest.raises(ToolException, match="must be https"):
            await _truenas_post("/smb/status")

        assert not route.called

    @pytest.mark.parametrize("url", ["ws://truenas.test", "truenas.test", "HTTP://truenas.test"])
    @respx.mock
    async def test_non_https_schemes_refused(self, mock_settings: Any, url: str) -> None:
        mock_settings.truenas_url = url
        with pytest.raises(ToolException, match="must be https"):
            await _truenas_get("/pool")
        assert respx.calls.call_count == 0

    @respx.mock
    async def test_tool_reports_refusal_and_sends_nothing(self, mock_settings: Any) -> None:
        mock_settings.truenas_url = "http://truenas.test"
        route = respx.get(url__startswith=HTTP_BASE).mock(return_value=httpx.Response(200, json=[]))

        result = await truenas_snapshots.ainvoke({})

        assert "must be https" in result
        assert not route.called

    @respx.mock
    async def test_uppercase_https_scheme_allowed(self, mock_settings: Any) -> None:
        mock_settings.truenas_url = "HTTPS://truenas.test"
        route = respx.get(f"{BASE}/pool").mock(return_value=httpx.Response(200, json=[]))

        assert await _truenas_get("/pool") == []
        assert route.called
