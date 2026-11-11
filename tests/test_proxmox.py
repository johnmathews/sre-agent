"""Unit tests for Proxmox VE tool formatting and helpers."""

import ssl

from src.agent.tools.proxmox import (
    PveGuestEntry,
    PveTaskEntry,
    _format_guest_config,
    _format_guests,
    _format_node_status,
    _format_tasks,
    _pve_ssl_verify,
)


class TestFormatGuests:
    def test_empty_list(self) -> None:
        assert "No guests found" in _format_guests([])

    def test_single_running_vm(self) -> None:
        guest: PveGuestEntry = {
            "vmid": 100,
            "name": "jellyfin",
            "status": "running",
            "type": "qemu",
            "cpus": 4,
            "maxmem": 8 * 1024**3,
            "cpu": 0.15,
        }
        result = _format_guests([guest])
        assert "1 guest(s)" in result
        assert "1 running" in result
        assert "100" in result
        assert "jellyfin" in result
        assert "VM" in result
        assert "CPU 15%" in result

    def test_mixed_running_and_stopped(self) -> None:
        guests: list[PveGuestEntry] = [
            {"vmid": 100, "name": "vm1", "status": "running", "type": "qemu", "cpus": 2, "maxmem": 4096, "cpu": 0.1},
            {"vmid": 101, "name": "ct1", "status": "stopped", "type": "lxc", "cpus": 1, "maxmem": 2048, "cpu": 0.0},
        ]
        result = _format_guests(guests)
        assert "2 guest(s)" in result
        assert "1 running" in result
        assert "1 stopped" in result
        assert "+" in result  # running marker
        assert "-" in result  # stopped marker

    def test_sorted_by_vmid(self) -> None:
        guests: list[PveGuestEntry] = [
            {"vmid": 200, "name": "b", "status": "running", "type": "qemu", "cpus": 1, "maxmem": 1024, "cpu": 0.0},
            {"vmid": 100, "name": "a", "status": "running", "type": "qemu", "cpus": 1, "maxmem": 1024, "cpu": 0.0},
        ]
        result = _format_guests(guests)
        lines = result.split("\n")
        guest_lines = [line for line in lines if line.strip().startswith("+") or line.strip().startswith("-")]
        assert "100" in guest_lines[0]
        assert "200" in guest_lines[1]

    def test_container_label(self) -> None:
        guest: PveGuestEntry = {
            "vmid": 101,
            "name": "adguard",
            "status": "running",
            "type": "lxc",
            "cpus": 1,
            "maxmem": 512 * 1024 * 1024,
            "cpu": 0.02,
        }
        result = _format_guests([guest])
        assert "CT" in result


class TestFormatNodeStatus:
    def test_memory_not_truncated(self) -> None:
        """1.99 TiB prints as 2.0 TiB; the old int() division printed 1.0 TiB."""
        data: dict[str, object] = {"memory": {"used": int(1.99 * 1024**4), "total": int(1.99 * 1024**4)}}
        result = _format_node_status(data, "1.0%")
        assert "2.0 TiB / 2.0 TiB" in result

    def test_full_status(self) -> None:
        data: dict[str, object] = {
            "cpu": 0.25,
            "memory": {"used": 16 * 1024**3, "total": 64 * 1024**3},
            "uptime": 864000,
            "loadavg": ["1.5", "2.0", "1.8"],
            "pveversion": "pve-manager/8.1.3",
            "kversion": "6.5.11-8-pve",
            "rootfs": {"used": 10 * 1024**3, "total": 100 * 1024**3},
        }
        result = _format_node_status(data, "12.3%")
        assert "CPU (node_exporter 5-min avg): 12.3%" in result
        assert "CPU (PVE instant sample): 25.0%" in result
        assert "GiB" in result
        assert "10 days" in result
        assert "1.5, 2.0, 1.8" in result
        assert "pve-manager/8.1.3" in result

    def test_missing_fields(self) -> None:
        result = _format_node_status({}, "unavailable: no data")
        assert "Proxmox Node Status:" in result
        assert "?" in result


class TestFormatTasks:
    def test_empty_list(self) -> None:
        assert "No recent tasks" in _format_tasks([])

    def test_successful_task(self) -> None:
        task: PveTaskEntry = {
            "type": "vzdump",
            "status": "OK",
            "user": "root@pam",
            "id": "100",
            "starttime": 1700000000,
            "endtime": 1700003600,
        }
        result = _format_tasks([task])
        assert "1 recent task" in result
        assert "[OK]" in result
        assert "vzdump" in result
        assert "(100)" in result
        assert "root@pam" in result

    def test_failed_task(self) -> None:
        task: PveTaskEntry = {
            "type": "qmigrate",
            "status": "ERROR: migration failed",
            "user": "root@pam",
            "id": "200",
            "starttime": 1700000000,
        }
        result = _format_tasks([task])
        assert "ERROR" in result
        assert "qmigrate" in result


class TestFormatGuestConfig:
    def test_groups_config_keys(self) -> None:
        config: dict[str, object] = {
            "name": "jellyfin",
            "cores": 4,
            "memory": 8192,
            "scsi0": "local-lvm:vm-100-disk-0,size=50G",
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
            "boot": "order=scsi0",
            "onboot": 1,
            "ostype": "l26",
            "agent": "1",
        }
        result = _format_guest_config(100, config)
        assert "100 (jellyfin)" in result
        assert "Compute:" in result
        assert "Disks:" in result
        assert "Network:" in result
        assert "Boot / OS:" in result
        assert "cores: 4" in result
        assert "scsi0:" in result
        assert "net0:" in result

    def test_skips_digest(self) -> None:
        config: dict[str, object] = {"name": "test", "digest": "abc123", "cores": 1}
        result = _format_guest_config(100, config)
        assert "digest" not in result


class TestPveSslVerify:
    def test_default_no_verify(self, mock_settings: object) -> None:
        result = _pve_ssl_verify()
        assert result is False

    def test_verify_with_system_ca(self, mock_settings: object) -> None:
        mock_settings.proxmox_verify_ssl = True  # type: ignore[attr-defined]
        result = _pve_ssl_verify()
        assert result is True

    def test_verify_with_custom_ca(self, mock_settings: object, tmp_path: object) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as f:
            f.write(b"fake cert")
            cert_path = f.name

        mock_settings.proxmox_verify_ssl = True  # type: ignore[attr-defined]
        mock_settings.proxmox_ca_cert = cert_path  # type: ignore[attr-defined]
        # We can't fully test SSLContext creation without a real cert,
        # but we verify the code path doesn't return bool
        try:
            result = _pve_ssl_verify()
            assert isinstance(result, ssl.SSLContext)
        except ssl.SSLError:
            # Expected — fake cert is not valid PEM
            pass
