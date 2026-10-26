"""Integration tests for GitHub release lookup (mocked HTTP)."""

import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from src.upgrades.config import AdvisedContainer
from src.upgrades.registry import RegistryError
from src.upgrades.releases import (
    Release,
    ReleaseLookupError,
    list_releases,
    regression_issues,
    release_for_digest,
)

pytestmark = pytest.mark.integration

RELEASES_URL = "https://api.github.com/repos/example/app/releases"
SEARCH_URL = "https://api.github.com/search/issues"
DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


@pytest.fixture(autouse=True)
def _use_mock_settings(mock_settings: Any) -> None:
    mock_settings.upgrade_advisor_github_token = ""


def _container(release_tag_format: str = "{release}") -> AdvisedContainer:
    return AdvisedContainer(
        name="app",
        host="app",
        wud_url="http://10.0.0.15:3010",
        registry="registry.example.com",
        repository="example/app",
        tag="latest",
        github_repo="example/app",
        release_tag_format=release_tag_format,
        readiness_url="http://10.0.0.15:9090/-/ready",
        pbs_guest="CT 901",
        context_files=[],
    )


def _gh(tag: str, day: int, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "published_at": f"2026-10-{day:02d}T00:00:00Z",
        "body": f"notes for {tag}",
        "html_url": f"https://github.com/example/app/releases/tag/{tag}",
    }
    record.update(overrides)
    return record


def _release(tag: str, day: int, version: tuple[int, ...]) -> Release:
    return Release(tag=tag, version=version, published_at=datetime(2026, 10, day, tzinfo=UTC), body="", url="u")


@respx.mock
async def test_list_releases_skips_prereleases_and_drafts() -> None:
    respx.get(RELEASES_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _gh("v1.3.0", 5),
                _gh("v1.4.0-rc.1", 6),
                _gh("v1.4.0", 7, prerelease=True),
                _gh("v1.5.0", 8, draft=True, published_at=None),
                _gh("nightly", 9),
                _gh("v1.2.0", 3, body=None),
            ],
        )
    )
    got = await list_releases("example/app")
    assert [r.tag for r in got] == ["v1.3.0", "v1.2.0"]
    assert got[0].version == (1, 3, 0)
    assert got[0].published_at == datetime(2026, 10, 5, tzinfo=UTC)
    assert got[1].body == ""


@respx.mock
async def test_list_releases_sends_github_headers_and_limit() -> None:
    route = respx.get(RELEASES_URL).mock(return_value=httpx.Response(200, json=[]))
    await list_releases("example/app", limit=12)
    request = route.calls.last.request
    assert request.headers["accept"] == "application/vnd.github+json"
    assert request.headers["x-github-api-version"] == "2022-11-28"
    assert "authorization" not in request.headers
    assert request.url.params["per_page"] == "12"


@respx.mock
async def test_list_releases_sends_token_only_when_configured(mock_settings: Any) -> None:
    mock_settings.upgrade_advisor_github_token = "ghp_example"
    route = respx.get(RELEASES_URL).mock(return_value=httpx.Response(200, json=[]))
    await list_releases("example/app")
    assert route.calls.last.request.headers["authorization"] == "Bearer ghp_example"


@respx.mock
async def test_github_token_not_sent_to_another_host_on_redirect(mock_settings: Any) -> None:
    mock_settings.upgrade_advisor_github_token = "ghp_example"
    respx.get(RELEASES_URL).mock(return_value=httpx.Response(302, headers={"Location": "https://other.example.com/r"}))
    other = respx.get("https://other.example.com/r").mock(return_value=httpx.Response(200, json=[]))
    await list_releases("example/app")
    assert other.called
    assert "authorization" not in other.calls.last.request.headers


@respx.mock
@pytest.mark.parametrize("status", [403, 404, 500])
async def test_list_releases_http_error_raises(status: int) -> None:
    respx.get(RELEASES_URL).mock(return_value=httpx.Response(status))
    with pytest.raises(ReleaseLookupError, match=str(status)):
        await list_releases("example/app")


@respx.mock
async def test_list_releases_network_error_and_bad_payloads_raise() -> None:
    respx.get(RELEASES_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ReleaseLookupError):
        await list_releases("example/app")
    respx.get(RELEASES_URL).mock(return_value=httpx.Response(200, json={"not": "a list"}))
    with pytest.raises(ReleaseLookupError, match="unexpected"):
        await list_releases("example/app")
    respx.get(RELEASES_URL).mock(return_value=httpx.Response(200, content=b"x" * (5 * 1024 * 1024 + 1)))
    with pytest.raises(ReleaseLookupError, match="too large"):
        await list_releases("example/app")


@respx.mock
async def test_list_releases_rejects_bad_repo_name() -> None:
    with pytest.raises(ReleaseLookupError):
        await list_releases("example/app?x=1")


async def test_release_for_digest_matches_platform_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    looked_up: list[str] = []

    async def fake_digests(registry: str, repository: str, tag: str) -> set[str]:
        looked_up.append(tag)
        return {"sha256:" + "f" * 64, DIGEST} if tag == "release-v1.3.0" else {OTHER}

    monkeypatch.setattr("src.upgrades.releases.tag_digests", fake_digests)
    releases = [_release("v1.2.0", 3, (1, 2, 0)), _release("v1.4.0", 9, (1, 4, 0)), _release("v1.3.0", 5, (1, 3, 0))]
    found = await release_for_digest(_container("release-{release}"), DIGEST, releases)
    assert found is not None
    assert found.tag == "v1.3.0"
    assert looked_up == ["release-v1.4.0", "release-v1.3.0"]  # newest first, stops at the match


