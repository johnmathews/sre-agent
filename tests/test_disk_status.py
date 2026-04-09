"""Unit tests for the HDD power status composite tool."""

import pytest
from langchain_core.tools import ToolException

from src.agent.tools.disk_status import (
    _ACTIVE_STATES,
    _ERROR_STATES,
    _STANDBY_STATES,
    POWER_STATE_LABELS,
    _build_disk_lookup,
    _compute_stats_from_data,
    _compute_time_in_state,
    _count_group_transitions,
    _extract_hex,
    _extract_transitions_from_data,
    _format_disk_name,
    _format_power_state,
    _resolve_pool_filter,
    _select_step,
    _state_group,
)
from src.agent.tools.prometheus import PrometheusSeries
from src.agent.tools.truenas import TruenasDiskEntry


class TestExtractHex:
    """Tests for hex extraction used in device_id ↔ identifier cross-referencing."""

    def test_extracts_from_prometheus_device_id(self) -> None:
        device_id = "/dev/disk/by-id/wwn-0x5000c500eb02b449"
        assert _extract_hex(device_id) == "5000c500eb02b449"

    def test_extracts_from_truenas_identifier(self) -> None:
        identifier = "{serial_lunid}5000c500eb02b449"
        assert _extract_hex(identifier) == "5000c500eb02b449"

    def test_extracts_longest_hex_sequence(self) -> None:
        # If multiple hex-like substrings, pick the longest
        s = "abc-12345678-9abcdef012345678"
        assert _extract_hex(s) == "9abcdef012345678"

    def test_returns_empty_for_no_hex(self) -> None:
        assert _extract_hex("no-hex-here") == ""
        assert _extract_hex("short-1234") == ""  # < 8 chars

    def test_case_insensitive(self) -> None:
        assert _extract_hex("ABCDEF0123456789") == "abcdef0123456789"

    def test_matching_prometheus_to_truenas(self) -> None:
        """Verify that the same hex is extracted from both formats for the same disk."""
        prom_id = "/dev/disk/by-id/wwn-0x5000c500f742ccbf"
        truenas_id = "{serial_lunid}5000c500f742ccbf"
        assert _extract_hex(prom_id) == _extract_hex(truenas_id)


class TestBuildDiskLookup:
    def test_builds_lookup_from_disk_entries(self) -> None:
        disks: list[TruenasDiskEntry] = [
            TruenasDiskEntry(
                identifier="{serial_lunid}5000c500eb02b449",
                name="sdc",
                model="ST8000VN004",
                serial="WWZ5TZSF",
                type="HDD",
                size=8_000_000_000_000,
            ),
            TruenasDiskEntry(
                identifier="{serial_lunid}5000c500f742ccbf",
                name="sdf",
                model="ST16000NT001",
                serial="K3S04BKQ",
                type="HDD",
                size=16_000_000_000_000,
            ),
        ]
        lookup = _build_disk_lookup(disks)
        assert "5000c500eb02b449" in lookup
        assert lookup["5000c500eb02b449"]["name"] == "sdc"
        assert "5000c500f742ccbf" in lookup
        assert lookup["5000c500f742ccbf"]["name"] == "sdf"

    def test_ignores_entries_without_hex_identifier(self) -> None:
        disks: list[TruenasDiskEntry] = [
            TruenasDiskEntry(identifier="", name="sda"),
        ]
        lookup = _build_disk_lookup(disks)
        assert len(lookup) == 0


class TestFormatDiskName:
    def test_formats_with_disk_entry(self) -> None:
        disk = TruenasDiskEntry(
            name="sdc",
            model="ST8000VN004",
            serial="WWZ5TZSF",
            size=8_000_000_000_000,
        )
        result = _format_disk_name(disk, "irrelevant")
        assert "sdc" in result
        assert "ST8000VN004" in result
        assert "WWZ5TZSF" in result

    def test_falls_back_to_short_device_id(self) -> None:
        result = _format_disk_name(None, "/dev/disk/by-id/wwn-0x5000c500eb02b449")
        assert result == "wwn-0x5000c500eb02b449"
        assert "/dev/disk/by-id/" not in result

    def test_handles_device_id_without_path(self) -> None:
        result = _format_disk_name(None, "some-device-id")
        assert result == "some-device-id"


