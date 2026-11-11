"""Integration tests for Proxmox VE tools with mocked HTTP responses."""

from typing import Any

import httpx
import pytest
import respx
from langchain_core.tools import ToolException
from pydantic import ValidationError

from src.agent.tools.proxmox import (
    proxmox_get_guest_config,
    proxmox_list_guests,
    proxmox_list_tasks,
    proxmox_node_status,
)


@pytest.fixture(autouse=True)
def _use_mock_settings(mock_settings: Any) -> None:
    """Automatically use mock settings for all tests in this module."""


@pytest.mark.integration
class TestProxmoxListGuests:
    @respx.mock
    async def test_lists_vms_and_containers(self) -> None:
        respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/qemu").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "vmid": 100,
                            "name": "jellyfin",
                            "status": "running",
                            "cpus": 4,
                            "maxmem": 8589934592,
                            "cpu": 0.15,
                        }
                    ]
                },
            )
        )
        respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/lxc").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "vmid": 101,
                            "name": "adguard",
                            "status": "running",
                            "cpus": 1,
                            "maxmem": 536870912,
                            "cpu": 0.02,
                        }
                    ]
                },
            )
        )

        result = await proxmox_list_guests.ainvoke({})
        assert "2 guest(s)" in result
        assert "jellyfin" in result
        assert "adguard" in result

    @respx.mock
    async def test_filter_by_type(self) -> None:
        respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/qemu").mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"vmid": 100, "name": "vm1", "status": "running", "cpus": 1, "maxmem": 1024}]},
            )
        )

        result = await proxmox_list_guests.ainvoke({"guest_type": "qemu"})
        assert "1 guest(s)" in result
        assert "vm1" in result

    @respx.mock
    async def test_connect_error(self) -> None:
        respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/qemu").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        result = await proxmox_list_guests.ainvoke({})
        assert "Cannot connect" in result

    @respx.mock
    async def test_timeout(self) -> None:
        respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/qemu").mock(
            side_effect=httpx.ReadTimeout("Read timed out")
        )

        result = await proxmox_list_guests.ainvoke({})
        assert "timed out" in result

    @respx.mock
    async def test_auth_error(self) -> None:
        respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/qemu").mock(
            return_value=httpx.Response(401, text="authentication failure")
        )

        result = await proxmox_list_guests.ainvoke({})
        assert "401" in result

    @respx.mock
    async def test_sends_auth_header(self) -> None:
        route = respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/qemu").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/lxc").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        await proxmox_list_guests.ainvoke({})
        assert route.called
        auth_header = route.calls.last.request.headers["authorization"]
        assert auth_header.startswith("PVEAPIToken=")


@pytest.mark.integration
class TestProxmoxGetGuestConfig:
    @respx.mock
    async def test_successful_config(self) -> None:
        respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/qemu/100/config").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "name": "jellyfin",
                        "cores": 4,
                        "memory": 8192,
                        "scsi0": "local-lvm:vm-100-disk-0,size=50G",
                        "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
                    }
                },
            )
        )

        result = await proxmox_get_guest_config.ainvoke({"vmid": 100})
        assert "jellyfin" in result
        assert "cores: 4" in result
        assert "scsi0:" in result
        assert "net0:" in result

    @respx.mock
    async def test_lxc_config(self) -> None:
        respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/lxc/101/config").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "hostname": "adguard",
                        "cores": 1,
                        "memory": 512,
                        "rootfs": "local-lvm:vm-101-disk-0,size=8G",
                    }
                },
            )
        )

        result = await proxmox_get_guest_config.ainvoke({"vmid": 101, "guest_type": "lxc"})
        assert "adguard" in result or "101" in result

    @respx.mock
    async def test_wrong_type_500_error(self) -> None:
        respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/qemu/101/config").mock(
            return_value=httpx.Response(500, text="Configuration file 'nodes/proxmox/qemu/101.conf' does not exist")
        )

        result = await proxmox_get_guest_config.ainvoke({"vmid": 101, "guest_type": "qemu"})
        assert "not found" in result.lower() or "other type" in result.lower()

    @respx.mock
    async def test_connect_error(self) -> None:
        respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/qemu/100/config").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        result = await proxmox_get_guest_config.ainvoke({"vmid": 100})
        assert "Cannot connect" in result


