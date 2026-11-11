"""Tests for the /mcp client label and identity ref, and the MCP access settings (no live calls)."""

import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from pydantic import ValidationError

from src.api.mcp_identity import UNMAPPED_SERVICE, UNMAPPED_USER, client_label, identity_ref, mcp_audiences
from src.config import Settings, parse_client_names
from src.upgrades import access
from src.upgrades.access import AccessIdentity, verify_access_identity
from tests.upgrades_access_helpers import AUD, CERTS_URL, EMAIL, SERVICE_CLIENT_ID, TEAM, Signer, jwks

K1 = Signer("k1")
OTHER_CLIENT_ID = "aaaabbbbccccddddeeeeffff00001111.access"


@pytest.fixture(autouse=True)
def _env(mock_settings: Any) -> Iterator[None]:
    mock_settings.cf_access_team_domain = TEAM
    mock_settings.mcp_cf_access_aud = AUD
    access._reset_state()
    yield
    access._reset_state()


def _settings(**overrides: Any) -> Settings:
    """A real Settings load (env file blocked by the autouse _no_dotenv fixture)."""
    base: dict[str, Any] = {
        "prometheus_url": "http://prometheus.test:9090",
        "grafana_url": "http://grafana.test:3000",
        "grafana_service_account_token": "glsa_test_fake",
        "llm_provider": "openai",
        "openai_api_key": "sk-test-fake",
    }
    base.update(overrides)
    return Settings(**base)


# --- client_label ---


@pytest.mark.integration
@respx.mock
async def test_client_label_never_returns_raw_identity(mock_settings: Any) -> None:
    """A verified but unmapped service token gets the generic label, never its Client ID."""
    respx.get(CERTS_URL).mock(return_value=httpx.Response(200, json=jwks(K1)))
    mock_settings.mcp_client_names = f"{OTHER_CLIENT_ID}=claude-code"
    identity = await verify_access_identity(K1.service_token(), mcp_audiences())
    assert identity.kind == "service"

    label = client_label(identity)

    assert label == UNMAPPED_SERVICE
    assert SERVICE_CLIENT_ID not in label
    assert SERVICE_CLIENT_ID.split(".")[0] not in label


def test_client_label_unmapped_user_is_generic(mock_settings: Any) -> None:
    mock_settings.mcp_client_names = f"{OTHER_CLIENT_ID}=claude-code"
    label = client_label(AccessIdentity(kind="user", value=EMAIL))
    assert label == UNMAPPED_USER
    assert EMAIL not in label


def test_client_label_maps_case_insensitively(mock_settings: Any) -> None:
    mock_settings.mcp_client_names = f" {SERVICE_CLIENT_ID.upper()} = nanoclaw , {EMAIL.upper()}=john-laptop,"
    assert client_label(AccessIdentity(kind="service", value=SERVICE_CLIENT_ID)) == "nanoclaw"
    assert client_label(AccessIdentity(kind="user", value=EMAIL)) == "john-laptop"


@pytest.mark.parametrize("names", ["", f"{OTHER_CLIENT_ID}=a", f"{SERVICE_CLIENT_ID}=claude.code_1-x"])
@pytest.mark.parametrize("kind", ["service", "user"])
def test_client_label_is_a_name_or_a_generic_label(mock_settings: Any, names: str, kind: str) -> None:
    """Whatever the mapping, the label matches the client-name pattern or is one of the generic labels."""
    mock_settings.mcp_client_names = names
    identity = AccessIdentity(kind="service" if kind == "service" else "user", value=SERVICE_CLIENT_ID)
    label = client_label(identity)
    assert label in (UNMAPPED_SERVICE, UNMAPPED_USER) or re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,31}", label)
    assert label != SERVICE_CLIENT_ID


# --- identity_ref ---


def test_identity_ref_is_a_short_stable_hash() -> None:
    ref = identity_ref(AccessIdentity(kind="service", value=SERVICE_CLIENT_ID))
    assert re.fullmatch(r"[0-9a-f]{8}", ref)
    assert ref not in SERVICE_CLIENT_ID
    assert ref == identity_ref(AccessIdentity(kind="service", value=SERVICE_CLIENT_ID.upper()))
    assert ref != identity_ref(AccessIdentity(kind="service", value=OTHER_CLIENT_ID))


# --- mcp_audiences ---


def test_mcp_audiences_split_and_trimmed(mock_settings: Any) -> None:
    mock_settings.mcp_cf_access_aud = f" {AUD} , second-app,,"
    assert mcp_audiences() == [AUD, "second-app"]
    mock_settings.mcp_cf_access_aud = ""
    assert mcp_audiences() == []


# --- settings ---


def test_mcp_settings_defaults() -> None:
    settings = _settings()
    assert settings.mcp_access_mode == "log"
    assert settings.mcp_cf_access_aud == ""
    assert settings.mcp_client_names == ""


def test_mcp_access_mode_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        _settings(mcp_access_mode="strict")


def test_client_names_parse() -> None:
    raw = f"  {SERVICE_CLIENT_ID} = claude-code , {EMAIL.upper()}=john.laptop_2,"
    assert parse_client_names(raw) == {SERVICE_CLIENT_ID: "claude-code", EMAIL: "john.laptop_2"}
    assert parse_client_names("") == {}


@pytest.mark.parametrize(
    "raw",
    [
        SERVICE_CLIENT_ID,  # no "="
        "=claude-code",  # no identity
        f"{SERVICE_CLIENT_ID}=",  # no name
        f"{SERVICE_CLIENT_ID}=Claude",  # uppercase name
        f"{SERVICE_CLIENT_ID}=-claude",  # name starts with a symbol
        f"{SERVICE_CLIENT_ID}=claude code",  # space inside the name
        f"{SERVICE_CLIENT_ID}={'a' * 33}",  # name too long
        f"{SERVICE_CLIENT_ID}=a,{SERVICE_CLIENT_ID.upper()}=b",  # same identity twice
        f"{OTHER_CLIENT_ID}=ok,{SERVICE_CLIENT_ID}=bad/name",  # second pair bad
    ],
)
def test_malformed_client_names_rejected(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """A malformed pair fails the settings load, and the error never quotes the identity."""
    monkeypatch.setenv("MCP_CLIENT_NAMES", raw)
    with pytest.raises(ValidationError) as exc_info:
        _settings()
    message = str(exc_info.value)
    assert "MCP_CLIENT_NAMES pair" in message
    for secret in (SERVICE_CLIENT_ID, SERVICE_CLIENT_ID.upper(), OTHER_CLIENT_ID, SERVICE_CLIENT_ID.split(".")[0]):
        assert secret not in message
    assert "input_value" not in message


def test_settings_errors_never_quote_input() -> None:
    """hide_input_in_errors: a model-level failure does not print the settings dict (secrets included)."""
    with pytest.raises(ValidationError) as exc_info:
        _settings(llm_provider="anthropic", smtp_password="fake-smtp-secret")
    assert "fake-smtp-secret" not in str(exc_info.value)
    assert "input_value" not in str(exc_info.value)
