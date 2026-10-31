"""Integration tests for the evidence collector: every source over mocked HTTP (respx) and a temporary advisor DB."""

import asyncio
import json
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from src.incidents import evidence
from src.incidents.evidence import SOURCE_NAMES, collect
from src.incidents.models import EvidenceBundle, Incident, Source
from src.upgrades import store as advisor_store

GATUS_STATUSES = "http://gatus.test:8080/api/v1/endpoints/monitoring_dozzle/statuses"
LOKI_RANGE = "http://loki.test:3100/loki/api/v1/query_range"
LOKI_LABEL_VALUES = "http://loki.test:3100/loki/api/v1/label/service_name/values"
PROM_QUERY = "http://prometheus.test:9090/api/v1/query"
PROM_RANGE = "http://prometheus.test:9090/api/v1/query_range"
PVE_TASKS = "https://proxmox.test:8006/api2/json/nodes/proxmox/tasks"

FIRST_FAILURE = datetime(2026, 10, 27, 10, 6, 44, 928614, tzinfo=UTC)
RECOVERED = datetime(2026, 10, 27, 10, 11, 44, tzinfo=UTC)
TRIGGERED = datetime(2026, 10, 27, 10, 9, 50, tzinfo=UTC)
RESOLVED = datetime(2026, 10, 27, 10, 12, 30, tzinfo=UTC)
NOW = datetime(2026, 10, 27, 10, 13, 0, tzinfo=UTC)
CONTAINER_STARTED = datetime(2026, 10, 27, 10, 9, 30, tzinfo=UTC)


def _ns(dt: datetime) -> str:
    return str(int(dt.timestamp()) * 1_000_000_000)


def _incident(*, host: str | None = "infra", container: str | None = "dozzle") -> Incident:
    return Incident(
        id=7,
        endpoint_key="monitoring_dozzle",
        group="monitoring",
        name="Dozzle",
        url="https://dozzle.example.com",
        host=host,
        container=container,
        triggered_at=TRIGGERED,
        resolved_at=RESOLVED,
        state="resolved",
        outcome=None,
        detail=None,
    )


def _by_name(bundle: EvidenceBundle) -> dict[str, Source]:
    return {s.name: s for s in bundle.sources}


# --- fake upstream responses ---


def _gatus_body() -> dict[str, Any]:
    def result(at: str, ok: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "status": 200 if ok else 502,
            "hostname": "dozzle.example.com",
            "duration": 12_000_000,
            "conditionResults": [{"condition": "[STATUS] == 200", "success": ok}],
            "success": ok,
            "timestamp": at,
        }
        if not ok:
            body["errors"] = ["unexpected status code 502"]
        return body

    return {
        "name": "Dozzle",
        "group": "monitoring",
        "key": "monitoring_dozzle",
        # Newest-first on purpose: the collector sorts by timestamp.
        "results": [
            result("2026-10-27T10:11:44.000000001Z", True),
            result("2026-10-27T10:08:44.5Z", False),
            result("2026-10-27T10:07:44.5Z", False),
            result("2026-10-27T10:06:44.928614545Z", False),
            result("2026-10-27T10:05:44.1Z", True),
        ],
        "events": [
            {"type": "START", "timestamp": "2026-10-20T08:00:00Z"},
            {"type": "UNHEALTHY", "timestamp": "2026-10-27T10:06:44.928614545Z"},
            {"type": "HEALTHY", "timestamp": "2026-10-27T10:11:44.000000001Z"},
        ],
    }


def _docker_event(action: str, at: datetime, name: str = "dozzle", **attrs: str) -> str:
    return json.dumps(
        {
            "status": action,
            "id": "2f13abcdef",
            "from": "amir20/dozzle:v11",
            "Type": "container",
            "Action": action,
            "Actor": {"ID": "2f13abcdef", "Attributes": {"image": "amir20/dozzle:v11", "name": name, **attrs}},
            "scope": "local",
            "time": int(at.timestamp()),
            "timeNano": int(at.timestamp()) * 1_000_000_000,
        }
    )


