"""Section S of ``docs/release-checks.md``, automated.

*Deployment identity and the unauthenticated surface* — the checks that run
first after a release because they fail fastest and they need no credentials.
Everything here talks to a **deployed** server over the network (staging by
default); nothing in this module stands the app up in-process, so a green run
says something about the pod, not about the checkout.

Run it::

    make release-checks                                   # staging
    RUN_RELEASE_CHECKS=1 MCP_RELEASE_BASE_URL=https://mcp.autods.com \\
      uv run pytest tests/e2e/test_release_checks_s.py    # production

Every check is read-only and safe against production. S5 POSTs to the DCR
shim, which is a pure function of settings — it registers nothing and stores
nothing.

Failure triage, per the checklist's rules: a wrong value here is a **release**
problem (the rollout didn't land, an env var moved, a redirect allowlist
drifted), not an upstream one — none of these paths touch AutoDSApi or
ProductsResearch. Report the check id, the URL, and the response body.
"""

import re
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import UNREGISTERED_REDIRECT_URI, ReleaseTarget

# ``https://cognito-idp.<region>.amazonaws.com/<pool-id>/.well-known/jwks.json``
_JWKS_URL_RE = re.compile(
    r"^https://cognito-idp\.(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
    r"(?P<pool>(?P=region)_[A-Za-z0-9]+)/\.well-known/jwks\.json$"
)


def _json(response: httpx.Response) -> Any:
    """Parse a JSON body, failing with the raw text when it isn't JSON.

    A deployment fronted by nginx/ALB/Cloudflare can answer with an HTML error
    page; ``response.json()`` would then raise a decode error that says nothing
    about what actually came back.
    """
    try:
        return response.json()
    except ValueError:
        pytest.fail(f"{response.request.url} returned non-JSON ({response.status_code}): {response.text[:500]}")


def test_s1_health(probe: httpx.Client, release_target: ReleaseTarget) -> None:
    """S1 — ``/health`` answers 200 ``{"status": "ok"}``.

    This is the ALB target-group health check as well, so a failure here means
    the pod is out of rotation (or the route in front of it is broken), not
    that a feature regressed.
    """
    response = probe.get(f"{release_target.base_url}/health")

    assert response.status_code == 200, f"/health returned {response.status_code}: {response.text[:300]}"
    assert _json(response) == {"status": "ok"}


def test_s2_protected_resource_metadata(probe: httpx.Client, release_target: ReleaseTarget) -> None:
    """S2 — PRM (RFC 9728) advertises this server as the resource.

    ``resource`` must be **byte-identical** to the endpoint clients POST to.
    RFC 9728 / RFC 8707 make it the audience identifier, so a trailing slash —
    or a host derived from a proxy header instead of ``PUBLIC_HOSTNAME`` —
    breaks discovery for every client that has not connected yet, while
    already-authorized clients keep working. That asymmetry is why this is
    checked on the deployed host and not just in unit tests.
    """
    response = probe.get(release_target.prm_url)

    assert response.status_code == 200, f"PRM returned {response.status_code}: {response.text[:300]}"
    document = _json(response)

    assert document["resource"] == release_target.mcp_url
    assert document["authorization_servers"] == [release_target.base_url]
    assert document["scopes_supported"], "scopes_supported is empty; clients would request no scopes"
    assert document.get("bearer_methods_supported") == ["header"]


def test_s3_authorization_server_metadata(probe: httpx.Client, release_target: ReleaseTarget) -> None:
    """S3 — AS metadata (RFC 8414) points at the right Cognito for this env.

    We are a *proxy* AS: ``issuer`` is us (byte-identical to the URL this
    document was fetched from, RFC 8414 §3.3), authorize/token point straight
    at the environment's Cognito Hosted UI, and ``registration_endpoint`` comes
    back to our DCR shim because Cognito doesn't speak DCR.

    The JWKS URL is fetched rather than pattern-matched alone: the same pool
    that mints tokens must be the one this server verifies against, and a wrong
    ``COGNITO_USER_POOL_ID`` in the rollout looks perfectly well-formed right
    up until every sign-in fails.
    """
    response = probe.get(release_target.as_metadata_url)

    assert response.status_code == 200, f"AS metadata returned {response.status_code}: {response.text[:300]}"
    document = _json(response)

    assert document["issuer"] == release_target.base_url
    assert document["registration_endpoint"] == release_target.registration_url
    assert document["scopes_supported"], "scopes_supported is empty"

    authorize, token = document["authorization_endpoint"], document["token_endpoint"]
    if release_target.cognito_hosted_ui_base_url is not None:
        hosted_ui = release_target.cognito_hosted_ui_base_url.rstrip("/")
        assert authorize == f"{hosted_ui}/oauth2/authorize"
        assert token == f"{hosted_ui}/oauth2/token"
    else:
        # An unknown host: assert the shape instead of the value, and say so.
        assert authorize.endswith("/oauth2/authorize") and authorize.startswith("https://")
        assert token.endswith("/oauth2/token")
        assert authorize.removesuffix("/oauth2/authorize") == token.removesuffix("/oauth2/token")

    jwks_match = _JWKS_URL_RE.match(document["jwks_uri"])
    assert jwks_match is not None, f"jwks_uri is not a Cognito user-pool JWKS URL: {document['jwks_uri']!r}"

    jwks = probe.get(document["jwks_uri"])
    assert jwks.status_code == 200, f"jwks_uri is unreachable ({jwks.status_code}) — no client can verify a token"
    assert _json(jwks).get("keys"), f"jwks_uri {document['jwks_uri']} published no keys"

    # The Hosted UI domain must resolve and answer. Cognito rejects a
    # parameterless authorize with a 4xx, which is fine — we are proving the
    # domain exists, not driving the flow. A DNS/TLS failure raises here.
    hosted_ui_probe = probe.get(authorize)
    assert hosted_ui_probe.status_code < 500, (
        f"Cognito Hosted UI {authorize} answered {hosted_ui_probe.status_code}; sign-in would fail for everyone"
    )


