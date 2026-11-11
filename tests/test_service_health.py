"""Unit tests for service_health: matching, verdict rules and formatting (pure, no IO)."""

from datetime import UTC, datetime

import pytest
from langchain_core.tools import ToolException
from pydantic import ValidationError

from src.agent.tools.containers import ContainerRecord, ContainerReport, ExporterStatus, Sample
from src.agent.tools.grafana_alerts import GrafanaAlert
from src.agent.tools.service_health import (
    SOURCE_NAMES,
    ServiceHealthInput,
    SourceResult,
    blackbox_source,
    container_source,
    decide,
    format_health,
    gatus_source,
    grafana_source,
    loki_error_query,
    loki_source,
    window_seconds,
)

NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)


def _gatus(name: str, key: str, group: str = "apps") -> dict[str, str]:
    return {"group": group, "key": key, "name": name, "type": "HTTP", "job": "gatus"}


def _report(*records: ContainerRecord, unavailable: dict[str, str] | None = None) -> ContainerReport:
    return ContainerReport(
        containers=list(records),
        known_hosts=sorted({r.host for r in records}),
        unknown_host=False,
        unavailable=unavailable or {},
        exporter=ExporterStatus(up=1.0, last_collection=NOW.timestamp(), stale=False, reasons=[]),
        name_filter="jellyfin",
    )


def _alert(alertname: str, summary: str = "", **labels: str) -> GrafanaAlert:
    return {
        "labels": {"alertname": alertname, "severity": "warning", **labels},
        "annotations": {"summary": summary},
        "status": {"state": "active"},
    }


OK = SourceResult("ok", "matched")
NO_MATCH = SourceResult("no match")


class TestInput:
    def test_defaults(self) -> None:
        parsed = ServiceHealthInput(name="jellyfin")
        assert parsed.window == "1h"

    @pytest.mark.parametrize("name", ["Home Assistant", "a.b(c)", "tube-archivist_2"])
    def test_accepts_names(self, name: str) -> None:
        assert ServiceHealthInput(name=name).name == name

    @pytest.mark.parametrize("name", ["", "x" * 129, 'jelly"fin', "a|b", "a{b}"])
    def test_rejects_names(self, name: str) -> None:
        with pytest.raises(ValidationError):
            ServiceHealthInput(name=name)

    def test_window_description_states_range(self) -> None:
        description = ServiceHealthInput.model_fields["window"].description or ""
        assert "5m" in description and "7d" in description


class TestWindow:
    @pytest.mark.parametrize(("window", "seconds"), [("5m", 300), ("1h", 3600), ("1h30m", 5400), ("7d", 604800)])
    def test_accepted(self, window: str, seconds: int) -> None:
        assert window_seconds(window) == seconds

    @pytest.mark.parametrize("window", ["4m", "299s", "8d", "7d1s", "2w"])
    def test_out_of_range_names_range(self, window: str) -> None:
        with pytest.raises(ToolException, match="5m to 7d"):
            window_seconds(window)

    @pytest.mark.parametrize("window", ["soon", "", "-1h"])
    def test_not_a_duration(self, window: str) -> None:
        with pytest.raises(ToolException, match="5m to 7d"):
            window_seconds(window)