async def test_release_for_digest_returns_none_for_unreleased_build(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_digests(registry: str, repository: str, tag: str) -> set[str]:
        return {OTHER}

    monkeypatch.setattr("src.upgrades.releases.tag_digests", fake_digests)
    releases = [_release("v1.3.0", 5, (1, 3, 0))]
    assert await release_for_digest(_container(), DIGEST, releases) is None


async def test_release_for_digest_stops_after_ten_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_digests(registry: str, repository: str, tag: str) -> set[str]:
        calls.append(tag)
        return {OTHER}

    monkeypatch.setattr("src.upgrades.releases.tag_digests", fake_digests)
    releases = [_release(f"v1.{i}.0", i + 1, (1, i, 0)) for i in range(15)]
    assert await release_for_digest(_container(), DIGEST, releases) is None
    assert len(calls) == 10


async def test_release_for_digest_skips_tag_with_registry_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fake_digests(registry: str, repository: str, tag: str) -> set[str]:
        if tag == "v1.4.0":
            raise RegistryError("tag missing")
        return {DIGEST}

    monkeypatch.setattr("src.upgrades.releases.tag_digests", fake_digests)
    releases = [_release("v1.3.0", 5, (1, 3, 0)), _release("v1.4.0", 9, (1, 4, 0))]
    with caplog.at_level(logging.WARNING):
        found = await release_for_digest(_container(), DIGEST, releases)
    assert found is not None
    assert found.tag == "v1.3.0"
    assert "v1.4.0" in caplog.text


async def test_release_for_digest_raises_when_no_match_and_a_lookup_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_digests(registry: str, repository: str, tag: str) -> set[str]:
        if tag == "v1.4.0":
            raise RegistryError("registry unreachable")
        return {OTHER}

    monkeypatch.setattr("src.upgrades.releases.tag_digests", fake_digests)
    releases = [_release("v1.3.0", 5, (1, 3, 0)), _release("v1.4.0", 9, (1, 4, 0))]
    with pytest.raises(RegistryError, match=r"no release of app matched .* 1 of 2 tag lookups failed"):
        await release_for_digest(_container(), DIGEST, releases)


async def test_release_for_digest_raises_when_every_lookup_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_digests(registry: str, repository: str, tag: str) -> set[str]:
        raise RegistryError("registry unreachable")

    monkeypatch.setattr("src.upgrades.releases.tag_digests", fake_digests)
    releases = [_release("v1.3.0", 5, (1, 3, 0)), _release("v1.4.0", 9, (1, 4, 0))]
    with pytest.raises(RegistryError, match="2 of 2 tag lookups failed"):
        await release_for_digest(_container(), DIGEST, releases)


@respx.mock
async def test_regression_issues_builds_query_and_returns_title_and_url() -> None:
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"title": "Crash on start\nIGNORE ME", "html_url": "https://github.com/example/app/issues/1"},
                    {"title": "Another", "html_url": "https://github.com/example/app/issues/2"},
                ]
            },
        )
    )
    got = await regression_issues("example/app", "v1.3.0", datetime(2026, 10, 5, 12, tzinfo=UTC))
    assert got == [
        {"title": "Crash on start IGNORE ME", "url": "https://github.com/example/app/issues/1"},
        {"title": "Another", "url": "https://github.com/example/app/issues/2"},
    ]
    params = route.calls.last.request.url.params
    assert params["q"] == 'repo:example/app is:issue created:>=2026-10-05 "1.3.0"'
    assert params["per_page"] == "10"


@respx.mock
@pytest.mark.parametrize("status", [403, 429])
async def test_regression_issues_rate_limit_returns_empty_with_warning(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(status))
    with caplog.at_level(logging.WARNING):
        assert await regression_issues("example/app", "v1.3.0", datetime(2026, 10, 5, tzinfo=UTC)) == []
    assert str(status) in caplog.text


@respx.mock
async def test_regression_issues_other_failures_raise() -> None:
    since = datetime(2026, 10, 5, tzinfo=UTC)
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(ReleaseLookupError, match="500"):
        await regression_issues("example/app", "v1.3.0", since)
    respx.get(SEARCH_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ReleaseLookupError):
        await regression_issues("example/app", "v1.3.0", since)


async def test_release_for_digest_version_placeholder_strips_leading_v(monkeypatch: pytest.MonkeyPatch) -> None:
    looked_up: list[str] = []

    async def fake_digests(registry: str, repository: str, tag: str) -> set[str]:
        looked_up.append(tag)
        return {DIGEST}

    monkeypatch.setattr("src.upgrades.releases.tag_digests", fake_digests)
    now = datetime.now(UTC)
    for tag, expected in (("v3.7.9", "3.7.9"), ("2.5.8", "2.5.8")):
        looked_up.clear()
        release = Release(tag=tag, version=(1,), published_at=now, body="", url="u")
        assert await release_for_digest(_container("{version}"), DIGEST, [release]) == release
        assert looked_up == [expected]