def _streams(*streams: tuple[dict[str, str], list[tuple[datetime, str]]]) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {"stream": labels, "values": [[_ns(at), text] for at, text in values]} for labels, values in streams
            ],
        },
    }


def _docker_events_response() -> dict[str, Any]:
    labels = {"service_name": "docker-events", "hostname": "infra"}
    killed = datetime(2026, 10, 27, 10, 6, 12, tzinfo=UTC)
    died = datetime(2026, 10, 27, 10, 6, 13, tzinfo=UTC)
    return _streams(
        (
            labels,
            [
                (killed, _docker_event("kill", killed, signal="15")),
                (died, _docker_event("die", died, exitCode="0")),
                (died, _docker_event("die", died, name="dozzle-agent", exitCode="0")),
                (died + timedelta(seconds=1), "garbled line"),
                (CONTAINER_STARTED, _docker_event("start", CONTAINER_STARTED)),
            ],
        )
    )


def _loki_router(
    *,
    service_names: list[str] | None = None,
    docker_events: dict[str, Any] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    names = ["docker-events", "dozzle", "sshd", "sudo", "wud"] if service_names is None else service_names

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/label/service_name/values"):
            return httpx.Response(200, json={"status": "success", "data": names})
        query = request.url.params["query"]
        if 'service_name="docker-events"' in query:
            return httpx.Response(200, json=docker_events or _docker_events_response())
        if "detected_level" in query:
            return httpx.Response(
                200,
                json=_streams(
                    (
                        {"hostname": "infra", "container": "dozzle", "detected_level": "error"},
                        [(datetime(2026, 10, 27, 10, 6, 10, tzinfo=UTC), 'level=error msg="shutting down"')],
                    ),
                ),
            )
        if 'container="dozzle"' in query:
            # direction=backward: newest first, as Loki returns it.
            return httpx.Response(
                200,
                json=_streams(
                    (
                        {"hostname": "infra", "container": "dozzle", "detected_level": "error"},
                        [(datetime(2026, 10, 27, 10, 6, 10, tzinfo=UTC), 'level=error msg="shutting down"')],
                    ),
                    (
                        {"hostname": "infra", "container": "dozzle", "detected_level": "info"},
                        [(datetime(2026, 10, 27, 10, 5, 0, tzinfo=UTC), 'level=info msg="GET /healthcheck"')],
                    ),
                ),
            )
        if "sudo|sshd" in query:
            return httpx.Response(
                200,
                json=_streams(
                    (
                        {"hostname": "infra", "service_name": "sudo"},
                        [
                            (
                                datetime(2026, 10, 27, 10, 6, 0, tzinfo=UTC),
                                "john : TTY=pts/0 ; PWD=/home/john ; USER=root ; COMMAND=/usr/bin/docker stop dozzle",
                            )
                        ],
                    ),
                ),
            )
        if "wud" in query:
            return httpx.Response(
                200,
                json=_streams(
                    (
                        {"hostname": "infra", "service_name": "wud"},
                        [(datetime(2026, 10, 27, 10, 5, 30, tzinfo=UTC), "Container dozzle: new tag v11.1 found")],
                    ),
                ),
            )
        return httpx.Response(400, text=f"unexpected query {query}")

    return route


def _vector(value: float | None) -> dict[str, Any]:
    result = [] if value is None else [{"metric": {}, "value": [NOW.timestamp(), str(value)]}]
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


def _prom_router(
    *, start: float | None = CONTAINER_STARTED.timestamp(), changes: float | None = 1.0
) -> Callable[[httpx.Request], httpx.Response]:
    boot = datetime(2026, 10, 1, 6, 0, tzinfo=UTC).timestamp()

    def route(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        if "changes(container_start_time_seconds" in query:
            return httpx.Response(200, json=_vector(changes))
        if "container_start_time_seconds" in query:
            return httpx.Response(200, json=_vector(start))
        if "node_boot_time_seconds" in query:
            return httpx.Response(200, json=_vector(boot))
        return httpx.Response(400, json={"status": "error", "error": f"unexpected {query}"})

    return route


def _prom_range_router(*, absent: tuple[datetime, datetime] | None = None) -> Callable[[httpx.Request], httpx.Response]:
    """cAdvisor's container series over the window, one sample per step, missing between ``absent`` (inclusive)."""

    def route(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        start, end, step = float(params["start"]), float(params["end"]), float(params["step"].removesuffix("s"))
        values: list[list[Any]] = []
        t = start
        while t <= end:
            at = datetime.fromtimestamp(t, tz=UTC)
            if absent is None or not absent[0] <= at <= absent[1]:
                values.append([t, str(CONTAINER_STARTED.timestamp())])
            t += step
        result = [{"metric": {}, "values": values}] if values else []
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": result}})

    return route


PVE_TASKS_BODY: dict[str, Any] = {
    "data": [
        {
            "upid": "UPID:proxmox:1:2:3:vzdump:101:root@pam:",
            "node": "proxmox",
            "type": "vzdump",
            "status": "OK",
            "user": "root@pam",
            "starttime": int(datetime(2026, 10, 27, 10, 0, 0, tzinfo=UTC).timestamp()),
            "endtime": int(datetime(2026, 10, 27, 10, 4, 0, tzinfo=UTC).timestamp()),
            "id": "101",
        }
    ]
}


@pytest.fixture
def settings(mock_settings: Any) -> Generator[Any]:
    with patch("src.incidents.evidence.get_settings", return_value=mock_settings):
        yield mock_settings


@pytest.fixture
def advisor_db(settings: Any, tmp_path: Path) -> Path:
    path = tmp_path / "advisor.db"
    conn = advisor_store.connect(str(path))
    verdict_id = advisor_store.save_verdict(
        conn,
        container="dozzle",
        running_digest="sha256:old",
        candidate_digest="sha256:new",
        running_version="v11.0",
        candidate_version="v11.1",
        verdict="upgrade",
        summary="safe",
        details={},
        now=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
    )
    advisor_store.save_deploy(
        conn,
        verdict_id=verdict_id,
        now=datetime(2026, 10, 27, 10, 6, 0, tzinfo=UTC),
        outcome="ok",
        running_digest="sha256:new",
        detail="verified",
        trigger_state="triggered",
    )
    # Outside the window: must not be reported.
    advisor_store.save_deploy(
        conn,
        verdict_id=verdict_id,
        now=datetime(2026, 10, 20, 10, 6, 0, tzinfo=UTC),
        outcome="failed",
        running_digest=None,
        detail="old",
    )
    conn.close()
    settings.upgrade_advisor_db_path = str(path)
    return path


def _mock_all(
    *,
    loki: Callable[[httpx.Request], httpx.Response] | None = None,
    prom: Callable[[httpx.Request], httpx.Response] | None = None,
    prom_range: Callable[[httpx.Request], httpx.Response] | None = None,
) -> dict[str, respx.Route]:
    loki_route = loki or _loki_router()
    return {
        "gatus": respx.get(GATUS_STATUSES).mock(return_value=httpx.Response(200, json=_gatus_body())),
        "loki_range": respx.get(LOKI_RANGE).mock(side_effect=loki_route),
        "loki_labels": respx.get(LOKI_LABEL_VALUES).mock(side_effect=loki_route),
        "prom": respx.get(PROM_QUERY).mock(side_effect=prom or _prom_router()),
        "prom_range": respx.get(PROM_RANGE).mock(side_effect=prom_range or _prom_range_router()),
        "pve": respx.get(PVE_TASKS).mock(return_value=httpx.Response(200, json=PVE_TASKS_BODY)),
    }


@pytest.mark.integration
class TestCollect:
    @respx.mock
    async def test_happy_path_all_sources_ok(self, advisor_db: Path) -> None:
        routes = _mock_all()
        bundle = await collect(_incident(), NOW)

        assert [s.name for s in bundle.sources] == list(SOURCE_NAMES)
        sources = _by_name(bundle)
        assert {name: s.status for name, s in sources.items()} == dict.fromkeys(SOURCE_NAMES, "ok")
        assert bundle.first_failure_at == FIRST_FAILURE
        assert bundle.window_start == FIRST_FAILURE - timedelta(minutes=10)
        assert bundle.window_end == RESOLVED

        gatus = sources["gatus"].lines
        assert "first failed check 10:06:44Z" in gatus
        assert "recovered 10:11:44Z" in gatus
        assert "error: unexpected status code 502" in gatus
        assert "failed condition: [STATUS] == 200" in gatus
        assert "failed checks: 3 of 5 in the window" in gatus

        assert sources["docker_events"].lines == [
            "10:06:12Z kill signal=15",
            "10:06:13Z die exitCode=0",
            "10:09:30Z start",
        ]
        assert "1 unparseable" in sources["docker_events"].detail

        logs = sources["container_logs"].lines
        assert logs == [
            '10:05:00Z [info] level=info msg="GET /healthcheck"',
            '10:06:10Z [error] level=error msg="shutting down"',
        ]

        restarts = sources["restarts"].lines
        assert "container start time changed 1 time(s) in the window" in restarts
        assert "container created 10:09:30Z" in restarts
        assert not any("no running container" in line for line in restarts)
        assert any(line.startswith("host not rebooted") for line in restarts)

        assert sources["proxmox_tasks"].lines == ["10:00:00Z vzdump guest=101 user=root@pam status=OK"]
        assert sources["logins"].lines[0].startswith("10:06:00Z sudo john : TTY=pts/0")
        assert sources["deploys"].lines == [
            "10:05:30Z wud Container dozzle: new tag v11.1 found",
            "10:06:00Z advisor deploy v11.0 -> v11.1 outcome=ok trigger=triggered",
        ]

        # The queries carry the window and the right selectors.
        pve_params = routes["pve"].calls.last.request.url.params
        assert pve_params["since"] == str(int(bundle.window_start.timestamp()))
        assert pve_params["until"] == str(int(RESOLVED.timestamp()))
        loki_queries = [c.request.url.params.get("query", "") for c in routes["loki_range"].calls]
        assert '{service_name="docker-events", hostname="infra"} |= "dozzle"' in loki_queries
        prom_queries = [c.request.url.params["query"] for c in routes["prom"].calls]
        assert any('name="dozzle", hostname="infra"' in q for q in prom_queries)

    @respx.mock
    async def test_loki_down_keeps_gatus_restarts_and_proxmox(self, settings: Any) -> None:
        _mock_all(loki=lambda _request: httpx.Response(500, text="internal error"))
        bundle = await collect(_incident(), NOW)
        sources = _by_name(bundle)

        for name in ("docker_events", "container_logs", "logins", "deploys"):
            assert sources[name].status == "unavailable", name
            assert "HTTP 500" in sources[name].detail
            assert "loki.test" not in sources[name].detail
        for name in ("gatus", "restarts", "proxmox_tasks"):
            assert sources[name].status == "ok", name
            assert sources[name].lines

    @respx.mock
    async def test_gatus_timeout_uses_trigger_fallback(self, settings: Any) -> None:
        routes = _mock_all()
        routes["gatus"].mock(side_effect=httpx.ConnectTimeout("timed out"))
        bundle = await collect(_incident(), NOW)
        gatus = _by_name(bundle)["gatus"]

        assert gatus.status == "unavailable"
        assert "timed out" in gatus.detail
        assert "TRIGGERED" in gatus.detail
        assert bundle.first_failure_at is None
        assert bundle.window_start == TRIGGERED - timedelta(minutes=13)
        assert bundle.window_end == RESOLVED
        # Other sources still collected over the fallback window.
        assert _by_name(bundle)["proxmox_tasks"].status == "ok"

    async def test_source_timeout_becomes_unavailable(self, settings: Any) -> None:
        async def slow(*_args: object, **_kwargs: object) -> dict[str, object]:
            await asyncio.sleep(5)
            return {}

        settings.loki_url = ""
        settings.prometheus_url = ""
        settings.proxmox_url = ""
        with (
            patch.object(evidence, "SOURCE_TIMEOUT_SECONDS", 0.05),
            patch.object(evidence, "_fetch_gatus", slow),
        ):
            bundle = await collect(_incident(), NOW)
        gatus = _by_name(bundle)["gatus"]
        assert gatus.status == "unavailable"
        assert "timed out after 0.05 s" in gatus.detail

    @respx.mock
    async def test_no_host_collects_only_gatus_and_proxmox(self, settings: Any) -> None:
        routes = _mock_all()
        bundle = await collect(_incident(host=None, container=None), NOW)
        sources = _by_name(bundle)

        assert sources["gatus"].status == "ok"
        assert sources["proxmox_tasks"].status == "ok"
        for name in ("docker_events", "container_logs", "restarts", "logins", "deploys"):
            assert sources[name].status == "skipped", name
            assert sources[name].detail == "no host mapped for this check"
        assert not routes["loki_range"].called
        assert not routes["loki_labels"].called
        assert not routes["prom"].called

    @respx.mock
    async def test_host_without_container(self, settings: Any) -> None:
        routes = _mock_all()
        bundle = await collect(_incident(container=None), NOW)
        sources = _by_name(bundle)

        assert sources["docker_events"].status == "skipped"
        assert sources["container_logs"].status == "skipped"
        assert "no container" in sources["docker_events"].detail
        assert sources["restarts"].status == "ok"
        assert "no container" in sources["restarts"].detail
        assert any(line.startswith("host not rebooted") for line in sources["restarts"].lines)
        assert sources["deploys"].status == "ok"
        assert "no container" in sources["deploys"].detail
        loki_queries = [c.request.url.params.get("query", "") for c in routes["loki_range"].calls]
        assert '{hostname="infra", service_name=~"wud|wud-advise"}' in loki_queries
        prom_queries = [c.request.url.params["query"] for c in routes["prom"].calls]
        assert all("container_start_time_seconds" not in q for q in prom_queries)

    @respx.mock
    async def test_window_override_respected(self, settings: Any) -> None:
        routes = _mock_all()
        start = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
        end = datetime(2026, 10, 27, 10, 30, tzinfo=UTC)
        bundle = await collect(_incident(), NOW, window=(start, end))

        assert (bundle.window_start, bundle.window_end) == (start, end)
        assert bundle.first_failure_at == FIRST_FAILURE
        assert "window set by the caller" in _by_name(bundle)["gatus"].detail
        pve_params = routes["pve"].calls.last.request.url.params
        assert pve_params["since"] == str(int(start.timestamp()))
        assert pve_params["until"] == str(int(end.timestamp()))
        label_params = routes["loki_labels"].calls.last.request.url.params
        assert label_params["start"] == str(int(start.timestamp() * 1_000_000_000))
        assert label_params["end"] == str(int(end.timestamp() * 1_000_000_000))

    @respx.mock
    async def test_host_without_login_logs_skips_logins(self, settings: Any) -> None:
        routes = _mock_all(loki=_loki_router(service_names=["docker-events", "dozzle"]))
        bundle = await collect(_incident(), NOW)
        logins = _by_name(bundle)["logins"]

        assert logins.status == "skipped"
        assert logins.detail == "host does not ship login logs"
        loki_queries = [c.request.url.params.get("query", "") for c in routes["loki_range"].calls]
        assert all("sudo|sshd" not in q for q in loki_queries)

    @respx.mock
    async def test_unconfigured_backends_are_skipped(self, settings: Any) -> None:
        settings.loki_url = ""
        settings.prometheus_url = ""
        settings.gatus_url = ""
        settings.proxmox_url = ""
        bundle = await collect(_incident(), NOW)
        sources = _by_name(bundle)

        assert {s.status for s in bundle.sources} == {"skipped"}
        assert sources["gatus"].detail.startswith("Gatus not configured")
        assert "TRIGGERED" in sources["gatus"].detail
        assert sources["docker_events"].detail == "Loki not configured"
        assert sources["restarts"].detail == "Prometheus not configured"
        assert sources["proxmox_tasks"].detail == "Proxmox not configured"
        assert bundle.window_start == TRIGGERED - timedelta(minutes=13)

    @respx.mock
    async def test_docker_events_capped(self, settings: Any) -> None:
        base = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)
        many = _streams(
            (
                {"service_name": "docker-events", "hostname": "infra"},
                [
                    (
                        base + timedelta(seconds=i),
                        _docker_event("health_status: unhealthy", base + timedelta(seconds=i)),
                    )
                    for i in range(45)
                ],
            )
        )
        _mock_all(loki=_loki_router(docker_events=many))
        bundle = await collect(_incident(), NOW)
        source = _by_name(bundle)["docker_events"]

        assert source.status == "ok"
        assert len(source.lines) == evidence.MAX_LINES_PER_SOURCE
        assert "25 more" in source.detail

    @respx.mock
    async def test_proxmox_host_maps_to_prometheus_name(self, settings: Any) -> None:
        routes = _mock_all(prom=_prom_router(start=None, changes=None))
        bundle = await collect(_incident(host="pve", container="pve-exporter"), NOW)

        prom_queries = [c.request.url.params["query"] for c in routes["prom"].calls]
        assert prom_queries and all('hostname="proxmox"' in q for q in prom_queries)
        loki_queries = [c.request.url.params.get("query", "") for c in routes["loki_range"].calls]
        assert loki_queries and all('hostname="pve"' in q for q in loki_queries)
        restarts = _by_name(bundle)["restarts"]
        assert "no cAdvisor series for pve-exporter on proxmox" in restarts.lines

    @respx.mock
    async def test_missing_advisor_db_marks_deploys_unavailable_but_keeps_wud_lines(
        self, settings: Any, tmp_path: Path
    ) -> None:
        settings.upgrade_advisor_db_path = str(tmp_path / "missing.db")
        _mock_all()
        bundle = await collect(_incident(), NOW)
        deploys = _by_name(bundle)["deploys"]

        assert deploys.status == "unavailable"
        assert "advisor DB" in deploys.detail
        assert deploys.lines == ["10:05:30Z wud Container dozzle: new tag v11.1 found"]

    @respx.mock
    async def test_proxmox_error_is_unavailable(self, settings: Any) -> None:
        routes = _mock_all()
        routes["pve"].mock(return_value=httpx.Response(401, text="no ticket"))
        bundle = await collect(_incident(), NOW)
        source = _by_name(bundle)["proxmox_tasks"]
        assert source.status == "unavailable"
        assert source.detail == "HTTP 401"
        assert "fake-token" not in source.detail

    @respx.mock
    async def test_prometheus_error_is_unavailable(self, settings: Any) -> None:
        _mock_all(prom=lambda _request: httpx.Response(200, json={"status": "error", "error": "bad query"}))
        bundle = await collect(_incident(), NOW)
        restarts = _by_name(bundle)["restarts"]
        assert restarts.status == "unavailable"
        assert "bad query" in restarts.detail

    @respx.mock
    async def test_bug_in_one_source_does_not_fail_the_bundle(self, settings: Any) -> None:
        async def broken(*_args: object, **_kwargs: object) -> Source:
            raise RuntimeError("boom")

        _mock_all()
        with patch.object(evidence, "_logins", broken):
            bundle = await collect(_incident(), NOW)
        sources = _by_name(bundle)
        assert sources["logins"].status == "unavailable"
        assert sources["logins"].detail == "internal error (RuntimeError)"
        assert sources["gatus"].status == "ok"

    @respx.mock
    async def test_gatus_unexpected_body_is_unavailable(self, settings: Any) -> None:
        routes = _mock_all()
        routes["gatus"].mock(return_value=httpx.Response(200, json=["not", "an", "object"]))
        bundle = await collect(_incident(), NOW)
        gatus = _by_name(bundle)["gatus"]
        assert gatus.status == "unavailable"
        assert gatus.detail.startswith("unexpected response from Gatus")

    @respx.mock
    async def test_container_error_lines_capped_after_lines_before_failure(self, settings: Any) -> None:
        base = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)
        labels = {"hostname": "infra", "container": "dozzle", "detected_level": "warn"}
        warnings = _streams((labels, [(base + timedelta(seconds=i), f"warn {i}") for i in range(30)]))
        fallback = _loki_router()

        def route(request: httpx.Request) -> httpx.Response:
            if "detected_level" in request.url.params.get("query", ""):
                return httpx.Response(200, json=warnings)
            return fallback(request)

        _mock_all(loki=route)
        bundle = await collect(_incident(), NOW)
        logs = _by_name(bundle)["container_logs"]
        assert logs.status == "ok"
        assert len(logs.lines) == evidence.MAX_LINES_PER_SOURCE
        assert '10:05:00Z [info] level=info msg="GET /healthcheck"' in logs.lines
        assert "more error/warn lines not shown" in logs.detail


@pytest.mark.integration
class TestRestartGaps:
    """A stop and start of the same container keeps cAdvisor's start time; the gap in its series is the signal."""

    @respx.mock
    async def test_gap_in_the_cadvisor_series_is_reported(self, settings: Any) -> None:
        created = datetime(2026, 10, 27, 9, 26, 15, tzinfo=UTC).timestamp()
        absent = (datetime(2026, 10, 27, 10, 7, 0, tzinfo=UTC), datetime(2026, 10, 27, 10, 9, 30, tzinfo=UTC))
        _mock_all(prom=_prom_router(start=created, changes=0.0), prom_range=_prom_range_router(absent=absent))
        bundle = await collect(_incident(), NOW)

        restarts = _by_name(bundle)["restarts"]
        assert restarts.status == "ok"
        assert any(line.startswith("cAdvisor saw no running container from 10:07:") for line in restarts.lines), (
            restarts.lines
        )
        assert any("to 10:09:" in line for line in restarts.lines if "no running container" in line)
        assert not any("not restarted" in line for line in restarts.lines)
        assert any(line.startswith("container start time unchanged") for line in restarts.lines)

    @respx.mock
    async def test_no_samples_in_the_window_says_so(self, settings: Any) -> None:
        everything = (datetime(2026, 10, 27, 0, 0, tzinfo=UTC), datetime(2026, 10, 28, 0, 0, tzinfo=UTC))
        _mock_all(prom=_prom_router(start=None, changes=None), prom_range=_prom_range_router(absent=everything))
        bundle = await collect(_incident(), NOW)
        lines = _by_name(bundle)["restarts"].lines
        assert any("no running container in the whole window" in line for line in lines), lines

    @respx.mock
    async def test_long_window_widens_the_range_query_step(self, settings: Any) -> None:
        """Prometheus rejects a range query with more than 11,000 points per series; a 5-day window at 30 s needs
        14,400, so the step must widen."""
        series = _prom_range_router()

        def strict(request: httpx.Request) -> httpx.Response:
            params = request.url.params
            span = float(params["end"]) - float(params["start"])
            if span / float(params["step"].removesuffix("s")) > 11_000:
                body = {"status": "error", "errorType": "bad_data", "error": "exceeded maximum resolution"}
                return httpx.Response(400, json=body)
            return series(request)

        routes = _mock_all(prom_range=strict)
        start = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
        end = start + timedelta(days=5)
        bundle = await collect(_incident(), NOW, window=(start, end))

        step = float(routes["prom_range"].calls.last.request.url.params["step"].removesuffix("s"))
        assert step >= 216
        restarts = _by_name(bundle)["restarts"]
        assert restarts.status == "ok", restarts.detail
        assert not any("no running container" in line for line in restarts.lines), restarts.lines
