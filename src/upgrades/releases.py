"""GitHub release lookup: which release a digest belongs to, and what changed between two versions."""

import logging
import re
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from src.config import get_settings
from src.upgrades.config import AdvisedContainer, GithubRepo
from src.upgrades.http import BoundedResponse, ResponseTooLarge, bounded_request
from src.upgrades.registry import RegistryError, tag_digests
from src.upgrades.textutil import clean_text

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
TIMEOUT_SECONDS = 30.0
MAX_DIGEST_LOOKUPS = 10
MAX_ISSUES = 10
_VERSION_RE = re.compile(r"v?([0-9]{1,9}(?:\.[0-9]{1,9}){0,3})")
_REPO_ADAPTER: TypeAdapter[str] = TypeAdapter(GithubRepo)


class ReleaseLookupError(Exception):
    """GitHub was unreachable, refused the request, or answered with something unusable."""


class Release(BaseModel):
    """A published, stable, parseable GitHub release."""

    tag: str
    version: tuple[int, ...]
    published_at: datetime
    body: str
    url: str


def _fail(message: str) -> ReleaseLookupError:
    """Log ``message`` and build the ReleaseLookupError (the caller raises it, chained)."""
    logger.error(message)
    return ReleaseLookupError(message)


def parse_version(tag: str) -> tuple[int, ...] | None:
    """``v3.15.1`` / ``3.15.1`` -> ``(3, 15, 1)``; 1-4 numeric parts only, any suffix -> None."""
    match = _VERSION_RE.fullmatch(tag)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_major(running: tuple[int, ...], candidate: tuple[int, ...]) -> bool:
    """A major change: the major differs, or the major is 0 and the minor differs."""
    run, cand = (running + (0, 0))[:2], (candidate + (0, 0))[:2]
    return run[0] != cand[0] or (run[0] == 0 and run[1] != cand[1])


def _padded(version: tuple[int, ...], width: int) -> tuple[int, ...]:
    """Zero-pad a version tuple so (3, 15) compares equal to (3, 15, 0)."""
    return version + (0,) * (width - len(version))


def releases_between(releases: list[Release], running: tuple[int, ...], candidate: tuple[int, ...]) -> list[Release]:
    """Releases newer than ``running`` up to and including ``candidate`` (input order kept)."""
    width = max(len(running), len(candidate), *(len(r.version) for r in releases), 0)
    low, high = _padded(running, width), _padded(candidate, width)
    return [r for r in releases if low < _padded(r.version, width) <= high]


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = get_settings().upgrade_advisor_github_token
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _check_repo(github_repo: str) -> None:
    try:
        _REPO_ADAPTER.validate_python(github_repo)
    except ValidationError:
        raise _fail("refusing GitHub lookup: repository name is not owner/name") from None


async def _github_get(path: str, params: dict[str, str | int], what: str) -> BoundedResponse:
    """GET ``path`` on the GitHub API; network errors and oversized bodies raise, status is the caller's call."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await bounded_request(client, "GET", f"{GITHUB_API}{path}", params=params, headers=_headers())
    except ResponseTooLarge as exc:
        raise _fail(f"GitHub response too large for {what}") from exc
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise _fail(f"GitHub request failed for {what}: {type(exc).__name__}: {clean_text(str(exc))}") from exc
    return response


def _json(response: BoundedResponse, what: str) -> Any:
    if not response.is_success:
        raise _fail(f"GitHub returned HTTP {response.status_code} for {what}")
    try:
        return response.json()
    except ValueError as exc:
        raise _fail(f"GitHub returned invalid JSON for {what}") from exc


def _parse_release(record: Any) -> Release | None:
    """A Release from one GitHub record, or None for drafts, prereleases and unparseable tags."""
    if not isinstance(record, dict) or record.get("draft") or record.get("prerelease"):
        return None
    tag = record.get("tag_name")
    version = parse_version(tag) if isinstance(tag, str) else None
    if version is None:
        return None
    try:
        return Release(
            tag=str(tag),
            version=version,
            published_at=record["published_at"],
            body=record.get("body") or "",
            url=record["html_url"],
        )
    except (KeyError, ValidationError):
        logger.warning("skipping GitHub release %s: malformed record", clean_text(str(tag)))
        return None


async def list_releases(github_repo: str, limit: int = 30) -> list[Release]:
    """Stable, published releases of ``github_repo`` as GitHub lists them (newest first)."""
    _check_repo(github_repo)
    what = f"releases of {github_repo}"
    records = _json(await _github_get(f"/repos/{github_repo}/releases", {"per_page": limit}, what), what)
    if not isinstance(records, list):
        raise _fail(f"GitHub returned an unexpected payload for {what}")
    return [r for r in map(_parse_release, records) if r is not None]


async def release_for_digest(c: AdvisedContainer, digest: str, releases: list[Release]) -> Release | None:
    """The release whose image tag currently resolves to ``digest`` (newest first, at most 10 registry lookups)."""
    newest_first = sorted(releases, key=lambda r: r.published_at, reverse=True)
    for release in newest_first[:MAX_DIGEST_LOOKUPS]:
        image_tag = c.release_tag_format.format(release=release.tag)
        try:
            digests = await tag_digests(c.registry, c.repository, image_tag)
        except RegistryError as exc:
            logger.warning("skipping %s for %s: %s", image_tag, c.name, clean_text(str(exc)))
            continue
        if digest in digests:
            return release
    return None


async def regression_issues(github_repo: str, release_tag: str, since: datetime) -> list[dict[str, str]]:
    """Issues mentioning the release's version, opened since ``since`` (best effort; ``[]`` when rate limited)."""
    _check_repo(github_repo)
    version = release_tag.removeprefix("v").replace('"', "")
    query = f'repo:{github_repo} is:issue created:>={since.date().isoformat()} "{version}"'
    what = f"issues of {github_repo}"
    response = await _github_get("/search/issues", {"q": query, "per_page": MAX_ISSUES}, what)
    if response.status_code in {403, 429}:
        logger.warning(
            "GitHub issue search for %s returned HTTP %s (rate limited?); skipping", github_repo, response.status_code
        )
        return []
    body = _json(response, what)
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise _fail(f"GitHub returned an unexpected payload for {what}")
    return [
        {"title": clean_text(str(item["title"])), "url": clean_text(str(item["html_url"]))}
        for item in items[:MAX_ISSUES]
        if isinstance(item, dict) and "title" in item and "html_url" in item
    ]