@pytest.mark.integration
class TestProxmoxGetGuestConfigByName:
    """Tests for the name-based lookup feature of proxmox_get_guest_config."""

    def _mock_guest_lists(self) -> None:
        """Set up mock responses for both qemu and lxc guest lists."""
        respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/qemu").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"vmid": 100, "name": "home-assistant", "status": "running", "cpus": 2, "maxmem": 4294967296},
                        {"vmid": 104, "name": "truenas", "status": "running", "cpus": 4, "maxmem": 17179869184},
                    ]
                },
            )
        )
        respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/lxc").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"vmid": 113, "name": "immich", "status": "running", "cpus": 12, "maxmem": 6442450944},
                        {"vmid": 110, "name": "jellyfin", "status": "running", "cpus": 8, "maxmem": 4294967296},
                    ]
                },
            )
        )

    @respx.mock
    async def test_resolve_lxc_by_name(self) -> None:
        self._mock_guest_lists()
        respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/lxc/113/config").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "hostname": "immich",
                        "cores": 12,
                        "memory": 6144,
                        "rootfs": "local-zfs:subvol-113-disk-0,size=50G",
                    }
                },
            )
        )

        result = await proxmox_get_guest_config.ainvoke({"name": "immich"})
        assert "113" in result
        assert "cores: 12" in result
        assert "rootfs:" in result

    @respx.mock
    async def test_resolve_qemu_by_name(self) -> None:
        self._mock_guest_lists()
        respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/qemu/104/config").mock(
            return_value=httpx.Response(
                200,
                json={"data": {"name": "truenas", "cores": 4, "memory": 16384}},
            )
        )

        result = await proxmox_get_guest_config.ainvoke({"name": "truenas"})
        assert "104" in result
        assert "truenas" in result

    @respx.mock
    async def test_name_case_insensitive(self) -> None:
        self._mock_guest_lists()
        respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/lxc/113/config").mock(
            return_value=httpx.Response(
                200,
                json={"data": {"hostname": "immich", "cores": 12, "memory": 6144}},
            )
        )

        result = await proxmox_get_guest_config.ainvoke({"name": "Immich"})
        assert "113" in result

    @respx.mock
    async def test_name_not_found(self) -> None:
        self._mock_guest_lists()

        result = await proxmox_get_guest_config.ainvoke({"name": "nonexistent"})
        assert "no guest found" in result.lower()
        assert "proxmox_list_guests" in result.lower()

    async def test_neither_vmid_nor_name(self) -> None:
        result = await proxmox_get_guest_config.ainvoke({})
        assert "provide either vmid or name" in result.lower()


PROM_QUERY_URL = "http://prometheus.test:9090/api/v1/query"
STATUS_URL = "https://proxmox.test:8006/api2/json/nodes/proxmox/status"


def _mock_node_cpu_ok(value: str = "0.1") -> respx.Route:
    return respx.get(PROM_QUERY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1700000000, value]}]},
            },
        )
    )


def _mock_status() -> None:
    respx.get(STATUS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "cpu": 0.25,
                    "memory": {"used": 17179869184, "total": 68719476736},
                    "uptime": 864000,
                    "loadavg": ["1.5", "2.0", "1.8"],
                    "pveversion": "pve-manager/8.1.3",
                    "kversion": "6.5.11-8-pve",
                    "rootfs": {"used": 10737418240, "total": 107374182400},
                }
            },
        )
    )


@pytest.mark.integration
class TestProxmoxNodeStatus:
    @respx.mock
    async def test_successful_status(self) -> None:
        respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/status").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "cpu": 0.25,
                        "memory": {"used": 17179869184, "total": 68719476736},
                        "uptime": 864000,
                        "loadavg": ["1.5", "2.0", "1.8"],
                        "pveversion": "pve-manager/8.1.3",
                        "kversion": "6.5.11-8-pve",
                        "rootfs": {"used": 10737418240, "total": 107374182400},
                    }
                },
            )
        )

        _mock_node_cpu_ok()
        result = await proxmox_node_status.ainvoke({})
        assert "PVE instant sample): 25.0%" in result
        assert "10 days" in result
        assert "pve-manager/8.1.3" in result

    @respx.mock
    async def test_timeout(self) -> None:
        respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/status").mock(
            side_effect=httpx.ReadTimeout("Read timed out")
        )

        result = await proxmox_node_status.ainvoke({})
        assert "timed out" in result

    @respx.mock
    async def test_node_status_shows_prometheus_cpu(self) -> None:
        _mock_status()
        route = _mock_node_cpu_ok("0.4321")
        result = await proxmox_node_status.ainvoke({})
        assert "CPU (node_exporter 5-min avg): 43.2%" in result
        assert "CPU (PVE instant sample): 25.0%" in result
        query = route.calls.last.request.url.params["query"]
        assert 'hostname="proxmox"' in query
        assert 'mode="idle"' in query
        assert "[5m]" in query

    @respx.mock
    async def test_node_status_prometheus_failure_visible(self) -> None:
        _mock_status()
        respx.get(PROM_QUERY_URL).mock(return_value=httpx.Response(503, text="down"))
        result = await proxmox_node_status.ainvoke({})
        assert "CPU (node_exporter 5-min avg): unavailable:" in result
        assert "503" in result
        assert "CPU (PVE instant sample): 25.0%" in result
        assert "pve-manager/8.1.3" in result

    @respx.mock
    async def test_node_status_prometheus_no_data_visible(self) -> None:
        _mock_status()
        respx.get(PROM_QUERY_URL).mock(
            return_value=httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": []}})
        )
        result = await proxmox_node_status.ainvoke({})
        assert "unavailable: no data" in result