class TestGatus:
    def test_matches_name_label_ignoring_case(self) -> None:
        labels = _gatus("Home Assistant", "apps_home-assistant")
        result = gatus_source([(labels, 1.0)], [(labels, 1.0)], "home assistant", "1h")
        assert result.status == "ok"
        assert "Home Assistant" in result.detail
        assert not result.down and not result.degraded

    def test_matches_key_without_group_prefix(self) -> None:
        labels = _gatus("Home Assistant", "apps_home-assistant")
        result = gatus_source([(labels, 1.0)], [(labels, 1.0)], "Home-Assistant", "1h")
        assert result.status == "ok"

    def test_substring_does_not_match(self) -> None:
        labels = _gatus("Home Assistant", "apps_home-assistant")
        result = gatus_source([(labels, 0.0)], [(labels, 0.5)], "home", "1h")
        assert result.status == "no match"
        assert not result.down and not result.degraded

    def test_full_key_with_group_does_not_match(self) -> None:
        labels = _gatus("Home Assistant", "apps_home-assistant")
        assert gatus_source([(labels, 1.0)], [], "apps_home-assistant", "1h").status == "no match"

    def test_failing_now_is_down(self) -> None:
        labels = _gatus("Jellyfin", "media_jellyfin", group="media")
        result = gatus_source([(labels, 0.0)], [(labels, 0.25)], "jellyfin", "1h")
        assert result.status == "ok"
        assert result.down == ["Gatus endpoint Jellyfin is failing now"]
        assert result.degraded == ["Gatus endpoint Jellyfin succeeded 25.0% of checks over 1h"]

    def test_ratio_below_one_is_degraded(self) -> None:
        labels = _gatus("Jellyfin", "media_jellyfin", group="media")
        result = gatus_source([(labels, 1.0)], [(labels, 0.9)], "jellyfin", "6h")
        assert not result.down
        assert result.degraded == ["Gatus endpoint Jellyfin succeeded 90.0% of checks over 6h"]


class TestContainers:
    def test_running_healthy_is_ok(self) -> None:
        record = ContainerRecord(host="media vm", name="jellyfin", image="example/jellyfin:1", state="running")
        record.health, record.restarts_24h = "healthy", 0
        result = container_source(_report(record), "1h")
        assert result.status == "ok"
        assert "media vm/jellyfin running" in result.detail
        assert not result.down and not result.degraded

    def test_not_running_is_down(self) -> None:
        record = ContainerRecord(host="media vm", name="jellyfin", image="example/jellyfin:1", state="exited")
        assert container_source(_report(record), "1h").down == ["container media vm/jellyfin is exited"]

    def test_unhealthy_and_restarts_are_degraded(self) -> None:
        record = ContainerRecord(host="media vm", name="jellyfin", image="example/jellyfin:1", state="running")
        record.health, record.restarts_24h = "unhealthy", 2
        result = container_source(_report(record), "1h")
        assert result.degraded == [
            "container media vm/jellyfin is unhealthy",
            "container media vm/jellyfin restarted 2 times in 1h",
        ]

    def test_outdated_image_is_not_a_verdict_reason(self) -> None:
        record = ContainerRecord(host="media vm", name="jellyfin", image="example/jellyfin:1", state="running")
        record.outdated = True
        result = container_source(_report(record), "1h")
        assert not result.down and not result.degraded

    def test_no_containers_is_no_match(self) -> None:
        assert container_source(_report(), "1h").status == "no match"

    def test_state_failure_makes_source_unavailable(self) -> None:
        report = _report(unavailable={"state": "HTTP 500 - boom", "health": "HTTP 500 - boom"})
        result = container_source(report, "1h")
        assert result.status == "unavailable"
        assert result.detail == "container_state: HTTP 500 - boom; container_health: HTTP 500 - boom"

    def test_other_failed_queries_noted_and_source_still_decides(self) -> None:
        record = ContainerRecord(host="media vm", name="jellyfin", image="example/jellyfin:1", state="exited")
        result = container_source(_report(record, unavailable={"image_info": "HTTP 500 - boom"}), "1h")
        assert result.status == "ok"
        assert result.detail.endswith("; container_image_info unavailable: HTTP 500 - boom")
        assert result.down == ["container media vm/jellyfin is exited"]

    def test_other_failed_queries_noted_on_no_match(self) -> None:
        result = container_source(_report(unavailable={"start_time": "timed out"}), "1h")
        assert result.status == "no match"
        assert result.text() == "no match (start time (cadvisor) unavailable: timed out)"