class TestFormatPowerState:
    def test_known_states(self) -> None:
        assert "error" in _format_power_state(-2)
        assert "unknown" in _format_power_state(-1)
        assert "standby" in _format_power_state(0)
        assert "idle" in _format_power_state(1)
        assert "active_or_idle" in _format_power_state(2)
        assert "idle_a" in _format_power_state(3)
        assert "idle_b" in _format_power_state(4)
        assert "idle_c" in _format_power_state(5)
        assert "active" in _format_power_state(6)
        assert "sleep" in _format_power_state(7)

    def test_unmapped_state_includes_value(self) -> None:
        result = _format_power_state(99)
        assert "unknown state" in result
        assert "99" in result

    def test_all_documented_states_have_labels(self) -> None:
        for value in (-2, -1, 0, 1, 2, 3, 4, 5, 6, 7):
            assert value in POWER_STATE_LABELS

    def test_state_sets_cover_all_values(self) -> None:
        """Active + standby + error sets should cover all mapped values."""
        all_states = _ACTIVE_STATES | _STANDBY_STATES | _ERROR_STATES
        for value in POWER_STATE_LABELS:
            assert value in all_states, f"Value {value} not in any classification set"

    def test_state_sets_are_disjoint(self) -> None:
        assert set() == _ACTIVE_STATES & _STANDBY_STATES
        assert set() == _ACTIVE_STATES & _ERROR_STATES
        assert set() == _STANDBY_STATES & _ERROR_STATES

    def test_standby_and_sleep_are_spun_down(self) -> None:
        assert 0 in _STANDBY_STATES  # standby
        assert 7 in _STANDBY_STATES  # sleep

    def test_idle_and_active_are_spun_up(self) -> None:
        for value in (1, 2, 3, 4, 5, 6):
            assert value in _ACTIVE_STATES


class TestStateGroup:
    """Tests for _state_group classification used in transition counting."""

    def test_active_states_classified_as_active(self) -> None:
        for value in (1, 2, 3, 4, 5, 6):
            assert _state_group(float(value)) == "active"

    def test_standby_states_classified_as_standby(self) -> None:
        assert _state_group(0.0) == "standby"
        assert _state_group(7.0) == "standby"

    def test_error_states_classified_as_error(self) -> None:
        assert _state_group(-2.0) == "error"
        assert _state_group(-1.0) == "error"


class TestCountGroupTransitions:
    """Tests for _count_group_transitions — the core fix for inflated change counts."""

    def test_empty_values(self) -> None:
        assert _count_group_transitions([]) == 0

    def test_single_value(self) -> None:
        assert _count_group_transitions([[1700000000, "2"]]) == 0

    def test_no_transitions_same_group(self) -> None:
        """Sub-state fluctuations within the same group are NOT counted."""
        values = [
            [1700000000, "3"],  # idle_a (active)
            [1700000060, "4"],  # idle_b (active)
            [1700000120, "5"],  # idle_c (active)
            [1700000180, "3"],  # idle_a (active)
            [1700000240, "6"],  # active (active)
        ]
        assert _count_group_transitions(values) == 0

    def test_one_transition_standby_to_active(self) -> None:
        values = [
            [1700000000, "0"],  # standby
            [1700000060, "0"],
            [1700000120, "2"],  # active
            [1700000180, "2"],
        ]
        assert _count_group_transitions(values) == 1

    def test_multiple_transitions(self) -> None:
        """standby → active → standby → active = 3 transitions."""
        values = [
            [1700000000, "0"],  # standby
            [1700000060, "4"],  # active (idle_b)
            [1700000120, "0"],  # standby
            [1700000180, "2"],  # active
        ]
        assert _count_group_transitions(values) == 3

    def test_ignores_sub_state_noise_around_real_transition(self) -> None:
        """idle_a → idle_b → standby = 1 real transition, not 2."""
        values = [
            [1700000000, "3"],  # idle_a (active)
            [1700000060, "4"],  # idle_b (active) — sub-state noise
            [1700000120, "5"],  # idle_c (active) — sub-state noise
            [1700000180, "0"],  # standby — real transition!
        ]
        assert _count_group_transitions(values) == 1


