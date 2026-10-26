"""Tests for Cloudflare Access JWT verification (keys generated in-test, JWKS endpoint mocked with respx)."""

import json
import logging
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from src.upgrades import access
from src.upgrades.access import AccessDenied, verify_access_jwt
from tests.upgrades_access_helpers import AUD, CERTS_URL, EMAIL, TEAM, Signer, jwks

K1 = Signer("k1")
K2 = Signer("k2")


@pytest.fixture(autouse=True)
def _env(mock_settings: Any) -> Iterator[None]:
    mock_settings.cf_access_team_domain = TEAM
    mock_settings.cf_access_aud = AUD
    access._reset_state()
    yield
    access._reset_state()


def _certs(*signers: Signer) -> respx.Route:
    return respx.get(CERTS_URL).mock(return_value=httpx.Response(200, json=jwks(*signers)))


@pytest.mark.integration
@respx.mock
async def test_jwt_valid_returns_email() -> None:
    _certs(K1)
    assert await verify_access_jwt(K1.token()) == EMAIL


@pytest.mark.integration
@respx.mock
async def test_jwt_wrong_audience_denied() -> None:
    _certs(K1)
    with pytest.raises(AccessDenied):
        await verify_access_jwt(K1.token(aud=["some-other-app"]))


@pytest.mark.integration
@respx.mock
async def test_jwt_wrong_issuer_denied() -> None:
    _certs(K1)
    with pytest.raises(AccessDenied):
        await verify_access_jwt(K1.token(iss="https://evil.cloudflareaccess.com"))


@pytest.mark.integration
@respx.mock
async def test_jwt_expired_beyond_leeway_denied() -> None:
    _certs(K1)
    now = int(time.time())
    with pytest.raises(AccessDenied):
        await verify_access_jwt(K1.token(iat=now - 3600, exp=now - 120))


@pytest.mark.integration
@respx.mock
async def test_jwt_expired_within_leeway_accepted() -> None:
    _certs(K1)
    now = int(time.time())
    assert await verify_access_jwt(K1.token(iat=now - 3600, exp=now - 30)) == EMAIL


@pytest.mark.integration
@respx.mock
@pytest.mark.parametrize("missing", ["exp", "iat", "email"])
async def test_jwt_missing_required_claim_denied(missing: str) -> None:
    _certs(K1)
    with pytest.raises(AccessDenied):
        await verify_access_jwt(K1.token(**{missing: None}))


@pytest.mark.integration
@respx.mock
@pytest.mark.parametrize("token", [None, ""])
async def test_missing_token_denied_without_fetching_keys(token: str | None) -> None:
    route = _certs(K1)
    with pytest.raises(AccessDenied):
        await verify_access_jwt(token)
    assert route.call_count == 0


@pytest.mark.integration
@respx.mock
async def test_alg_none_denied() -> None:
    route = _certs(K1)
    token = jwt.encode(K1.claims(), key=None, algorithm="none", headers={"kid": "k1"})
    with pytest.raises(AccessDenied):
        await verify_access_jwt(token)
    assert route.call_count == 0


@pytest.mark.integration
@respx.mock
async def test_hs256_denied_even_with_the_public_key_as_secret() -> None:
    _certs(K1)
    public_pem = K1.jwk()["n"]  # anything a forger could know about the public key
    token = jwt.encode(K1.claims(), key=public_pem, algorithm="HS256", headers={"kid": "k1"})
    with pytest.raises(AccessDenied):
        await verify_access_jwt(token)


@pytest.mark.integration
@respx.mock
async def test_bad_signature_denied() -> None:
    _certs(K1)
    forged = jwt.encode(K1.claims(), K2.key, algorithm="RS256", headers={"kid": "k1"})
    with pytest.raises(AccessDenied):
        await verify_access_jwt(forged)


@pytest.mark.integration
@respx.mock
async def test_garbage_token_denied() -> None:
    _certs(K1)
    with pytest.raises(AccessDenied):
        await verify_access_jwt("not.a.jwt")


