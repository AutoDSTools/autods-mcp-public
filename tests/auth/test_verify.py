"""B2 acceptance — verify_token raises the right typed exception per failure mode."""

from __future__ import annotations

from typing import Any

import pytest

from autods_mcp_server.auth.exceptions import (
    InvalidAudience,
    InvalidIssuer,
    InvalidSignature,
    MalformedToken,
    TokenExpired,
    UnknownKid,
)
from autods_mcp_server.auth.jwks import JWKSClient
from autods_mcp_server.auth.verify import Claims, verify_token
from autods_mcp_server.settings import Settings
from tests.auth.conftest import (
    TEST_CLIENT_ID,
    TEST_ISSUER,
    TEST_JWKS_URL,
    SigningKey,
)


def _settings() -> Settings:
    return Settings(
        MCP_ENV="local",
        COGNITO_USER_POOL_ID="us-west-2_TESTPOOL",
        COGNITO_REGION="us-west-2",
        ALLOWED_COGNITO_CLIENT_IDS=[TEST_CLIENT_ID],
        COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
        COGNITO_PUBLIC_CLIENT_ID=TEST_CLIENT_ID,
    )


def _jwks_client_from(payload: dict[str, Any]) -> JWKSClient:
    async def fetch(_url: str) -> dict[str, Any]:
        return payload

    return JWKSClient(jwks_url=TEST_JWKS_URL, fetcher=fetch)


async def test_happy_path_returns_parsed_claims(make_token, make_jwks) -> None:
    token = make_token(extra_claims={"email": "alice@example.com", "cognito:groups": ["User"]})
    jwks = _jwks_client_from(make_jwks())

    claims = await verify_token(token, jwks=jwks, settings=_settings())

    assert isinstance(claims, Claims)
    assert claims.sub == "user-1"
    assert claims.iss == TEST_ISSUER
    assert claims.client_id == TEST_CLIENT_ID
    assert claims.email == "alice@example.com"
    assert claims.groups == ["User"]


async def test_expired_token_raises_token_expired(make_token, make_jwks) -> None:
    token = make_token(exp_offset=-60)  # exp 60s in the past
    jwks = _jwks_client_from(make_jwks())

    with pytest.raises(TokenExpired):
        await verify_token(token, jwks=jwks, settings=_settings())


async def test_wrong_client_id_raises_invalid_audience(make_token, make_jwks) -> None:
    token = make_token(client_id="some-other-client")
    jwks = _jwks_client_from(make_jwks())

    with pytest.raises(InvalidAudience):
        await verify_token(token, jwks=jwks, settings=_settings())


async def test_missing_client_id_raises_invalid_audience(make_token, make_jwks) -> None:
    """An ID token (no `client_id` claim) is rejected as the wrong token type."""
    token = make_token(omit_client_id=True)
    jwks = _jwks_client_from(make_jwks())

    with pytest.raises(InvalidAudience):
        await verify_token(token, jwks=jwks, settings=_settings())


async def test_wrong_token_use_raises_invalid_audience(make_token, make_jwks) -> None:
    """An ID-shaped token (token_use='id') is rejected even if it carries a client_id."""
    token = make_token(token_use="id")
    jwks = _jwks_client_from(make_jwks())

    with pytest.raises(InvalidAudience):
        await verify_token(token, jwks=jwks, settings=_settings())


async def test_missing_token_use_raises_invalid_audience(make_token, make_jwks) -> None:
    """A token with no token_use claim is rejected (not a Cognito access token)."""
    token = make_token(extra_claims={"token_use": None})
    jwks = _jwks_client_from(make_jwks())

    with pytest.raises(InvalidAudience):
        await verify_token(token, jwks=jwks, settings=_settings())


async def test_wrong_issuer_raises_invalid_issuer(make_token, make_jwks) -> None:
    token = make_token(iss="https://cognito-idp.us-west-2.amazonaws.com/wrong-pool")
    jwks = _jwks_client_from(make_jwks())

    with pytest.raises(InvalidIssuer):
        await verify_token(token, jwks=jwks, settings=_settings())


async def test_bad_signature_raises_invalid_signature(
    make_token, make_jwks, signing_key: SigningKey, foreign_signing_key: SigningKey
) -> None:
    # Token signed with a key whose JWK we do NOT publish, but it
    # claims the same kid as a key we DO publish — so JWKS lookup
    # succeeds and the signature check fails.
    token = make_token(key=foreign_signing_key, kid_override=signing_key.kid)
    jwks = _jwks_client_from(make_jwks(signing_key))

    with pytest.raises(InvalidSignature):
        await verify_token(token, jwks=jwks, settings=_settings())


async def test_unknown_kid_raises_unknown_kid(make_token, make_jwks) -> None:
    token = make_token(kid_override="kid-not-in-jwks")
    jwks = _jwks_client_from(make_jwks())

    with pytest.raises(UnknownKid):
        await verify_token(token, jwks=jwks, settings=_settings())


async def test_malformed_token_raises_malformed_token(make_jwks) -> None:
    jwks = _jwks_client_from(make_jwks())
    with pytest.raises(MalformedToken):
        await verify_token("not.a.jwt", jwks=jwks, settings=_settings())


async def test_token_missing_kid_header_raises_malformed_token(make_token, make_jwks, signing_key: SigningKey) -> None:
    import jwt as pyjwt

    token = pyjwt.encode(
        {"sub": "x", "iss": TEST_ISSUER, "client_id": TEST_CLIENT_ID, "exp": 9999999999},
        signing_key.private_key,
        algorithm="RS256",
        # No kid in headers
    )
    jwks = _jwks_client_from(make_jwks())
    with pytest.raises(MalformedToken):
        await verify_token(token, jwks=jwks, settings=_settings())


async def test_token_missing_required_claim_raises_malformed_token(make_jwks, signing_key: SigningKey) -> None:
    """A token missing a `require`-listed claim (e.g. `exp`) is malformed,
    not a signature failure."""
    import jwt as pyjwt

    token = pyjwt.encode(
        # `exp` is in `require=["exp", "iss"]` — leaving it out triggers
        # MissingRequiredClaimError inside pyjwt.decode.
        {"sub": "x", "iss": TEST_ISSUER, "client_id": TEST_CLIENT_ID, "token_use": "access"},
        signing_key.private_key,
        algorithm="RS256",
        headers={"kid": signing_key.kid},
    )
    jwks = _jwks_client_from(make_jwks())
    with pytest.raises(MalformedToken):
        await verify_token(token, jwks=jwks, settings=_settings())


async def test_token_with_disallowed_algorithm_raises_malformed_token(make_jwks, signing_key: SigningKey) -> None:
    """A token signed with HS256 (not in `algorithms=["RS256"]`) is malformed.

    `pyjwt.decode` raises InvalidAlgorithmError before checking the signature,
    so this lands in the MalformedToken bucket — not InvalidSignature.
    """
    import jwt as pyjwt

    token = pyjwt.encode(
        {"sub": "x", "iss": TEST_ISSUER, "client_id": TEST_CLIENT_ID, "exp": 9999999999},
        "secret",
        algorithm="HS256",
        headers={"kid": signing_key.kid},
    )
    jwks = _jwks_client_from(make_jwks())
    with pytest.raises(MalformedToken):
        await verify_token(token, jwks=jwks, settings=_settings())
