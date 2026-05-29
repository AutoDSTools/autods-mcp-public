"""Shared auth-test fixtures: RSA keypair, token signer, JWKS builder.

Generating a real RSA keypair per-session lets us exercise the
signature verification path end-to-end without mocking PyJWT itself.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from jwt.algorithms import JWKDict, RSAAlgorithm

TEST_ISSUER = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_TESTPOOL"
TEST_CLIENT_ID = "test-client-id"
TEST_JWKS_URL = f"{TEST_ISSUER}/.well-known/jwks.json"


@dataclass(frozen=True)
class SigningKey:
    kid: str
    private_key: RSAPrivateKey

    def jwk(self) -> JWKDict:
        public_pem = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        # Round-trip through PyJWT's RSAAlgorithm so we emit the exact
        # JWK shape Cognito would publish (kty/n/e/alg/use).
        jwk_json = RSAAlgorithm.to_jwk(serialization.load_pem_public_key(public_pem))

        jwk = json.loads(jwk_json)
        jwk["kid"] = self.kid
        jwk["alg"] = "RS256"
        jwk["use"] = "sig"
        return jwk


def _generate_keypair(kid: str) -> SigningKey:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return SigningKey(kid=kid, private_key=private_key)


@pytest.fixture(scope="session")
def signing_key() -> SigningKey:
    return _generate_keypair(kid="primary-kid")


@pytest.fixture(scope="session")
def rotated_signing_key() -> SigningKey:
    return _generate_keypair(kid="rotated-kid")


@pytest.fixture(scope="session")
def foreign_signing_key() -> SigningKey:
    """A key whose JWK we never publish — used to test InvalidSignature."""
    return _generate_keypair(kid="primary-kid")


@pytest.fixture
def make_token(signing_key: SigningKey) -> Callable[..., str]:
    """Factory: build a signed Cognito-shaped access token with overridable claims."""

    def _make(
        *,
        sub: str = "user-1",
        iss: str = TEST_ISSUER,
        client_id: str = TEST_CLIENT_ID,
        exp_offset: int = 3600,
        token_use: str = "access",
        extra_claims: dict[str, Any] | None = None,
        key: SigningKey | None = None,
        kid_override: str | None = None,
        omit_client_id: bool = False,
    ) -> str:
        key = key or signing_key
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": sub,
            "iss": iss,
            "exp": now + exp_offset,
            "iat": now,
            "token_use": token_use,
        }
        if not omit_client_id:
            claims["client_id"] = client_id
        if extra_claims:
            claims.update(extra_claims)
        return pyjwt.encode(
            claims,
            key.private_key,
            algorithm="RS256",
            headers={"kid": kid_override or key.kid},
        )

    return _make


@pytest.fixture
def make_jwks(signing_key: SigningKey) -> Callable[..., dict[str, Any]]:
    """Factory: build a JWKS doc from one or more SigningKeys."""

    def _make(*keys: SigningKey) -> dict[str, Any]:
        if not keys:
            keys = (signing_key,)
        return {"keys": [k.jwk() for k in keys]}

    return _make