class TestBlackbox:
    def test_substring_of_instance_ignoring_case(self) -> None:
        samples: list[Sample] = [
            ({"instance": "https://jellyfin.example.com", "job": "blackbox_public"}, 1.0),
            ({"instance": "https://sonarr.example.com", "job": "blackbox_public"}, 0.0),
        ]
        result = blackbox_source(samples, "JellyFin")
        assert result.status == "ok"
        assert "https://jellyfin.example.com" in result.detail
        assert "sonarr" not in result.detail

    def test_failing_probe_is_degraded(self) -> None:
        samples: list[Sample] = [
            ({"instance": "http://192.0.2.5:8096", "job": "blackbox_lan"}, 0.0),
            ({"instance": "https://192.0.2.5", "job": "blackbox_public"}, 1.0),
        ]
        result = blackbox_source(samples, "192.0.2.5")
        assert "FAILING" in result.detail
        assert not result.down
        assert result.degraded == ["probe http://192.0.2.5:8096 failing now"]

    def test_no_match(self) -> None:
        samples: list[Sample] = [({"instance": "https://sonarr.example.com"}, 1.0)]
        assert blackbox_source(samples, "jellyfin").status == "no match"


class TestGrafana:
    def test_matches_label_value(self) -> None:
        alerts = [("Containers", _alert("ContainerUnhealthy", container_name="jellyfin", exported_hostname="media vm"))]
        result = grafana_source(alerts, "Jellyfin")
        assert result.status == "ok"
        assert result.degraded == ["Grafana alert ContainerUnhealthy is active"]

    def test_matches_alert_name_and_summary(self) -> None:
        alerts = [
            ("Apps", _alert("JellyfinDown")),
            ("Apps", _alert("EndpointDown", summary="Home Assistant is not answering")),
        ]
        assert len(grafana_source(alerts, "jellyfin").degraded) == 1
        assert grafana_source(alerts, "home assistant").degraded == ["Grafana alert EndpointDown is active"]

    @pytest.mark.parametrize("severity", ["none", "info", "INFO"])
    def test_informational_alert_listed_but_not_degraded(self, severity: str) -> None:
        alert = _alert("App update available", summary="Update available for jellyfin on jellyfin lxc")
        alert["labels"]["severity"] = severity
        result = grafana_source([("Containers", alert)], "jellyfin")
        assert result.status == "ok"
        assert result.degraded == []
        assert f"App update available (severity={severity}, informational) active" in result.detail

    def test_alert_without_severity_still_degraded(self) -> None:
        alert = _alert("NanoClaw down", container_name="jellyfin")
        del alert["labels"]["severity"]
        result = grafana_source([("Containers", alert)], "jellyfin")
        assert result.degraded == ["Grafana alert NanoClaw down is active"]
        assert result.detail == "NanoClaw down (no severity) active"

    def test_informational_alert_alone_gives_up(self) -> None:
        alert = _alert("App update available", container_name="jellyfin", severity="info")
        results = {key: SourceResult("no match") for key in SOURCE_NAMES}
        results["grafana"] = grafana_source([("Containers", alert)], "jellyfin")
        assert format_health("jellyfin", "1h", results).startswith("UP: jellyfin over 1h")

    def test_internal_labels_ignored(self) -> None:
        alerts = [("Apps", _alert("DiskFull", __alert_rule_uid__="jellyfin-uid"))]
        assert grafana_source(alerts, "jellyfin").status == "no match"


class TestLoki:
    def test_query_escapes_name_and_uses_error_levels(self) -> None:
        query = loki_error_query("a.b(c)", 3600)
        assert 'service_name=~"(?i).*a\\\\.b\\\\(c\\\\).*"' in query
        assert 'detected_level=~"error|err|fatal|critical|crit"' in query
        assert query.endswith("[3600s]))")

    def test_errors_are_degraded(self) -> None:
        samples: list[Sample] = [({"service_name": "jellyfin"}, 3.0), ({"service_name": "jellyfin-vue"}, 1.0)]
        result = loki_source(samples, "jellyfin", "1h")
        assert result.status == "ok"
        assert result.degraded == ["4 error log lines in Loki over 1h"]
        assert "jellyfin 3" in result.detail and "jellyfin-vue 1" in result.detail

    def test_no_error_lines_is_no_match(self) -> None:
        result = loki_source([], "jellyfin", "1h")
        assert result.status == "no match"
        assert not result.degraded


