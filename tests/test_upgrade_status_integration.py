"""Integration tests for upgrade_status: a real advisor SQLite file in tmp_path and Prometheus mocked with respx."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from langchain_core.tools import ToolException

from src.agent.tools.upgrade_status import upgrade_status
from src.upgrades import store

PROM_QUERY = "http://prometheus.test:9090/api/v1/query"
NOW = datetime.now(UTC)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _use_mock_settings(mock_settings: Any) -> None:
    """Every test runs on the fake settings (Prometheus at prometheus.test, no advisor DB by default)."""


def _vector(series: list[dict[str, Any]]) -> dict[str, Any]:
    return {"status": "success", "data": {"resultType": "vector", "result": series}}


def _labels(container: str, host: str, **extra: str) -> dict[str, str]:
    # The exporter's own host is in `hostname`; the container's host is in `exported_hostname`.
    return {"container_name": container, "hostname": "infra", "exported_hostname": host, **extra}


HOMEPAGE_OUTDATED = [{"metric": _labels("homepage", "media vm"), "value": [1794369600, "1"]}]
HOMEPAGE_INFO = [
    {
        "metric": _labels(
            "homepage",
            "media vm",
            image="ghcr.io/example/homepage:latest",
            status="outdated",
            current_version="v1.2.0",
            available_version="v1.3.0",
        ),
        "value": [1794369600, "1"],
    }
]


def _prometheus(
    outdated: list[dict[str, Any]], info: list[dict[str, Any]], seen: list[str]
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        seen.append(query)
        if query.startswith("container_image_outdated"):
            return httpx.Response(200, json=_vector(outdated))
        if query.startswith("container_image_info"):
            return httpx.Response(200, json=_vector(info))
        return httpx.Response(400, json={"status": "error", "error": f"unexpected query {query}"})

    return handler


def _verdict(conn: Any, container: str, running: str, candidate: str, verdict: str, summary: str, at: datetime) -> int:
    return store.save_verdict(
        conn,
        container=container,
        running_digest=f"sha256:{running}",
        candidate_digest=f"sha256:{candidate}",
        running_version=running,
        candidate_version=candidate,
        verdict=verdict,  # type: ignore[arg-type]
        summary=summary,
        details={},
        now=at,
    )


@pytest.fixture
def advisor_db(tmp_path: Path, mock_settings: Any) -> Path:
    """An advisor store with two homepage verdicts (each deployed), one sonarr verdict, and a last success."""
    path = tmp_path / "advisor.db"
    conn = store.connect(str(path))
    old = _verdict(conn, "homepage", "v1.0.0", "v1.1.0", "upgrade", "Minor release, safe", NOW - timedelta(days=6))
    store.save_deploy(
        conn,
        verdict_id=old,
        now=NOW - timedelta(days=5),
        outcome="ok",
        running_digest="sha256:v1.1.0",
        detail="verified healthy",
        trigger_state="triggered",
    )
    new = _verdict(
        conn, "homepage", "v1.1.0", "v1.2.0", "hold", "Breaking config change in v1.2", NOW - timedelta(days=1)
    )
    store.save_deploy(
        conn,
        verdict_id=new,
        now=NOW - timedelta(hours=2),
        outcome="skipped",
        running_digest=None,
        detail="hold verdict, not deployed",
    )
    sonarr = _verdict(conn, "sonarr", "4.0.1", "4.0.2", "upgrade", "Patch release", NOW - timedelta(days=2))
    store.save_deploy(
        conn,
        verdict_id=sonarr,
        now=NOW - timedelta(days=2),
        outcome="failed",
        running_digest=None,
        detail="healthcheck failed",
        trigger_state="triggered",
    )
    store.set_last_success(conn, NOW - timedelta(hours=8))
    conn.close()
    mock_settings.upgrade_advisor_db_path = str(path)
    return path


@respx.mock
async def test_upgrade_status_shows_latest_verdict_and_deploys(advisor_db: Path) -> None:
    seen: list[str] = []
    respx.get(PROM_QUERY).mock(side_effect=_prometheus(HOMEPAGE_OUTDATED, HOMEPAGE_INFO, seen))

    result = await upgrade_status.coroutine(container="homepage", days=30)  # type: ignore[misc]

    assert "last successful run" in result and "(8h ago)" in result
    # The newest homepage verdict, not the older one, with its own deploy under it.
    assert "homepage: hold (v1.1.0 -> v1.2.0)" in result
    assert "Breaking config change in v1.2" in result
    assert "skipped" in result and "hold verdict, not deployed" in result
    # The older verdict's deploy is in the window and listed once among the other deploys.
    assert "homepage v1.0.0 -> v1.1.0: ok (trigger triggered): verified healthy" in result
    assert result.count("verified healthy") == 1
    # Other containers stay out when one is named.
    assert "sonarr" not in result
    # The exporter part: filtered on container_name, joined on the real host.
    assert all('container_name="homepage"' in q for q in seen) and len(seen) == 2
    assert "homepage on media vm: newer image available (v1.2.0 -> v1.3.0)" in result
    assert "unavailable" not in result


@respx.mock
async def test_days_limits_the_other_deploys(advisor_db: Path) -> None:
    respx.get(PROM_QUERY).mock(side_effect=_prometheus(HOMEPAGE_OUTDATED, HOMEPAGE_INFO, []))

    result = await upgrade_status.coroutine(container="homepage", days=3)  # type: ignore[misc]

    assert "verified healthy" not in result  # the 5-day-old deploy is outside a 3-day window
    assert "hold verdict, not deployed" in result  # the latest verdict's deploy shows whatever its age


@respx.mock
async def test_without_container_lists_every_latest_verdict_and_only_outdated_images(advisor_db: Path) -> None:
    seen: list[str] = []
    outdated = [
        *HOMEPAGE_OUTDATED,
        {"metric": _labels("sonarr", "media vm"), "value": [1794369600, "0"]},
    ]
    respx.get(PROM_QUERY).mock(side_effect=_prometheus(outdated, HOMEPAGE_INFO, seen))

    result = await upgrade_status.coroutine(container=None, days=30)  # type: ignore[misc]

    assert "homepage: hold (v1.1.0 -> v1.2.0)" in result
    assert "sonarr: upgrade (4.0.1 -> 4.0.2)" in result and "healthcheck failed" in result
    assert result.index("homepage: hold") < result.index("sonarr: upgrade")  # newest verdict first
    assert "1 of 2 containers have a newer image" in result
    assert "homepage on media vm: newer image available" in result
    assert "sonarr on media vm" not in result  # up-to-date images are not listed without a container
    assert sorted(seen) == ["container_image_info", "container_image_outdated"]  # no selector without a container


@respx.mock
async def test_no_verdict_for_container_says_so(advisor_db: Path) -> None:
    respx.get(PROM_QUERY).mock(side_effect=_prometheus([], [], []))

    result = await upgrade_status.coroutine(container="jellyfin", days=30)  # type: ignore[misc]

    assert "No Upgrade Advisor verdict for jellyfin" in result
    assert 'no exporter series for container_name="jellyfin"' in result


@respx.mock
async def test_not_configured_still_shows_outdated_flag(mock_settings: Any) -> None:
    mock_settings.upgrade_advisor_db_path = ""
    respx.get(PROM_QUERY).mock(side_effect=_prometheus(HOMEPAGE_OUTDATED, HOMEPAGE_INFO, []))

    result = await upgrade_status.coroutine(container="homepage", days=30)  # type: ignore[misc]

    assert "Upgrade Advisor not configured" in result
    assert "homepage on media vm: newer image available (v1.2.0 -> v1.3.0)" in result


@respx.mock
async def test_missing_db_file_is_visible_error(mock_settings: Any, tmp_path: Path) -> None:
    missing = tmp_path / "gone" / "advisor.db"
    mock_settings.upgrade_advisor_db_path = str(missing)
    respx.get(PROM_QUERY).mock(side_effect=_prometheus(HOMEPAGE_OUTDATED, HOMEPAGE_INFO, []))

    result = await upgrade_status.coroutine(container="homepage", days=30)  # type: ignore[misc]

    assert f"Upgrade Advisor DB unavailable: file not found: {missing}" in result
    assert "homepage on media vm: newer image available" in result  # the exporter part still renders
    assert not missing.exists()  # opening read-only never creates the file


@respx.mock
async def test_unreadable_db_is_visible_error(mock_settings: Any, tmp_path: Path) -> None:
    path = tmp_path / "advisor.db"
    path.write_bytes(b"not a sqlite database at all, just bytes" * 4)
    mock_settings.upgrade_advisor_db_path = str(path)
    respx.get(PROM_QUERY).mock(side_effect=_prometheus(HOMEPAGE_OUTDATED, HOMEPAGE_INFO, []))

    result = await upgrade_status.coroutine(container="homepage", days=30)  # type: ignore[misc]

    assert "Upgrade Advisor DB unavailable: DatabaseError" in result
    assert "homepage on media vm" in result


@respx.mock
async def test_prometheus_failure_still_shows_db_part(advisor_db: Path) -> None:
    respx.get(PROM_QUERY).mock(return_value=httpx.Response(503, text="service unavailable"))

    result = await upgrade_status.coroutine(container="homepage", days=30)  # type: ignore[misc]

    assert "Image freshness (container-status exporter): unavailable: HTTP 503" in result
    assert "homepage: hold (v1.1.0 -> v1.2.0)" in result


@respx.mock
async def test_prometheus_error_status_is_unavailable(advisor_db: Path) -> None:
    respx.get(PROM_QUERY).mock(return_value=httpx.Response(200, json={"status": "error", "error": "bad query"}))

    result = await upgrade_status.coroutine(container="homepage", days=30)  # type: ignore[misc]

    assert "unavailable: Prometheus error: bad query" in result


@respx.mock
async def test_every_source_failing_raises_tool_exception(mock_settings: Any) -> None:
    mock_settings.upgrade_advisor_db_path = ""
    respx.get(PROM_QUERY).mock(side_effect=httpx.ConnectError("connection refused"))

    with pytest.raises(ToolException) as excinfo:
        await upgrade_status.coroutine(container="homepage", days=30)  # type: ignore[misc]

    message = str(excinfo.value)
    assert "Upgrade Advisor not configured" in message
    assert "unavailable: ConnectError: connection refused" in message


async def test_db_reader_does_not_write(advisor_db: Path) -> None:
    """The tool opens the store read-only: the file's bytes are unchanged after a call."""
    before = advisor_db.read_bytes()
    with respx.mock:
        respx.get(PROM_QUERY).mock(side_effect=_prometheus([], [], []))
        await upgrade_status.coroutine(container=None, days=30)  # type: ignore[misc]
    assert advisor_db.read_bytes() == before
