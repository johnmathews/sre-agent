"""Integration tests for the WUD client (mocked HTTP)."""

import base64
from typing import Any

import httpx
import pytest
import respx

from src.upgrades.config import AdvisedContainer
from src.upgrades.wud import WudError, get_container, trigger_deploy

WUD_URL = "http://10.0.0.15:3010"


@pytest.fixture(autouse=True)
def _use_mock_settings(mock_settings: Any) -> None:
    mock_settings.upgrade_advisor_wud_username = "advisor"
    mock_settings.upgrade_advisor_wud_password = "s3cret"


def _container(name: str = "prometheus") -> AdvisedContainer:
    return AdvisedContainer(
        name=name,
        host="prometheus",
        wud_url=WUD_URL,
        registry="registry-1.docker.io",
        repository="prom/prometheus",
        tag="v3",
        github_repo="prometheus/prometheus",
        readiness_url="http://10.0.0.15:9090/-/ready",
        pbs_guest="CT 115",
        context_files=[],
    )


def _record(name: str, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": f"id-{name}",
        "name": name,
        "image": {
            "digest": {"watch": True, "value": "sha256:running", "repo": "sha256:repo"},
            "tag": {"value": "v3", "semver": True},
        },
        "result": {"tag": "v3", "digest": "sha256:candidate", "created": "2026-10-20T00:00:00Z"},
        "updateAvailable": True,
        "updateKind": {"kind": "digest", "localValue": "sha256:running", "remoteValue": "sha256:candidate"},
    }
    record.update(overrides)
    return record


def _expected_auth() -> str:
    return "Basic " + base64.b64encode(b"advisor:s3cret").decode()


@pytest.mark.integration
class TestGetContainer:
    @respx.mock
    async def test_parses_wud_record_and_matches_on_name(self) -> None:
        route = respx.get(f"{WUD_URL}/api/containers").mock(
            return_value=httpx.Response(200, json=[_record("other"), _record("prometheus")])
        )
        got = await get_container(_container())
        assert got.id == "id-prometheus"
        assert got.name == "prometheus"
        assert got.running_digest == "sha256:running"
        assert got.candidate_digest == "sha256:candidate"
        assert got.update_available is True
        assert got.update_kind == "digest"
        assert route.calls[0].request.headers["authorization"] == _expected_auth()

    @respx.mock
    async def test_tolerates_missing_result_and_update_kind(self) -> None:
        record = _record("prometheus", updateAvailable=False)
        del record["result"]
        del record["updateKind"]
        respx.get(f"{WUD_URL}/api/containers").mock(return_value=httpx.Response(200, json=[record]))
        got = await get_container(_container())
        assert got.candidate_digest is None
        assert got.update_kind is None
        assert got.update_available is False

    @respx.mock
    async def test_tag_update_kind_is_carried(self) -> None:
        record = _record("prometheus", updateKind={"kind": "tag", "localValue": "v3.1", "remoteValue": "v3.2"})
        respx.get(f"{WUD_URL}/api/containers").mock(return_value=httpx.Response(200, json=[record]))
        assert (await get_container(_container())).update_kind == "tag"

    @respx.mock
    async def test_401_raises(self) -> None:
        respx.get(f"{WUD_URL}/api/containers").mock(return_value=httpx.Response(401))
        with pytest.raises(WudError, match="401") as exc:
            await get_container(_container())
        assert "prometheus" in str(exc.value)
        assert "10.0.0.15" in str(exc.value)

    @respx.mock
    async def test_missing_container_raises(self) -> None:
        respx.get(f"{WUD_URL}/api/containers").mock(return_value=httpx.Response(200, json=[_record("other")]))
        with pytest.raises(WudError, match="prometheus"):
            await get_container(_container())

    @respx.mock
    async def test_network_error_raises_wud_error(self) -> None:
        respx.get(f"{WUD_URL}/api/containers").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(WudError, match="prometheus"):
            await get_container(_container())


@pytest.mark.integration
class TestTriggerDeploy:
    @respx.mock
    async def test_posts_exact_path_with_basic_auth(self) -> None:
        route = respx.post(f"{WUD_URL}/api/containers/abc123/triggers/dockercompose/deploy").mock(
            return_value=httpx.Response(200, json={})
        )
        await trigger_deploy(_container(), "abc123")
        assert route.call_count == 1
        assert route.calls[0].request.headers["authorization"] == _expected_auth()

    @respx.mock
    async def test_500_surfaces_wud_message(self) -> None:
        respx.post(f"{WUD_URL}/api/containers/abc123/triggers/dockercompose/deploy").mock(
            return_value=httpx.Response(500, json={"error": "x", "message": "compose up failed: no space"})
        )
        with pytest.raises(WudError, match="compose up failed: no space") as exc:
            await trigger_deploy(_container(), "abc123")
        assert "prometheus" in str(exc.value)

    @respx.mock
    async def test_non_json_error_body_still_raises(self) -> None:
        respx.post(f"{WUD_URL}/api/containers/abc123/triggers/dockercompose/deploy").mock(
            return_value=httpx.Response(502, text="bad gateway")
        )
        with pytest.raises(WudError, match="502"):
            await trigger_deploy(_container(), "abc123")

    @respx.mock
    async def test_network_error_raises_wud_error(self) -> None:
        respx.post(f"{WUD_URL}/api/containers/abc123/triggers/dockercompose/deploy").mock(
            side_effect=httpx.ReadTimeout("slow")
        )
        with pytest.raises(WudError, match="prometheus"):
            await trigger_deploy(_container(), "abc123")
