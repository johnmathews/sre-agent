"""Container registry client: the digests a tag currently points at (index plus platform manifests)."""

import logging
import re

import httpx

from src.upgrades.textutil import MAX_RESPONSE_BYTES, clean_text

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30.0
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_CHALLENGE_PARAM_RE = re.compile(r'([A-Za-z_]+)="([^"]*)"')
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


def _check_response(response: httpx.Response, where: str) -> None:
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise _fail(f"registry response too large for {where}")
    if str(response.url).partition(":")[0] != "https":
        raise _fail(f"registry request for {where} ended on a non-https URL; refusing")


async def _fetch_token(client: httpx.AsyncClient, challenge: str, where: str) -> str:
    """Anonymous bearer token for a ``WWW-Authenticate: Bearer ...`` challenge."""
    scheme, _, rest = challenge.partition(" ")
    if scheme.lower() != "bearer":
        raise _fail(f"registry auth challenge for {where} is not a Bearer challenge")
    params = dict(_CHALLENGE_PARAM_RE.findall(rest))
    realm = params.get("realm", "")
    if not realm.startswith("https://"):
        raise _fail(f"registry auth realm for {where} is missing or not https")
    query = {k: params[k] for k in ("service", "scope") if k in params}
    try:
        response = await client.get(realm, params=query)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # `from None`: the realm comes from the registry and may be hostile; log only the exception class.
        raise _fail(f"registry token request failed for {where}: {type(exc).__name__}") from None
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
            response = await client.get(url, headers=headers)
            if response.status_code == httpx.codes.UNAUTHORIZED:
                challenge = response.headers.get("WWW-Authenticate", "")
                if not challenge.lower().startswith("bearer "):
                    raise _fail(f"registry returned 401 without a Bearer challenge for {where}")
                token = await _fetch_token(client, challenge, where)
                response = await client.get(url, headers={**headers, "Authorization": f"Bearer {token}"})
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise _fail(f"registry request failed for {where}: {exc!r}") from exc
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
