"""Container registry client: the digests a tag currently points at (index plus platform manifests)."""

import logging
import re
from urllib.parse import urlsplit

import httpx

from src.upgrades.http import BoundedResponse, ResponseTooLarge, bounded_request
from src.upgrades.textutil import clean_text

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30.0
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_BEARER_RE = re.compile(r"(?:^|,)\s*bearer\s+", re.IGNORECASE)
_NEXT_CHALLENGE_RE = re.compile(r',\s*[A-Za-z][A-Za-z0-9_-]*\s+[A-Za-z_]+="')
_REALM_RE = re.compile(r'\brealm="([^"]*)"')
# Token hosts accepted besides the registry's own host (Docker Hub's and ghcr's token services).
KNOWN_TOKEN_HOSTS = frozenset({"auth.docker.io", "ghcr.io"})
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


class RegistryError(Exception):
    """A registry was unreachable, refused the request, or answered with something unusable."""


def _fail(message: str) -> RegistryError:
    """Log ``message`` and build the RegistryError (the caller raises it, chained)."""
    logger.error(message)
    return RegistryError(message)


def _where(registry: str, repository: str, tag: str) -> str:
    return f"{registry}/{repository}:{tag}"


def _exc_text(exc: Exception) -> str:
    """Exception class plus cleaned message, for log lines (never the raw repr)."""
    return f"{type(exc).__name__}: {clean_text(str(exc))}"


def _check_response(response: BoundedResponse, where: str) -> None:
    if str(response.url).partition(":")[0] != "https":
        raise _fail(f"registry request for {where} ended on a non-https URL; refusing")


def _param(rest: str, name: str) -> str | None:
    """First ``name="value"`` in the challenge parameters."""
    match = re.search(rf'\b{name}="([^"]*)"', rest)
    return match.group(1) if match else None


def _check_realm(realm: str, registry: str, where: str) -> None:
    """Accept only an https token endpoint on the registry's own host or a known token host (never echoes the realm)."""
    try:
        parts = urlsplit(realm)
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise _fail(f"registry auth realm for {where} is not a valid URL") from None
    if parts.scheme != "https":
        raise _fail(f"registry auth realm for {where} is missing or not https")
    registry_host = urlsplit(f"https://{registry}").hostname
    if parts.username is not None or parts.password is not None or port not in (None, 443):
        raise _fail(f"registry auth realm for {where} has credentials or a non-default port; refusing")
    if host is None or (host != registry_host and host not in KNOWN_TOKEN_HOSTS):
        raise _fail(f"registry auth realm for {where} is not on {registry} or a known token host; refusing")


async def _fetch_token(client: httpx.AsyncClient, registry: str, challenge: str, where: str) -> str:
    """Anonymous bearer token for a ``WWW-Authenticate: Bearer ...`` challenge (first Bearer challenge only)."""
    start = _BEARER_RE.search(challenge)
    if start is None:
        raise _fail(f"registry auth challenge for {where} is not a Bearer challenge")
    rest = challenge[start.end() :]
    following = _NEXT_CHALLENGE_RE.search(rest)
    if following is not None:
        rest = rest[: following.start()]
    realm = _param(rest, "realm") or ""
    _check_realm(realm, registry, where)
    query = {k: v for k in ("service", "scope") if (v := _param(rest, k)) is not None}
    try:
        response = await bounded_request(client, "GET", realm, params=query, follow_redirects=False)
    except ResponseTooLarge:
        raise _fail(f"registry response too large for {where}") from None
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # `from None`: the realm comes from the registry and may be hostile; log only the exception class.
        raise _fail(f"registry token request failed for {where}: {type(exc).__name__}") from None
    if response.status_code // 100 == 3:
        raise _fail(f"registry token endpoint redirected for {where}; refusing")
    _check_response(response, where)
    if not response.is_success:
        raise _fail(f"registry token endpoint returned HTTP {response.status_code} for {where}")
    try:
        body = response.json()
    except ValueError as exc:
        raise _fail(f"registry token endpoint returned invalid JSON for {where}") from exc
    token = body.get("token") or body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise _fail(f"registry token response for {where} has no token")
    return token


def _digest_or_fail(value: object, where: str, what: str) -> str:
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        raise _fail(f"registry returned a malformed {what} digest for {where}")
    return value


async def tag_digests(registry: str, repository: str, tag: str) -> set[str]:
    """Digests ``registry/repository:tag`` resolves to: the index digest plus every platform manifest digest.

    A single (non-index) manifest yields just its own digest.
    """
    where = _where(registry, repository, tag)
    url = f"https://{registry}/v2/{repository}/manifests/{tag}"
    headers = {"Accept": MANIFEST_ACCEPT}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await bounded_request(client, "GET", url, headers=headers)
            if response.status_code == httpx.codes.UNAUTHORIZED:
                challenge = response.headers.get("WWW-Authenticate", "")
                if not challenge.lower().startswith("bearer "):
                    raise _fail(f"registry returned 401 without a Bearer challenge for {where}")
                token = await _fetch_token(client, registry, challenge, where)
                response = await bounded_request(
                    client, "GET", url, headers={**headers, "Authorization": f"Bearer {token}"}
                )
    except ResponseTooLarge as exc:
        raise _fail(f"registry response too large for {where}") from exc
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise _fail(f"registry request failed for {where}: {_exc_text(exc)}") from exc
    _check_response(response, where)
    if not response.is_success:
        raise _fail(f"registry returned HTTP {response.status_code} for {where}")
    header_digest = response.headers.get("Docker-Content-Digest")
    if header_digest is None:
        raise _fail(f"registry sent no Docker-Content-Digest for {where}")
    digests = {_digest_or_fail(header_digest, where, "index")}
    try:
        body = response.json()
    except ValueError as exc:
        raise _fail(f"registry returned invalid JSON for {where}") from exc
    manifests = body.get("manifests") if isinstance(body, dict) else None
    if manifests is None:
        return digests  # a single manifest, not an index
    if not isinstance(manifests, list):
        raise _fail(
            f"registry index for {where} has an unexpected 'manifests' value: {clean_text(type(manifests).__name__)}"
        )
    for entry in manifests:
        digests.add(_digest_or_fail(entry.get("digest") if isinstance(entry, dict) else None, where, "platform"))
    return digests
