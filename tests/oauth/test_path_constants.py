"""Guards against the duplicated-path-constant regressions fixed in RD-53.

The 401 ``WWW-Authenticate`` challenge (auth side) and the served PRM
route (oauth side) must advertise the *same* path, and the AS-metadata
``registration_endpoint`` must point at the *same* path the DCR route is
registered on. These previously lived as independent string literals in
separate modules; if anyone re-introduces a second source of truth these
tests fail before a client ever sees a broken discovery handshake.
"""

from autods_mcp_server import auth, oauth, urls
from autods_mcp_server.oauth.metadata import build_as_metadata, build_prm


def test_prm_path_has_single_source_of_truth() -> None:
    """auth (challenge) and oauth (route) resolve the *same* PRM_PATH object."""
    assert auth.PRM_PATH is urls.PRM_PATH
    assert oauth.PRM_PATH is urls.PRM_PATH


def test_as_metadata_registration_endpoint_tracks_dcr_path() -> None:
    """registration_endpoint is built from DCR_PATH, not a copy-pasted literal.

    Changing DCR_PATH must move the advertised endpoint in lockstep.
    """
    base = "https://mcp.autods.com"
    metadata = build_as_metadata(
        base_url=base,
        authorization_endpoint="https://cognito.example/oauth2/authorize",
        token_endpoint="https://cognito.example/oauth2/token",
        jwks_uri="https://cognito.example/.well-known/jwks.json",
        scopes=["openid"],
    )
    assert str(metadata.registration_endpoint) == f"{base}{oauth.DCR_PATH}"


def test_prm_resource_tracks_mcp_path() -> None:
    """resource is the canonical MCP URL, built from MCP_PATH, not a literal.

    Changing MCP_PATH must move the advertised resource identifier in lockstep.
    """
    base = "https://mcp.autods.com"
    prm = build_prm(base_url=base, scopes=["openid"])
    assert prm.resource == f"{base}{urls.MCP_PATH}"


def test_advertised_issuer_and_resource_have_no_trailing_slash() -> None:
    """RFC 8414 §3.3 / RFC 9728 require byte-identity with the fetch URL —
    the bare-origin issuer/AS entries must not pick up a normalising slash."""
    base = "https://mcp.autods.com"
    prm = build_prm(base_url=base, scopes=["openid"])
    metadata = build_as_metadata(
        base_url=base,
        authorization_endpoint="https://cognito.example/oauth2/authorize",
        token_endpoint="https://cognito.example/oauth2/token",
        jwks_uri="https://cognito.example/.well-known/jwks.json",
        scopes=["openid"],
    )
    assert metadata.issuer == base
    assert prm.authorization_servers == [base]
