"""Unit tests for release version parsing and selection."""

from datetime import UTC, datetime

import pytest

from src.upgrades.releases import Release, is_major, parse_version, releases_between


def _release(tag: str) -> Release:
    version = parse_version(tag)
    assert version is not None
    return Release(tag=tag, version=version, published_at=datetime(2026, 1, 1, tzinfo=UTC), body="", url="u")


def test_parse_version_accepts_v_prefix_and_rejects_rc() -> None:
    assert parse_version("v3.15.1") == (3, 15, 1)
    assert parse_version("3.15.1") == (3, 15, 1)
    assert parse_version("v3.15.0-rc.1") is None


@pytest.mark.parametrize("tag", ["v1.2.3+build5", "v1.2.3-beta", "", "v", "latest", "1.2.3.4.5", "1..2", "v1.2.x"])
def test_parse_version_rejects_suffixes_and_junk(tag: str) -> None:
    assert parse_version(tag) is None


def test_parse_version_accepts_one_to_four_parts() -> None:
    assert parse_version("7") == (7,)
    assert parse_version("1.2.3.4") == (1, 2, 3, 4)


def test_is_major_treats_zero_minor_as_major() -> None:
    assert is_major((0, 11, 3), (0, 12, 4)) is True
    assert is_major((3, 15, 0), (3, 15, 1)) is False
    assert is_major((2, 55, 0), (3, 0, 0)) is True
    assert is_major((0, 11, 3), (0, 11, 4)) is False


def test_releases_between_is_exclusive_inclusive() -> None:
    releases = [_release(t) for t in ("v3.17.0", "v3.16.0", "v3.15.1", "v3.15.0", "v3.14.0")]
    got = releases_between(releases, (3, 15, 0), (3, 16, 0))
    assert [r.tag for r in got] == ["v3.16.0", "v3.15.1"]


def test_releases_between_treats_short_and_padded_versions_as_equal() -> None:
    releases = [_release("v3.15"), _release("v3.15.0"), _release("v3.16"), _release("v3.17.0")]
    got = releases_between(releases, (3, 15), (3, 16, 0))
    assert [r.tag for r in got] == ["v3.16"]
    got = releases_between(releases, (3, 15, 0), (3, 16))
    assert [r.tag for r in got] == ["v3.16"]
