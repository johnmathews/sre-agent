"""Integration tests for registry digest lookup (mocked HTTP)."""

import httpx
import pytest
import respx

from src.upgrades.registry import RegistryError, tag_digests

pytestmark = pytest.mark.integration

REGISTRY = "registry.example.com"
INDEX = "sha256:" + "a" * 64
AMD64 = "sha256:" + "b" * 64
ARM64 = "sha256:" + "c" * 64
MANIFEST_URL = f"https://{REGISTRY}/v2/team/app/manifests/v1"
INDEX_BODY = {
    "schemaVersion": 2,
    "manifests": [
        {"digest": AMD64, "platform": {"architecture": "amd64", "os": "linux"}},
        {"digest": ARM64, "platform": {"architecture": "arm64", "os": "linux"}},
    ],
}


@respx.mock
async def test_tag_digests_follows_bearer_challenge_and_returns_platform_digests() -> None:
    challenge = f'Bearer realm="https://auth.example.com/token",service="{REGISTRY}",scope="repository:team/app:pull"'
    route = respx.get(MANIFEST_URL).mock(
        side_effect=[
            httpx.Response(401, headers={"WWW-Authenticate": challenge}),
            httpx.Response(200, json=INDEX_BODY, headers={"Docker-Content-Digest": INDEX}),
        ]
    )
    token = respx.get("https://auth.example.com/token").mock(return_value=httpx.Response(200, json={"token": "tok"}))

    digests = await tag_digests(REGISTRY, "team/app", "v1")

    assert digests == {INDEX, AMD64, ARM64}
    assert token.calls.last.request.url.params["service"] == REGISTRY
    assert token.calls.last.request.url.params["scope"] == "repository:team/app:pull"
    assert "authorization" not in token.calls.last.request.headers
    first, second = route.calls[0].request, route.calls[1].request
    assert "authorization" not in first.headers
    assert second.headers["authorization"] == "Bearer tok"
    accept = first.headers["accept"]
    for media in (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ):
        assert media in accept


@respx.mock
async def test_tag_digests_single_manifest_returns_only_its_digest() -> None:
    respx.get(MANIFEST_URL).mock(
        return_value=httpx.Response(
            200, json={"schemaVersion": 2, "config": {}, "layers": []}, headers={"Docker-Content-Digest": AMD64}
        )
    )
    assert await tag_digests(REGISTRY, "team/app", "v1") == {AMD64}


@respx.mock
async def test_tag_digests_rejects_non_https_realm() -> None:
    challenge = 'Bearer realm="http://auth.example.com/token",service="x",scope="y"'
    respx.get(MANIFEST_URL).mock(return_value=httpx.Response(401, headers={"WWW-Authenticate": challenge}))
    token = respx.get("http://auth.example.com/token").mock(return_value=httpx.Response(200, json={"token": "t"}))
    with pytest.raises(RegistryError, match="https"):
        await tag_digests(REGISTRY, "team/app", "v1")
    assert not token.called


@respx.mock
async def test_tag_digests_401_without_bearer_challenge_raises() -> None:
    respx.get(MANIFEST_URL).mock(return_value=httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="x"'}))
    with pytest.raises(RegistryError, match="challenge"):
        await tag_digests(REGISTRY, "team/app", "v1")


@respx.mock
async def test_tag_digests_second_401_after_token_raises() -> None:
    challenge = 'Bearer realm="https://auth.example.com/token",service="s",scope="c"'
    route = respx.get(MANIFEST_URL).mock(return_value=httpx.Response(401, headers={"WWW-Authenticate": challenge}))
    respx.get("https://auth.example.com/token").mock(return_value=httpx.Response(200, json={"token": "tok"}))
    with pytest.raises(RegistryError, match="401"):
        await tag_digests(REGISTRY, "team/app", "v1")
    assert route.call_count == 2


@respx.mock
async def test_tag_digests_token_response_without_token_raises() -> None:
    challenge = 'Bearer realm="https://auth.example.com/token",service="s",scope="c"'
    respx.get(MANIFEST_URL).mock(return_value=httpx.Response(401, headers={"WWW-Authenticate": challenge}))
    respx.get("https://auth.example.com/token").mock(return_value=httpx.Response(200, json={"nope": 1}))
    with pytest.raises(RegistryError, match="token"):
        await tag_digests(REGISTRY, "team/app", "v1")


@respx.mock
async def test_tag_digests_404_raises_with_status() -> None:
    respx.get(MANIFEST_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(RegistryError, match="404"):
        await tag_digests(REGISTRY, "team/app", "v1")


@respx.mock
async def test_tag_digests_network_error_raises_registry_error() -> None:
    respx.get(MANIFEST_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(RegistryError, match="request failed"):
        await tag_digests(REGISTRY, "team/app", "v1")


@respx.mock
async def test_tag_digests_oversized_response_raises() -> None:
    respx.get(MANIFEST_URL).mock(return_value=httpx.Response(200, content=b"x" * (5 * 1024 * 1024 + 1)))
    with pytest.raises(RegistryError, match="too large"):
        await tag_digests(REGISTRY, "team/app", "v1")


@respx.mock
async def test_tag_digests_malformed_digest_in_index_raises_without_echoing_value() -> None:
    body = {"manifests": [{"digest": "sha256:evil\nINJECT"}]}
    respx.get(MANIFEST_URL).mock(return_value=httpx.Response(200, json=body, headers={"Docker-Content-Digest": INDEX}))
    with pytest.raises(RegistryError) as info:
        await tag_digests(REGISTRY, "team/app", "v1")
    assert "INJECT" not in str(info.value)


@respx.mock
async def test_tag_digests_refuses_redirect_to_plain_http() -> None:
    respx.get(MANIFEST_URL).mock(return_value=httpx.Response(302, headers={"Location": "http://cdn.example.com/m"}))
    respx.get("http://cdn.example.com/m").mock(return_value=httpx.Response(200, json=INDEX_BODY))
    with pytest.raises(RegistryError, match="https"):
        await tag_digests(REGISTRY, "team/app", "v1")
