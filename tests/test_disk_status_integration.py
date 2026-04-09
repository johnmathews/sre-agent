"""Integration tests for the HDD power status composite tool with mocked HTTP."""

from typing import Any

import httpx
import pytest
import respx

from src.agent.tools.disk_status import hdd_power_status


@pytest.fixture(autouse=True)
def _use_mock_settings(mock_settings: Any) -> None:
    """Automatically use mock settings for all tests in this module."""


def _mock_power_state_response(
    results: list[dict[str, Any]],
) -> httpx.Response:
    """Build a mocked Prometheus instant query response for disk_power_state."""
    return httpx.Response(
        200,
        json={
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": results,
            },
        },
    )


def _mock_range_response(
    results: list[dict[str, Any]],
) -> httpx.Response:
    """Build a mocked Prometheus range query response."""
    return httpx.Response(
        200,
        json={
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": results,
            },
        },
    )


def _stable_range_data() -> list[dict[str, Any]]:
    """Range result where both disks stay in the same state group (no group transitions)."""
    return [
        {
            "metric": {"device_id": "/dev/disk/by-id/wwn-0x5000c500eb02b449"},
            # idle_a(3) → idle_b(4) — both "active" group, 0 group transitions
            "values": [[1699999800, "3"], [1699999860, "4"], [1699999920, "3"], [1699999980, "4"]],
        },
        {
            "metric": {"device_id": "/dev/disk/by-id/wwn-0x5000c500f742ccbf"},
            "values": [[1699999800, "0"], [1699999860, "0"], [1699999920, "0"], [1699999980, "0"]],
        },
    ]


def _mock_truenas_disks() -> httpx.Response:
    """Build a mocked TrueNAS /disk response with 2 HDDs.

    Note: The real TrueNAS /disk endpoint returns pool as null (or omits it).
    Pool assignments come from /pool topology — see _mock_truenas_pools().
    """
    return httpx.Response(
        200,
        json=[
            {
                "identifier": "{serial_lunid}5000c500eb02b449",
                "name": "sdc",
                "serial": "WWZ5TZSF",
                "model": "ST8000VN004-3CP101",
                "type": "HDD",
                "size": 8001563222016,
                "pool": None,
                "togglesmart": True,
                "hddstandby": "30",
            },
            {
                "identifier": "{serial_lunid}5000c500f742ccbf",
                "name": "sdf",
                "serial": "K3S04BKQ",
                "model": "ST16000NT001-3LV101",
                "type": "HDD",
                "size": 16000900661248,
                "pool": None,
                "togglesmart": True,
                "hddstandby": "60",
            },
        ],
    )


def _mock_truenas_pools(pool_disks: dict[str, list[str]] | None = None) -> httpx.Response:
    """Build a mocked TrueNAS /pool response with topology.

    Args:
        pool_disks: mapping of pool_name → [disk_names]. Defaults to both
                    HDDs in "tank".
    """
    if pool_disks is None:
        pool_disks = {"tank": ["sdc", "sdf"]}

    pools = []
    for i, (pool_name, disks) in enumerate(pool_disks.items()):
        pools.append(
            {
                "id": i + 1,
                "name": pool_name,
                "status": "ONLINE",
                "healthy": True,
                "topology": {
                    "data": [
                        {
                            "type": "MIRROR",
                            "children": [{"disk": d} for d in disks],
                        }
                    ],
                    "special": [],
                    "cache": [],
                    "log": [],
                    "spare": [],
                    "dedup": [],
                },
            }
        )
    return httpx.Response(200, json=pools)


POWER_STATE_RESULTS = [
    {
        "metric": {
            "__name__": "disk_power_state",
            "device_id": "/dev/disk/by-id/wwn-0x5000c500eb02b449",
            "type": "hdd",
            "pool": "tank",
        },
        "value": [1700000000, "2"],
    },
    {
        "metric": {
            "__name__": "disk_power_state",
            "device_id": "/dev/disk/by-id/wwn-0x5000c500f742ccbf",
            "type": "hdd",
            "pool": "tank",
        },
        "value": [1700000000, "0"],
    },
]