@pytest.mark.integration
class TestGuestTypeValidation:
    @respx.mock
    async def test_guest_type_rejects_path(self) -> None:
        """A path-shaped guest_type raises and sends no request, even past the schema."""
        trap = respx.route().mock(return_value=httpx.Response(200, json={"data": {}}))
        with pytest.raises(ToolException):
            await proxmox_get_guest_config.coroutine(vmid=100, guest_type="../../access/acl#")  # type: ignore[misc, arg-type]  # pyright: ignore
        assert not trap.called

    @respx.mock
    async def test_guest_type_rejected_by_schema(self) -> None:
        trap = respx.route().mock(return_value=httpx.Response(200, json={"data": {}}))
        with pytest.raises(ValidationError):
            await proxmox_get_guest_config.ainvoke({"vmid": 100, "guest_type": "../../access/acl#"})
        with pytest.raises(ValidationError):
            await proxmox_list_guests.ainvoke({"guest_type": "bogus"})
        assert not trap.called

    @respx.mock
    async def test_list_guests_rejects_bad_type_in_body(self) -> None:
        trap = respx.route().mock(return_value=httpx.Response(200, json={"data": []}))
        with pytest.raises(ToolException):
            await proxmox_list_guests.coroutine(guest_type="../x")  # type: ignore[misc, arg-type]  # pyright: ignore
        assert not trap.called


@pytest.mark.integration
class TestProxmoxListTasks:
    @respx.mock
    async def test_successful_task_list(self) -> None:
        respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/tasks").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "type": "vzdump",
                            "status": "OK",
                            "user": "root@pam",
                            "id": "100",
                            "starttime": 1700000000,
                            "endtime": 1700003600,
                        },
                        {
                            "type": "qmigrate",
                            "status": "ERROR: migration failed",
                            "user": "root@pam",
                            "id": "200",
                            "starttime": 1700004000,
                        },
                    ]
                },
            )
        )

        result = await proxmox_list_tasks.ainvoke({"limit": 20})
        assert "2 recent task(s)" in result
        assert "[OK]" in result
        assert "ERROR" in result

    @respx.mock
    async def test_errors_only_filter(self) -> None:
        route = respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/tasks").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        await proxmox_list_tasks.ainvoke({"limit": 10, "errors_only": True})
        assert route.called
        assert route.calls.last.request.url.params["errors"] == "1"

    @respx.mock
    async def test_sends_limit_param(self) -> None:
        route = respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/tasks").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        await proxmox_list_tasks.ainvoke({"limit": 5})
        assert route.calls.last.request.url.params["limit"] == "5"

    @respx.mock
    async def test_connect_error(self) -> None:
        respx.get("https://proxmox.test:8006/api2/json/nodes/proxmox/tasks").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        result = await proxmox_list_tasks.ainvoke({})
        assert "Cannot connect" in result


@pytest.mark.integration
class TestProxmoxNotConfigured:
    async def test_list_guests_not_configured(self, mock_settings: Any) -> None:
        mock_settings.proxmox_url = ""
        result = await proxmox_list_guests.ainvoke({})
        assert "not configured" in result.lower()

    async def test_node_status_not_configured(self, mock_settings: Any) -> None:
        mock_settings.proxmox_url = ""
        result = await proxmox_node_status.ainvoke({})
        assert "not configured" in result.lower()
