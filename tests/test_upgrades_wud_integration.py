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


def _container(name: str = "prometheus", wud_url: str = WUD_URL) -> AdvisedContainer:
    return AdvisedContainer(
        name=name,
        host="prometheus",
        wud_url=wud_url,
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
        route = respx.post(f"{WUD_URL}/api/containers/abc123def456/triggers/dockercompose/deploy").mock(
            return_value=httpx.Response(200, json={})
        )
        await trigger_deploy(_container(), "abc123def456")
        assert route.call_count == 1
        assert route.calls[0].request.headers["authorization"] == _expected_auth()

    @respx.mock
    async def test_500_surfaces_wud_message(self) -> None:
        respx.post(f"{WUD_URL}/api/containers/abc123def456/triggers/dockercompose/deploy").mock(
            return_value=httpx.Response(500, json={"error": "x", "message": "compose up failed: no space"})
        )
        with pytest.raises(WudError, match="compose up failed: no space") as exc:
            await trigger_deploy(_container(), "abc123def456")
        assert "prometheus" in str(exc.value)

    @respx.mock
    async def test_non_json_error_body_still_raises(self) -> None:
        respx.post(f"{WUD_URL}/api/containers/abc123def456/triggers/dockercompose/deploy").mock(
            return_value=httpx.Response(502, text="bad gateway")
        )
        with pytest.raises(WudError, match="502"):
            await trigger_deploy(_container(), "abc123def456")

    @respx.mock
    async def test_network_error_raises_wud_error(self) -> None:
        respx.post(f"{WUD_URL}/api/containers/abc123def456/triggers/dockercompose/deploy").mock(
            side_effect=httpx.ReadTimeout("slow")
        )
        with pytest.raises(WudError, match="prometheus"):
            await trigger_deploy(_container(), "abc123def456")


@pytest.mark.integration
class TestRobustness:
    @respx.mock
    async def test_non_docker_id_is_refused_without_a_request(self) -> None:
        route = respx.post(url__regex=r".*/triggers/dockercompose/deploy").mock(
            return_value=httpx.Response(200, json={})
        )
        for bad in ("a/b?c", "../x", "ABC123DEF456", "abc", ""):
            with pytest.raises(WudError, match="Docker container id"):
                await trigger_deploy(_container(), bad)
        assert not route.called

    @respx.mock
    async def test_hex_id_happy_path(self) -> None:
        route = respx.post(f"{WUD_URL}/api/containers/{'a' * 64}/triggers/dockercompose/deploy").mock(
            return_value=httpx.Response(200, json={})
        )
        await trigger_deploy(_container(), "a" * 64)
        assert route.call_count == 1

    @respx.mock
    async def test_redirect_is_not_followed(self) -> None:
        first = respx.get(f"{WUD_URL}/api/containers").mock(
            return_value=httpx.Response(302, headers={"location": "http://evil.example.com/x"})
        )
        target = respx.get("http://evil.example.com/x").mock(return_value=httpx.Response(200, json=[]))
        with pytest.raises(WudError, match="302"):
            await get_container(_container())
        assert first.call_count == 1
        assert not target.called

    @respx.mock
    async def test_trigger_redirect_is_not_followed(self) -> None:
        path = "/api/containers/abc123def456/triggers/dockercompose/deploy"
        first = respx.post(f"{WUD_URL}{path}").mock(
            return_value=httpx.Response(302, headers={"location": "http://evil.example.com/x"})
        )
        target = respx.post("http://evil.example.com/x").mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(WudError, match="302"):
            await trigger_deploy(_container(), "abc123def456")
        assert first.call_count == 1
        assert not target.called

    @respx.mock
    async def test_malformed_record_message_is_bounded_and_single_line(self) -> None:
        # a list is not a valid str|None digest, so model construction fails
        record = _record("prometheus", image={"digest": {"value": ["x\ny\r\n" * 100]}})
        respx.get(f"{WUD_URL}/api/containers").mock(return_value=httpx.Response(200, json=[record]))
        with pytest.raises(WudError, match="malformed") as exc:
            await get_container(_container())
        text = str(exc.value)
        assert "\n" not in text and "\r" not in text
        assert len(text) < 200 + 120

    @respx.mock
    async def test_duplicate_names_are_ambiguous(self) -> None:
        respx.get(f"{WUD_URL}/api/containers").mock(
            return_value=httpx.Response(200, json=[_record("prometheus"), _record("prometheus")])
        )
        with pytest.raises(WudError, match="ambiguous: 2 WUD containers named prometheus"):
            await get_container(_container())

    @respx.mock
    async def test_wud_message_is_truncated_and_single_line(self) -> None:
        message = ("line one\r\nline two\n" * 40)[:500]
        respx.post(f"{WUD_URL}/api/containers/abc123def456/triggers/dockercompose/deploy").mock(
            return_value=httpx.Response(500, json={"message": message})
        )
        with pytest.raises(WudError) as exc:
            await trigger_deploy(_container(), "abc123def456")
        text = str(exc.value)
        assert "\n" not in text and "\r" not in text
        assert "line one  line two" in text
        assert len(text) < 200 + 120

    @respx.mock
    async def test_record_without_id_raises_wud_error(self) -> None:
        record = _record("prometheus")
        del record["id"]
        respx.get(f"{WUD_URL}/api/containers").mock(return_value=httpx.Response(200, json=[record]))
        with pytest.raises(WudError, match="malformed"):
            await get_container(_container())

    @respx.mock
    async def test_malformed_field_raises_wud_error(self) -> None:
        record = _record("prometheus", image={"digest": {"value": 5}})
        respx.get(f"{WUD_URL}/api/containers").mock(return_value=httpx.Response(200, json=[record]))
        with pytest.raises(WudError, match="malformed"):
            await get_container(_container())

    async def test_invalid_url_raises_wud_error(self) -> None:
        # The allowlist now rejects this URL; model_copy skips validation to test the client's own guard.
        bad = _container().model_copy(update={"wud_url": "not a url"})
        with pytest.raises(WudError):
            await get_container(bad)
        with pytest.raises(WudError):
            await trigger_deploy(bad, "abc123def456")

    @respx.mock
    async def test_trailing_slash_wud_url(self) -> None:
        route = respx.get(f"{WUD_URL}/api/containers").mock(
            return_value=httpx.Response(200, json=[_record("prometheus")])
        )
        await get_container(_container(wud_url=WUD_URL + "/"))
        assert route.call_count == 1

    @respx.mock
    async def test_password_never_in_error_or_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        respx.get(f"{WUD_URL}/api/containers").mock(side_effect=httpx.ConnectError("refused"))
        respx.post(f"{WUD_URL}/api/containers/abc123def456/triggers/dockercompose/deploy").mock(
            return_value=httpx.Response(500, json={"message": "boom"})
        )
        with caplog.at_level("DEBUG"):
            with pytest.raises(WudError) as e1:
                await get_container(_container())
            with pytest.raises(WudError) as e2:
                await trigger_deploy(_container(), "abc123def456")
        assert "s3cret" not in str(e1.value) + str(e2.value)
        assert "s3cret" not in caplog.text
        assert "failed" in caplog.text