class TestVerdict:
    def test_down_wins(self) -> None:
        results = {
            "gatus": SourceResult("ok", "x", down=["Gatus endpoint X is failing now"], degraded=["ratio"]),
            "loki": SourceResult("ok", "y", degraded=["errors"]),
        }
        assert decide(results) == ("DOWN", ["Gatus endpoint X is failing now"])

    def test_degraded_lists_every_reason(self) -> None:
        results = {
            "gatus": SourceResult("ok", "x", degraded=["ratio"]),
            "loki": SourceResult("ok", "y", degraded=["e"]),
        }
        assert decide(results) == ("DEGRADED", ["ratio", "e"])

    def test_up_names_matching_sources_and_unavailable_ones(self) -> None:
        results = {"gatus": OK, "containers": OK, "loki": SourceResult("unavailable", "LOKI_URL not set")}
        verdict, reasons = decide(results)
        assert verdict == "UP"
        assert reasons == ["matched by Gatus, Containers", "unavailable: Loki errors"]

    def test_unknown_when_nothing_matches(self) -> None:
        results = {"gatus": NO_MATCH, "grafana": SourceResult("unavailable", "HTTP 500")}
        assert decide(results) == ("UNKNOWN", ["no source matched", "unavailable: Grafana alerts"])


class TestFormat:
    def test_verdict_first_then_one_line_per_source(self) -> None:
        results = {
            "gatus": SourceResult("ok", "Jellyfin: succeeding now", degraded=["ratio low"]),
            "containers": NO_MATCH,
            "blackbox": NO_MATCH,
            "grafana": NO_MATCH,
            "loki": SourceResult("unavailable", "HTTP 500 - boom"),
        }
        lines = format_health("jellyfin", "1h", results).splitlines()
        assert lines[0] == "DEGRADED: jellyfin over 1h: ratio low"
        assert lines[1:] == [
            "Gatus: ok: Jellyfin: succeeding now",
            "Containers: no match",
            "Blackbox probes: no match",
            "Grafana alerts: no match",
            "Loki errors: unavailable: HTTP 500 - boom",
        ]


class TestEdges:
    def test_gatus_ratio_without_current_result(self) -> None:
        labels = _gatus("Jellyfin", "media_jellyfin", group="media")
        result = gatus_source([], [(labels, 1.0)], "jellyfin", "1h")
        assert result.status == "ok"
        assert "no current result" in result.detail
        assert not result.down

    def test_stale_exporter_noted(self) -> None:
        record = ContainerRecord(host="media vm", name="jellyfin", image="example/jellyfin:1", state="running")
        report = _report(record)
        report.exporter = ExporterStatus(up=0.0, last_collection=None, stale=True, reasons=["up=0"])
        assert "exporter STALE (up=0)" in container_source(report, "1h").detail


class TestGatusKeyMatching:
    """Gatus builds ``key`` from the sanitised group (lower case, separators as '-'), not the raw ``group`` label."""

    @pytest.mark.parametrize(
        ("group", "key"),
        [
            ("apps", "apps_home-assistant"),
            ("Home Automation", "home-automation_home-assistant"),
            ("", "_home-assistant"),
        ],
    )
    def test_key_matches_whatever_the_group_spelling(self, group: str, key: str) -> None:
        from src.agent.tools.service_health import _gatus_matches

        labels = {"group": group, "key": key, "name": "Home Assistant"}
        assert _gatus_matches(labels, "home-assistant")
