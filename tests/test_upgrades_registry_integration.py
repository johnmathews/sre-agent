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
    challenge = (
        f'Bearer realm="https://registry.example.com/token",service="{REGISTRY}",scope="repository:team/app:pull"'
    )
    route = respx.get(MANIFEST_URL).mock(
        side_effect=[
            httpx.Response(401, headers={"WWW-Authenticate": challenge}),
            httpx.Response(200, json=INDEX_BODY, headers={"Docker-Content-Digest": INDEX}),
        ]
    )
    token = respx.get("https://registry.example.com/token").mock(
        return_value=httpx.Response(200, json={"token": "tok"})
    )

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
    challenge = 'Bearer realm="http://registry.example.com/token",service="x",scope="y"'
    respx.get(MANIFEST_URL).mock(return_value=httpx.Response(401, headers={"WWW-Authenticate": challenge}))
    token = respx.get("http://registry.example.com/token").mock(return_value=httpx.Response(200, json={"token": "t"}))
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
    challenge = 'Bearer realm="https://registry.example.com/token",service="s",scope="c"'
    route = respx.get(MANIFEST_URL).mock(return_value=httpx.Response(401, headers={"WWW-Authenticate": challenge}))
    respx.get("https://registry.example.com/token").mock(return_value=httpx.Response(200, json={"token": "tok"}))
    with pytest.raises(RegistryError, match="401"):
        await tag_digests(REGISTRY, "team/app", "v1")
    assert route.call_count == 2


@respx.mock
async def test_tag_digests_token_response_without_token_raises() -> None:
    challenge = 'Bearer realm="https://registry.example.com/token",service="s",scope="c"'
    respx.get(MANIFEST_URL).mock(return_value=httpx.Response(401, headers={"WWW-Authenticate": challenge}))
    respx.get("https://registry.example.com/token").mock(return_value=httpx.Response(200, json={"nope": 1}))
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


def _challenge_401(realm_params: str) -> httpx.Response:
    return httpx.Response(401, headers={"WWW-Authenticate": f"Bearer {realm_params}"})


@respx.mock
@pytest.mark.parametrize(
    "realm",
    [
        "https://evil.example.com/token",
        "https://auth.docker.io:8443/token",
        "https://user:pw@auth.docker.io/token",
        "https://user@registry.example.com/token",
        "https://registry.example.com.evil.example.com/token",
    ],
)
async def test_tag_digests_refuses_realm_outside_allowed_hosts(realm: str) -> None:
    respx.get(MANIFEST_URL).mock(return_value=_challenge_401(f'realm="{realm}",service="s"'))
    token = respx.get(url__regex=r"https://(evil|auth|user|registry\.example\.com\.evil).*").mock(
        return_value=httpx.Response(200, json={"token": "t"})
    )
    with pytest.raises(RegistryError) as info:
        await tag_digests(REGISTRY, "team/app", "v1")
    assert "evil" not in str(info.value)
    assert not token.called


@respx.mock
async def test_tag_digests_uses_the_first_realm_only() -> None:
    challenge = 'realm="https://registry.example.com/token",service="s",realm="https://evil.example.com/token"'
    route = respx.get(MANIFEST_URL).mock(
        side_effect=[_challenge_401(challenge), httpx.Response(200, json={}, headers={"Docker-Content-Digest": INDEX})]
    )
    token = respx.get("https://registry.example.com/token").mock(return_value=httpx.Response(200, json={"token": "t"}))
    evil = respx.get("https://evil.example.com/token").mock(return_value=httpx.Response(200, json={"token": "e"}))
    assert await tag_digests(REGISTRY, "team/app", "v1") == {INDEX}
    assert token.called
    assert not evil.called
    assert route.calls[1].request.headers["authorization"] == "Bearer t"


@respx.mock
async def test_tag_digests_token_endpoint_redirect_is_an_error_and_not_followed() -> None:
    respx.get(MANIFEST_URL).mock(return_value=_challenge_401('realm="https://registry.example.com/token",service="s"'))
    respx.get("https://registry.example.com/token").mock(
        return_value=httpx.Response(302, headers={"Location": "https://registry.example.com/elsewhere"})
    )
    target = respx.get("https://registry.example.com/elsewhere").mock(
        return_value=httpx.Response(200, json={"token": "t"})
    )
    with pytest.raises(RegistryError, match="redirect"):
        await tag_digests(REGISTRY, "team/app", "v1")
    assert not target.called


@respx.mock
async def test_tag_digests_docker_hub_and_ghcr_token_hosts_are_accepted() -> None:
    for registry, realm in (
        ("registry-1.docker.io", "https://auth.docker.io/token"),
        ("ghcr.io", "https://ghcr.io/token"),
    ):
        url = f"https://{registry}/v2/team/app/manifests/v1"
        respx.get(url).mock(
            side_effect=[
                _challenge_401(f'realm="{realm}",service="s",scope="repository:team/app:pull"'),
                httpx.Response(200, json={}, headers={"Docker-Content-Digest": INDEX}),
            ]
        )
        respx.get(realm).mock(return_value=httpx.Response(200, json={"token": "t"}))
        assert await tag_digests(registry, "team/app", "v1") == {INDEX}


@respx.mock
async def test_tag_digests_authenticated_retry_redirect_does_not_leak_authorization() -> None:
    respx.get(MANIFEST_URL).mock(
        side_effect=[
            _challenge_401('realm="https://registry.example.com/token",service="s"'),
            httpx.Response(307, headers={"Location": "https://cdn.example.net/blob"}),
        ]
    )
    respx.get("https://registry.example.com/token").mock(return_value=httpx.Response(200, json={"token": "secret"}))
    cdn = respx.get("https://cdn.example.net/blob").mock(
        return_value=httpx.Response(200, json={}, headers={"Docker-Content-Digest": INDEX})
    )
    assert await tag_digests(REGISTRY, "team/app", "v1") == {INDEX}
    assert cdn.called
    assert "authorization" not in cdn.calls.last.request.headers