@pytest.mark.integration
@respx.mock
async def test_keys_are_cached() -> None:
    route = _certs(K1)
    await verify_access_jwt(K1.token())
    await verify_access_jwt(K1.token())
    assert route.call_count == 1


@pytest.mark.integration
@respx.mock
async def test_cache_expires_after_an_hour() -> None:
    route = _certs(K1)
    with patch.object(access, "_monotonic", return_value=1000.0):
        await verify_access_jwt(K1.token())
    with patch.object(access, "_monotonic", return_value=1000.0 + 3601):
        await verify_access_jwt(K1.token())
    assert route.call_count == 2


@pytest.mark.integration
@respx.mock
async def test_unknown_kid_refetches_once_and_succeeds() -> None:
    route = respx.get(CERTS_URL).mock(
        side_effect=[httpx.Response(200, json=jwks(K1)), httpx.Response(200, json=jwks(K1, K2))]
    )
    with patch.object(access, "_monotonic", return_value=1000.0):
        assert await verify_access_jwt(K1.token()) == EMAIL
    with patch.object(access, "_monotonic", return_value=1061.0):
        assert await verify_access_jwt(K2.token()) == EMAIL  # rotated key: one refetch
    assert route.call_count == 2


@pytest.mark.integration
@respx.mock
async def test_unknown_kid_after_refetch_denied_with_a_single_refetch() -> None:
    route = _certs(K1)
    with patch.object(access, "_monotonic", return_value=1000.0):
        await verify_access_jwt(K1.token())
    with patch.object(access, "_monotonic", return_value=1061.0), pytest.raises(AccessDenied):
        await verify_access_jwt(K2.token())
    assert route.call_count == 2


@pytest.mark.integration
@respx.mock
async def test_forged_kids_refetch_at_most_once_per_minute() -> None:
    route = _certs(K1)
    forged = [Signer(f"forged-{i}").token() for i in range(3)]
    with patch.object(access, "_monotonic", return_value=1000.0):
        await verify_access_jwt(K1.token())  # the TTL fetch counts as the last fetch
    for t in (1001.0, 1030.0, 1059.0):
        with patch.object(access, "_monotonic", return_value=t):
            for token in forged:
                with pytest.raises(AccessDenied):
                    await verify_access_jwt(token)
    assert route.call_count == 1
    with patch.object(access, "_monotonic", return_value=1060.0):
        for token in forged:
            with pytest.raises(AccessDenied):
                await verify_access_jwt(token)
    assert route.call_count == 2  # one refetch, then the floor applies again


@pytest.mark.integration
@respx.mock
async def test_first_request_with_unknown_kid_fetches_once() -> None:
    route = _certs(K1)
    with pytest.raises(AccessDenied):
        await verify_access_jwt(K2.token())
    assert route.call_count == 1


@pytest.mark.integration
@respx.mock
async def test_token_without_kid_denied() -> None:
    route = _certs(K1)
    token = jwt.encode(K1.claims(), K1.key, algorithm="RS256")
    with pytest.raises(AccessDenied):
        await verify_access_jwt(token)
    assert route.call_count == 0


@pytest.mark.integration
@respx.mock
async def test_aud_as_plain_string() -> None:
    _certs(K1)
    assert await verify_access_jwt(K1.token(aud=AUD)) == EMAIL
    with pytest.raises(AccessDenied):
        await verify_access_jwt(K1.token(aud="some-other-app"))


@pytest.mark.integration
@respx.mock
async def test_iat_in_the_future_beyond_leeway_denied() -> None:
    _certs(K1)
    now = int(time.time())
    with pytest.raises(AccessDenied):
        await verify_access_jwt(K1.token(iat=now + 300, exp=now + 900))
    assert await verify_access_jwt(K1.token(iat=now + 30, exp=now + 900)) == EMAIL  # within leeway


