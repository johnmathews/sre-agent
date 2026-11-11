"""Name the caller of ``/mcp`` from its verified Cloudflare Access identity, without exposing that identity.

``client_label`` is what goes into logs and metric labels: the name ``MCP_CLIENT_NAMES`` gives the identity, or a
generic ``unmapped-*`` label. It never returns the identity itself, which for a service token is its Client ID.
``identity_ref`` is a short hash that lets log lines from one caller be matched up without naming it.
"""

import hashlib

from src.config import get_settings, parse_client_names
from src.upgrades.access import AccessIdentity

UNMAPPED_SERVICE = "unmapped-service-token"
UNMAPPED_USER = "unmapped-user"


def mcp_audiences() -> list[str]:
    """The AUD tags in ``MCP_CF_ACCESS_AUD`` (comma-separated, trimmed, empties dropped). Empty means not set."""
    return [aud.strip() for aud in get_settings().mcp_cf_access_aud.split(",") if aud.strip()]


def client_label(identity: AccessIdentity) -> str:
    """The client name for ``identity`` from ``MCP_CLIENT_NAMES`` (case-insensitive), else a generic label."""
    names = parse_client_names(get_settings().mcp_client_names)
    name = names.get(identity.value.strip().lower())
    if name is not None:
        return name
    return UNMAPPED_SERVICE if identity.kind == "service" else UNMAPPED_USER


def identity_ref(identity: AccessIdentity) -> str:
    """sha256[:8] of the lowercased identity: for log lines only, never a metric label."""
    return hashlib.sha256(identity.value.strip().lower().encode()).hexdigest()[:8]