@pytest.mark.integration
class TestHddPowerStatus:
    @respx.mock
    async def test_shows_current_state_with_disk_names(self) -> None:
        """Tool cross-references Prometheus device_ids with TrueNAS disk inventory."""
        # Only current_state uses instant query now
        respx.get("http://prometheus.test:9090/api/v1/query").mock(
            return_value=_mock_power_state_response(POWER_STATE_RESULTS),
        )
        # 1 range fetch (stats+transitions) + widening skips 1h/6h/24h (≤86400),
        # tries only 7d — all return stable data (no group transitions).
        respx.get("http://prometheus.test:9090/api/v1/query_range").mock(
            side_effect=[
                _mock_range_response(_stable_range_data()),  # 24h fetch
                _mock_range_response(_stable_range_data()),  # 7d window
            ]
        )
        respx.get("https://truenas.test/api/v2.0/disk").mock(return_value=_mock_truenas_disks())
        respx.get("https://truenas.test/api/v2.0/pool").mock(return_value=_mock_truenas_pools())

        result = await hdd_power_status.ainvoke({})

        # Should show human-readable disk names, not wwn IDs
        assert "sdc" in result
        assert "ST8000VN004" in result
        assert "sdf" in result
        assert "ST16000NT001" in result
        # Should show power state labels
        assert "active_or_idle" in result
        assert "standby" in result
        # Should NOT show raw wwn paths
        assert "/dev/disk/by-id/" not in result

    @respx.mock
    async def test_no_changes_reports_stable(self) -> None:
        """When no group transitions in 7d, clearly states disks are stable."""
        respx.get("http://prometheus.test:9090/api/v1/query").mock(
            return_value=_mock_power_state_response(POWER_STATE_RESULTS),
        )
        # 1 range fetch + widening skips ≤24h, tries only 7d — all stable
        respx.get("http://prometheus.test:9090/api/v1/query_range").mock(
            side_effect=[
                _mock_range_response(_stable_range_data()),  # 24h fetch
                _mock_range_response(_stable_range_data()),  # 7d window
            ]
        )
        respx.get("https://truenas.test/api/v2.0/disk").mock(return_value=_mock_truenas_disks())
        respx.get("https://truenas.test/api/v2.0/pool").mock(return_value=_mock_truenas_pools())

        result = await hdd_power_status.ainvoke({})
        assert "no power state changes" in result.lower() or "no change" in result.lower()

    @respx.mock
    async def test_finds_transitions_with_range_query(self) -> None:
        """When group transitions are detected, extracts times from same data (no re-query)."""
        # Range data with a real group transition: standby(0) → active(2)
        transition_range_data = [
            {
                "metric": {"device_id": "/dev/disk/by-id/wwn-0x5000c500eb02b449"},
                "values": [
                    [1699999800, "0"],  # standby
                    [1699999815, "0"],
                    [1699999830, "2"],  # ← group transition (standby → active)
                    [1699999845, "2"],
                ],
            },
            {
                "metric": {"device_id": "/dev/disk/by-id/wwn-0x5000c500f742ccbf"},
                "values": [
                    [1699999800, "0"],
                    [1699999815, "0"],
                    [1699999830, "0"],
                    [1699999845, "0"],
                ],
            },
        ]
        respx.get("http://prometheus.test:9090/api/v1/query").mock(
            return_value=_mock_power_state_response(POWER_STATE_RESULTS),
        )
        # Only 1 range query — stats AND transitions extracted from same data
        respx.get("http://prometheus.test:9090/api/v1/query_range").mock(
            side_effect=[
                _mock_range_response(transition_range_data),  # single fetch
            ]
        )
        respx.get("https://truenas.test/api/v2.0/disk").mock(return_value=_mock_truenas_disks())
        respx.get("https://truenas.test/api/v2.0/pool").mock(return_value=_mock_truenas_pools())

        result = await hdd_power_status.ainvoke({})

        # Should show transition with disk name and timestamp
        assert "sdc" in result
        assert "standby" in result
        assert "active_or_idle" in result
        assert "→" in result
        # sdf should show no change
        assert "no change" in result.lower()
        # Should show 24h stats
        assert "1 change" in result
        assert "%" in result

    @respx.mock
    async def test_shows_24h_change_counts(self) -> None:
        """Tool includes per-disk group transition counts in last 24 hours."""
        # 24h range data: disk1 has 3 group transitions, disk2 has 2
        twentyfour_h_data = [
            {
                "metric": {"device_id": "/dev/disk/by-id/wwn-0x5000c500eb02b449"},
                # standby→active→standby→active (3 group transitions)
                "values": [
                    [1699920000, "0"],
                    [1699930000, "2"],
                    [1699940000, "0"],
                    [1699950000, "4"],
                ],
            },
            {
                "metric": {"device_id": "/dev/disk/by-id/wwn-0x5000c500f742ccbf"},
                # active→standby→active (2 group transitions)
                "values": [
                    [1699920000, "3"],
                    [1699930000, "0"],
                    [1699940000, "6"],
                    [1699950000, "6"],
                ],
            },
        ]
        respx.get("http://prometheus.test:9090/api/v1/query").mock(
            return_value=_mock_power_state_response(POWER_STATE_RESULTS),
        )
        # Only 1 range query — data has transitions so no widening needed
        respx.get("http://prometheus.test:9090/api/v1/query_range").mock(
            side_effect=[
                _mock_range_response(twentyfour_h_data),  # single fetch
            ]
        )
        respx.get("https://truenas.test/api/v2.0/disk").mock(return_value=_mock_truenas_disks())
        respx.get("https://truenas.test/api/v2.0/pool").mock(return_value=_mock_truenas_pools())

        result = await hdd_power_status.ainvoke({})

        # Should show total and per-disk counts
        assert "5" in result and "total" in result
        assert "3 change" in result
        assert "2 change" in result
        # Should show time-in-state percentages
        assert "standby" in result
        assert "active" in result
        assert "%" in result

    @respx.mock
    async def test_prometheus_unreachable(self) -> None:
        """When Prometheus is down, returns a clear error."""
        respx.get("http://prometheus.test:9090/api/v1/query").mock(side_effect=httpx.ConnectError("Connection refused"))

        result = await hdd_power_status.ainvoke({})
        assert "Cannot connect" in result

    @respx.mock
    async def test_no_metrics_found(self) -> None:
        """When disk_power_state returns no series, explains the issue."""
        respx.get("http://prometheus.test:9090/api/v1/query").mock(return_value=_mock_power_state_response([]))

        result = await hdd_power_status.ainvoke({})
        assert "disk-status-exporter" in result

    @respx.mock
    async def test_works_without_truenas(self) -> None:
        """When TrueNAS is unreachable, still shows power states with device IDs."""
        respx.get("http://prometheus.test:9090/api/v1/query").mock(
            return_value=_mock_power_state_response(POWER_STATE_RESULTS),
        )
        # 1 range fetch + widening skips ≤24h, tries only 7d
        respx.get("http://prometheus.test:9090/api/v1/query_range").mock(
            side_effect=[
                _mock_range_response(_stable_range_data()),  # 24h fetch
                _mock_range_response(_stable_range_data()),  # 7d window
            ]
        )
        respx.get("https://truenas.test/api/v2.0/disk").mock(side_effect=httpx.ConnectError("TrueNAS down"))

        result = await hdd_power_status.ainvoke({})
        # Should still show power states, just with shortened device IDs
        assert "active_or_idle" in result
        assert "standby" in result
        assert "wwn-0x5000c500eb02b449" in result

    @respx.mock
    async def test_custom_duration_12h(self) -> None:
        """Passing duration='12h' uses that window for stats."""
        respx.get("http://prometheus.test:9090/api/v1/query").mock(
            return_value=_mock_power_state_response(POWER_STATE_RESULTS),
        )
        # 1 range fetch (12h) + widening skips 1h/6h (≤43200), tries 24h then 7d
        respx.get("http://prometheus.test:9090/api/v1/query_range").mock(
            side_effect=[
                _mock_range_response(_stable_range_data()),  # 12h fetch
                _mock_range_response(_stable_range_data()),  # 24h window
                _mock_range_response(_stable_range_data()),  # 7d window
            ]
        )
        respx.get("https://truenas.test/api/v2.0/disk").mock(return_value=_mock_truenas_disks())
        respx.get("https://truenas.test/api/v2.0/pool").mock(return_value=_mock_truenas_pools())

        result = await hdd_power_status.ainvoke({"duration": "12h"})
        assert "Last 12h" in result

    @respx.mock
    async def test_pool_filter_via_truenas(self) -> None:
        """Pool filtering uses /pool topology when /disk lacks pool info."""
        # Prometheus returns ALL disks (no pool label — real-world scenario)
        prom_results_no_pool = [
            {
                "metric": {
                    "__name__": "disk_power_state",
                    "device_id": "/dev/disk/by-id/wwn-0x5000c500eb02b449",
                    "type": "hdd",
                },
                "value": [1700000000, "2"],
            },
            {
                "metric": {
                    "__name__": "disk_power_state",
                    "device_id": "/dev/disk/by-id/wwn-0x5000c500f742ccbf",
                    "type": "hdd",
                },
                "value": [1700000000, "0"],
            },
        ]
        respx.get("http://prometheus.test:9090/api/v1/query").mock(
            return_value=_mock_power_state_response(prom_results_no_pool),
        )
        # 1 range fetch + widening skips ≤24h, tries only 7d
        respx.get("http://prometheus.test:9090/api/v1/query_range").mock(
            side_effect=[
                _mock_range_response(_stable_range_data()),  # 24h fetch
                _mock_range_response(_stable_range_data()),  # 7d window
            ]
        )
        # /disk returns NO pool info (matches real TrueNAS API)
        respx.get("https://truenas.test/api/v2.0/disk").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "identifier": "{serial_lunid}5000c500eb02b449",
                        "name": "sdc",
                        "serial": "WWZ5TZSF",
                        "model": "ST8000VN004-3CP101",
                        "type": "HDD",
                        "size": 8001563222016,
                        "pool": None,
                    },
                    {
                        "identifier": "{serial_lunid}5000c500f742ccbf",
                        "name": "sdf",
                        "serial": "K3S04BKQ",
                        "model": "ST16000NT001-3LV101",
                        "type": "HDD",
                        "size": 16000900661248,
                        "pool": None,
                    },
                ],
            ),
        )
        # /pool topology says sdc=tank, sdf=backup
        respx.get("https://truenas.test/api/v2.0/pool").mock(
            return_value=_mock_truenas_pools({"tank": ["sdc"], "backup": ["sdf"]}),
        )

        result = await hdd_power_status.ainvoke({"pool": "tank"})
        # Should only show sdc (tank), not sdf (backup)
        assert "sdc" in result
        assert "sdf" not in result
        assert "HDD Power Status" in result

    @respx.mock
    async def test_invalid_duration_returns_error(self) -> None:
        """Invalid duration string returns a clear error."""
        result = await hdd_power_status.ainvoke({"duration": "banana"})
        assert "Invalid duration" in result

    @respx.mock
    async def test_week_duration(self) -> None:
        """Passing duration='1w' works with the week multiplier."""
        respx.get("http://prometheus.test:9090/api/v1/query").mock(
            return_value=_mock_power_state_response(POWER_STATE_RESULTS),
        )
        # 1 range fetch (1w=604800s) — widening skips ALL windows (≤604800),
        # so only the initial fetch is made.
        respx.get("http://prometheus.test:9090/api/v1/query_range").mock(
            side_effect=[
                _mock_range_response(_stable_range_data()),  # 1w fetch
            ]
        )
        respx.get("https://truenas.test/api/v2.0/disk").mock(return_value=_mock_truenas_disks())
        respx.get("https://truenas.test/api/v2.0/pool").mock(return_value=_mock_truenas_pools())

        result = await hdd_power_status.ainvoke({"duration": "1w"})
        assert "Last 1w" in result

    @respx.mock
    async def test_nonexistent_pool_lists_available_pools(self) -> None:
        """Filtering by a pool with no HDDs lists available pools from topology."""
        # Prometheus returns all disks (unfiltered — pool filtering is in Python)
        respx.get("http://prometheus.test:9090/api/v1/query").mock(
            return_value=_mock_power_state_response(POWER_STATE_RESULTS),
        )
        respx.get("https://truenas.test/api/v2.0/disk").mock(return_value=_mock_truenas_disks())
        respx.get("https://truenas.test/api/v2.0/pool").mock(return_value=_mock_truenas_pools())

        result = await hdd_power_status.ainvoke({"pool": "nonexistent"})
        assert "No HDDs found in pool" in result
        assert "nonexistent" in result
        assert "tank" in result