class TestComputeTimeInState:
    """Tests for _compute_time_in_state percentage calculations."""

    def test_empty_values(self) -> None:
        result = _compute_time_in_state([])
        assert result == {"active": 0.0, "standby": 0.0, "error": 0.0}

    def test_single_value(self) -> None:
        result = _compute_time_in_state([[1700000000, "2"]])
        assert result == {"active": 0.0, "standby": 0.0, "error": 0.0}

    def test_all_standby(self) -> None:
        values = [
            [1700000000, "0"],
            [1700000060, "0"],
            [1700000120, "0"],
        ]
        result = _compute_time_in_state(values)
        assert result["standby"] == 100.0
        assert result["active"] == 0.0

    def test_all_active(self) -> None:
        values = [
            [1700000000, "3"],
            [1700000060, "4"],  # sub-state change, still active
            [1700000120, "6"],
        ]
        result = _compute_time_in_state(values)
        assert result["active"] == 100.0
        assert result["standby"] == 0.0

    def test_half_and_half(self) -> None:
        """Equal time in standby and active."""
        values = [
            [1700000000, "0"],  # standby for 100s
            [1700000100, "2"],  # active for 100s
            [1700000200, "2"],
        ]
        result = _compute_time_in_state(values)
        assert result["standby"] == 50.0
        assert result["active"] == 50.0

    def test_mostly_standby(self) -> None:
        """75% standby, 25% active."""
        values = [
            [1700000000, "0"],  # standby for 300s
            [1700000300, "2"],  # active for 100s
            [1700000400, "2"],
        ]
        result = _compute_time_in_state(values)
        assert result["standby"] == 75.0
        assert result["active"] == 25.0


class TestResolvePoolFilter:
    """Tests for _resolve_pool_filter — Python-side pool filtering."""

    def _make_series(self, device_id: str, pool: str = "") -> PrometheusSeries:
        metric: dict[str, str] = {"device_id": device_id, "type": "hdd"}
        if pool:
            metric["pool"] = pool
        return PrometheusSeries(metric=metric, value=[1700000000, "2"])

    def test_matches_via_truenas_inventory(self) -> None:
        """Primary path: TrueNAS disk inventory has pool info."""
        power_states = [
            self._make_series("/dev/disk/by-id/wwn-0x5000c500eb02b449"),
            self._make_series("/dev/disk/by-id/wwn-0x5000c500f742ccbf"),
        ]
        disk_lookup = _build_disk_lookup(
            [
                TruenasDiskEntry(
                    identifier="{serial_lunid}5000c500eb02b449",
                    name="sdc",
                    pool="tank",
                ),
                TruenasDiskEntry(
                    identifier="{serial_lunid}5000c500f742ccbf",
                    name="sdf",
                    pool="backup",
                ),
            ]
        )
        result = _resolve_pool_filter("tank", power_states, disk_lookup)
        assert result == {"/dev/disk/by-id/wwn-0x5000c500eb02b449"}

    def test_falls_back_to_prometheus_label(self) -> None:
        """When TrueNAS has no pool info, uses Prometheus pool label."""
        power_states = [
            self._make_series("/dev/disk/by-id/wwn-0x5000c500eb02b449", pool="tank"),
            self._make_series("/dev/disk/by-id/wwn-0x5000c500f742ccbf", pool="backup"),
        ]
        # Empty disk_lookup — TrueNAS not configured
        result = _resolve_pool_filter("backup", power_states, {})
        assert result == {"/dev/disk/by-id/wwn-0x5000c500f742ccbf"}

    def test_raises_with_available_pools_from_truenas(self) -> None:
        """When pool doesn't exist, error lists available pools from TrueNAS."""
        power_states = [self._make_series("/dev/disk/by-id/wwn-0x5000c500eb02b449")]
        disk_lookup = _build_disk_lookup(
            [
                TruenasDiskEntry(
                    identifier="{serial_lunid}5000c500eb02b449",
                    name="sdc",
                    pool="tank",
                ),
            ]
        )
        with pytest.raises(ToolException, match="No HDDs found in pool 'nonexistent'"):
            _resolve_pool_filter("nonexistent", power_states, disk_lookup)

    def test_raises_with_available_pools_from_prometheus(self) -> None:
        """When TrueNAS has no pool info, error lists pools from Prometheus labels."""
        power_states = [
            self._make_series("/dev/disk/by-id/wwn-0xaaa", pool="tank"),
            self._make_series("/dev/disk/by-id/wwn-0xbbb", pool="backup"),
        ]
        with pytest.raises(ToolException, match="backup, tank"):
            _resolve_pool_filter("nonexistent", power_states, {})


