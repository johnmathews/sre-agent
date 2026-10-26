"""Test helpers: RSA signers and a JWKS document for Cloudflare Access JWT tests (keys generated in-test)."""

import json
import time
from functools import cache
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

TEAM = "team.example.cloudflareaccess.com"
AUD = "0123456789abcdef" * 4
ISSUER = f"https://{TEAM}"
CERTS_URL = f"https://{TEAM}/cdn-cgi/access/certs"
EMAIL = "admin@example.com"


@cache
def _private_key(kid: str) -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Signer:
    """Signs Access-shaped JWTs with a per-``kid`` RSA key (cached across tests)."""

    def __init__(self, kid: str) -> None:
        self.kid = kid
        self.key = _private_key(kid)

    def jwk(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        data.update(kid=self.kid, alg="RS256", use="sig")
        return data

    def claims(self, **overrides: Any) -> dict[str, Any]:
        now = int(time.time())
        claims: dict[str, Any] = {
            "aud": [AUD],
            "iss": ISSUER,
            "email": EMAIL,
            "sub": "user-1",
            "iat": now,
            "exp": now + 600,
        }
        claims.update(overrides)
        return {k: v for k, v in claims.items() if v is not None}

    def token(self, **overrides: Any) -> str:
        return jwt.encode(self.claims(**overrides), self.key, algorithm="RS256", headers={"kid": self.kid})


def jwks(*signers: Signer) -> dict[str, Any]:
    return {"keys": [s.jwk() for s in signers]}