def test_s4_unauthenticated_mcp_returns_the_discovery_challenge(
    probe: httpx.Client, release_target: ReleaseTarget
) -> None:
    """S4 — an unauthenticated ``POST /mcp`` returns the 401 + challenge.

    ``WWW-Authenticate: Bearer resource_metadata="…"`` *is* the whole discovery
    loop — without it no new client can bootstrap OAuth, and the symptom users
    report is "the connector just won't connect". The advertised URL must be
    the PRM endpoint S2 just read, byte for byte.

    A garbage bearer token is probed too, because the two failure modes are
    worth distinguishing on a deployed build: 401 means the token was rejected
    (correct), 503 means the pod cannot reach Cognito's JWKS at all — which
    fails *every* sign-in and is invisible from S1-S3.
    """
    response = probe.post(
        release_target.mcp_url,
        headers={"Content-Type": "application/json"},
        json={},
    )

    assert response.status_code == 401, f"POST /mcp without a token returned {response.status_code}, expected 401"
    challenge = response.headers.get("WWW-Authenticate")
    assert challenge is not None, "no WWW-Authenticate header; clients cannot discover the authorization server"
    assert f'resource_metadata="{release_target.prm_url}"' in challenge, (
        f"challenge advertises the wrong PRM URL: {challenge!r}"
    )
    assert challenge.startswith("Bearer "), f"challenge is not a Bearer challenge: {challenge!r}"

    rejected = probe.post(
        release_target.mcp_url,
        headers={"Content-Type": "application/json", "Authorization": "Bearer not-a-real-token"},
        json={},
    )
    assert rejected.status_code != 503, (
        "a bad token produced 503 — the server cannot reach the Cognito JWKS endpoint, so no token can be "
        "verified and every sign-in fails"
    )
    assert rejected.status_code == 401, f"a bad token returned {rejected.status_code}, expected 401"


def test_s5_dcr_shim_accepts_registered_and_rejects_unknown_redirects(
    probe: httpx.Client, release_target: ReleaseTarget
) -> None:
    """S5 — the DCR shim honours this environment's redirect allowlist.

    Cognito exact-matches redirect URIs and does not speak DCR, so the shim is
    the only place a mismatch can be caught with a useful message. A shim that
    accepts anything doesn't fix the mismatch — it just moves the failure to
    the Cognito sign-in page, where the user sees an opaque error.

    Note this can only prove the shim matches *its own* allowlist. That the
    allowlist mirrors the URIs registered on the Cognito app client is checked
    by C1 (a real browser sign-in), which no script can drive.
    """
    if release_target.registered_redirect_uri is None:
        pytest.skip(
            f"no known registered redirect URI for {release_target.host}; set E2E_REGISTERED_REDIRECT_URI to run S5"
        )

    accepted = probe.post(
        release_target.registration_url,
        json={"redirect_uris": [release_target.registered_redirect_uri], "client_name": "release-check"},
    )
    # RFC 7591 §3.2.1 registration success is a 201; the route declares it.
    assert accepted.status_code == 201, (
        f"registering {release_target.registered_redirect_uri!r} returned {accepted.status_code}, expected 201: "
        f"{accepted.text[:300]}"
    )
    registration = _json(accepted)
    assert registration["redirect_uris"] == [release_target.registered_redirect_uri]
    assert registration["client_id"], "the shim handed back an empty client_id"
    assert registration["token_endpoint_auth_method"] == "none"
    assert isinstance(registration["client_id_issued_at"], int)

    rejected = probe.post(release_target.registration_url, json={"redirect_uris": [UNREGISTERED_REDIRECT_URI]})
    assert rejected.status_code == 400, (
        f"an unregistered redirect URI returned {rejected.status_code}, expected 400 — the shim is accepting "
        f"anything, which moves the failure to the Cognito sign-in page: {rejected.text[:300]}"
    )
    assert _json(rejected)["detail"]["error"] == "invalid_redirect_uri"

    # One bad URI alongside a good one must still be refused: partial
    # acceptance would register a client the authorize step then rejects.
    mixed = probe.post(
        release_target.registration_url,
        json={"redirect_uris": [release_target.registered_redirect_uri, UNREGISTERED_REDIRECT_URI]},
    )
    assert mixed.status_code == 400, f"a mixed registered/unregistered list was accepted: {mixed.text[:300]}"