class TestSelectStep:
    """Tests for _select_step Prometheus step selection."""

    def test_short_duration(self) -> None:
        assert _select_step(3600) == "15s"  # 1h

    def test_medium_duration(self) -> None:
        assert _select_step(43200) == "60s"  # 12h

    def test_long_duration(self) -> None:
        assert _select_step(604800) == "5m"  # 7d


class TestComputeStatsFromData:
    """Tests for _compute_stats_from_data — pure function computing stats from raw series."""

    def test_empty_data(self) -> None:
        assert _compute_stats_from_data([]) == {}

    def test_computes_stats_from_series(self) -> None:
        data: list[PrometheusSeries] = [
            PrometheusSeries(
                metric={"device_id": "disk1"},
                values=[
                    [1700000000, "0"],  # standby
                    [1700000060, "0"],
                    [1700000120, "2"],  # active (1 transition)
                    [1700000180, "2"],
                ],
            ),
        ]
        stats = _compute_stats_from_data(data)
        assert "disk1" in stats
        assert stats["disk1"].change_count == 1
        assert stats["disk1"].standby_pct > 0
        assert stats["disk1"].active_pct > 0

    def test_no_transitions_within_group(self) -> None:
        """Sub-state fluctuations don't count as transitions."""
        data: list[PrometheusSeries] = [
            PrometheusSeries(
                metric={"device_id": "disk1"},
                values=[[1700000000, "3"], [1700000060, "4"], [1700000120, "5"]],
            ),
        ]
        stats = _compute_stats_from_data(data)
        assert stats["disk1"].change_count == 0
        assert stats["disk1"].active_pct == 100.0


class TestExtractTransitionsFromData:
    """Tests for _extract_transitions_from_data — pure function extracting transitions."""

    def test_empty_data(self) -> None:
        assert _extract_transitions_from_data([]) == {}

    def test_finds_all_transitions(self) -> None:
        data: list[PrometheusSeries] = [
            PrometheusSeries(
                metric={"device_id": "disk1"},
                values=[
                    [1700000000, "0"],  # standby
                    [1700000060, "2"],  # active (1st transition)
                    [1700000120, "0"],  # standby (2nd transition)
                    [1700000180, "4"],  # active (3rd transition)
                ],
            ),
        ]
        transitions = _extract_transitions_from_data(data)
        assert "disk1" in transitions
        assert len(transitions["disk1"]) == 3
        # First: standby → active_or_idle
        assert "standby" in transitions["disk1"][0]
        assert "active_or_idle" in transitions["disk1"][0]
        # Second: active_or_idle → standby
        assert "standby" in transitions["disk1"][1]
        # Third: standby → idle_b
        assert "idle_b" in transitions["disk1"][2]

    def test_no_group_transitions(self) -> None:
        """Disks with only sub-state changes produce no transition entries."""
        data: list[PrometheusSeries] = [
            PrometheusSeries(
                metric={"device_id": "disk1"},
                values=[[1700000000, "3"], [1700000060, "4"], [1700000120, "5"]],
            ),
        ]
        assert _extract_transitions_from_data(data) == {}

    def test_single_value_no_transition(self) -> None:
        data: list[PrometheusSeries] = [
            PrometheusSeries(
                metric={"device_id": "disk1"},
                values=[[1700000000, "0"]],
            ),
        ]
        assert _extract_transitions_from_data(data) == {}

    def test_chronological_order(self) -> None:
        """Transitions are returned in chronological (forward) order."""
        data: list[PrometheusSeries] = [
            PrometheusSeries(
                metric={"device_id": "disk1"},
                values=[
                    [1700000000, "0"],  # standby
                    [1700000060, "4"],  # active
                    [1700000120, "0"],  # standby
                ],
            ),
        ]
        transitions = _extract_transitions_from_data(data)
        # First transition timestamp should be earlier than second
        assert "00:01:00" in transitions["disk1"][0] or "2023" in transitions["disk1"][0]
        assert len(transitions["disk1"]) == 2