@pytest.mark.integration
@respx.mock
async def test_jwks_mixing_key_types_uses_the_rsa_key() -> None:
    ec_jwk = json.loads(ECAlgorithm.to_jwk(ec.generate_private_key(ec.SECP256R1()).public_key()))
    ec_jwk.update(kid="ec1", alg="ES256")
    oct_jwk = {"kty": "oct", "k": "c2VjcmV0", "kid": "oct1", "alg": "HS256"}
    respx.get(CERTS_URL).mock(return_value=httpx.Response(200, json={"keys": [ec_jwk, oct_jwk, K1.jwk()]}))
    assert await verify_access_jwt(K1.token()) == EMAIL
    hs = jwt.encode(K1.claims(), "s" * 40, algorithm="HS256", headers={"kid": "oct1"})
    with pytest.raises(AccessDenied):
        await verify_access_jwt(hs)
    ec_key = ec.generate_private_key(ec.SECP256R1())
    es = jwt.encode(K1.claims(), ec_key, algorithm="ES256", headers={"kid": "ec1"})
    with pytest.raises(AccessDenied):
        await verify_access_jwt(es)


@pytest.mark.integration
@respx.mock
async def test_jwks_fetch_error_denies() -> None:
    respx.get(CERTS_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(AccessDenied):
        await verify_access_jwt(K1.token())


@pytest.mark.integration
@respx.mock
async def test_jwks_redirect_not_followed() -> None:
    respx.get(CERTS_URL).mock(return_value=httpx.Response(302, headers={"Location": "https://example.com/certs"}))
    other = respx.get("https://example.com/certs").mock(return_value=httpx.Response(200, json=jwks(K1)))
    with pytest.raises(AccessDenied):
        await verify_access_jwt(K1.token())
    assert other.call_count == 0


@pytest.mark.integration
@respx.mock
async def test_jwks_network_error_denies() -> None:
    respx.get(CERTS_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(AccessDenied):
        await verify_access_jwt(K1.token())


@pytest.mark.integration
@respx.mock
@pytest.mark.parametrize(
    "body",
    [{"keys": []}, {"nope": 1}, [1, 2], {"keys": [{"kty": "oct", "k": "c2VjcmV0", "kid": "k1"}]}],
)
async def test_jwks_without_usable_rsa_keys_denies(body: object) -> None:
    respx.get(CERTS_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(AccessDenied):
        await verify_access_jwt(K1.token())


@pytest.mark.integration
@respx.mock
@pytest.mark.parametrize(
    "team",
    [
        "evil.example.com",
        "https://team.example.cloudflareaccess.com",
        "team.example.cloudflareaccess.com/path",
        "team.example.cloudflareaccess.com:8443",
        "cloudflareaccess.com",
        "user@team.cloudflareaccess.com",
        "team.cloudflareaccess.com.evil.example",
    ],
)
async def test_bad_team_domain_denies_every_request_and_logs_once(
    mock_settings: Any, team: str, caplog: pytest.LogCaptureFixture
) -> None:
    mock_settings.cf_access_team_domain = team
    route = respx.get(url__regex=r".*").mock(return_value=httpx.Response(200, json=jwks(K1)))
    caplog.set_level(logging.ERROR, logger="src.upgrades.access")
    for _ in range(2):
        with pytest.raises(AccessDenied):
            await verify_access_jwt(K1.token())
    assert route.call_count == 0
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1


@pytest.mark.integration
@respx.mock
@pytest.mark.parametrize("field", ["cf_access_team_domain", "cf_access_aud"])
async def test_empty_access_config_denies(mock_settings: Any, field: str) -> None:
    setattr(mock_settings, field, "")
    route = _certs(K1)
    with pytest.raises(AccessDenied):
        await verify_access_jwt(K1.token())
    assert route.call_count == 0


@pytest.mark.integration
@respx.mock
async def test_token_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    _certs(K1)
    caplog.set_level(logging.DEBUG)
    token = K1.token(aud=["wrong"])
    with pytest.raises(AccessDenied):
        await verify_access_jwt(token)
    assert caplog.records, "a rejection must be logged"
    assert all(token not in r.getMessage() and token.split(".")[1] not in r.getMessage() for r in caplog.records)
