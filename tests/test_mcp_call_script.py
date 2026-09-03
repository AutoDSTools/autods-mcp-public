"""Guards on ``scripts/mcp_call.py``'s sign-in target.

The failure these pin is not hypothetical and has happened more than once: a
sign-in run from this checkout authorizes against whatever Cognito ``.env``
names, so pointing ``MCP_URL`` at another environment either opens the wrong
browser flow or — the expensive, silent case — serves a cached token from the
wrong environment and 401s in a way that reads exactly like a broken release.

``scripts/`` is not a package and is not shipped, so the module is loaded by
path rather than imported.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mcp_call.py"


def _load_mcp_call():
    spec = importlib.util.spec_from_file_location("_mcp_call_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


mcp_call = _load_mcp_call()


def _settings(domain: str = "https://auth-staging.autods.com") -> SimpleNamespace:
    """Just the fields the guard and the cache key read."""
    return SimpleNamespace(
        cognito_domain=domain,
        cognito_authorization_endpoint=f"{domain}/oauth2/authorize",
        cognito_public_client_id="client-id",
    )


def test_a_mismatched_target_refuses_instead_of_minting_a_doomed_token(monkeypatch):
    monkeypatch.setattr(
        mcp_call,
        "_advertised_authorization_endpoint",
        lambda _url: "https://auth.autods.com/oauth2/authorize",
    )
    with pytest.raises(SystemExit) as excinfo:
        mcp_call._refuse_on_environment_mismatch("https://mcp.autods.com/mcp", _settings())

    message = str(excinfo.value)
    # The operator has to be able to act on this without reading the source:
    # both environments named, and the way out spelled out.
    assert "auth.autods.com" in message
    assert "auth-staging.autods.com" in message
    assert "scripts/mcp_token.py" in message


def test_a_matching_target_is_allowed(monkeypatch):
    monkeypatch.setattr(
        mcp_call,
        "_advertised_authorization_endpoint",
        lambda _url: "https://auth-staging.autods.com/oauth2/authorize",
    )
    mcp_call._refuse_on_environment_mismatch("https://mcp-staging.autods.com/mcp", _settings())


def test_a_local_target_skips_the_check_without_probing(monkeypatch):
    """``.env`` is the right config for localhost by construction."""

    def _fail(_url):
        raise AssertionError("a local target must not be probed for AS metadata")

    monkeypatch.setattr(mcp_call, "_advertised_authorization_endpoint", _fail)
    mcp_call._refuse_on_environment_mismatch(mcp_call._DEFAULT_URL, _settings())


def test_undiscoverable_metadata_warns_but_does_not_block(monkeypatch, capsys):
    """A host that cannot answer is not evidence of a mismatch."""
    monkeypatch.setattr(mcp_call, "_advertised_authorization_endpoint", lambda _url: None)
    mcp_call._refuse_on_environment_mismatch("https://mcp-staging.autods.com/mcp", _settings())

    assert "could not be confirmed" in capsys.readouterr().err


def test_the_token_cache_is_keyed_per_environment():
    """One shared cache file is how a staging token reached production."""
    staging = mcp_call._cache_path(_settings("https://auth-staging.autods.com"))
    production = mcp_call._cache_path(_settings("https://auth.autods.com"))
    assert staging != production


def test_help_is_answered_without_acquiring_a_token(monkeypatch, capsys):
    """``--help`` used to authenticate first, opening a browser sign-in."""

    def _fail(_url):
        raise AssertionError("--help must not acquire a token")

    monkeypatch.setattr(mcp_call, "get_token", _fail)
    monkeypatch.setattr(sys, "argv", ["mcp_call.py", "--help"])
    assert mcp_call.main() == 0
    assert "Usage:" in capsys.readouterr().out


def test_a_typo_is_rejected_locally_rather_than_sent_upstream(monkeypatch):
    """An unknown option reached the server as an operation_id and hit Sentry."""

    def _fail(_url):
        raise AssertionError("an unknown option must not acquire a token")

    monkeypatch.setattr(mcp_call, "get_token", _fail)
    monkeypatch.setattr(sys, "argv", ["mcp_call.py", "--dry-run"])
    assert mcp_call.main() == 2


def test_non_object_arguments_are_refused_before_a_token_is_acquired(monkeypatch):
    def _fail(_url):
        raise AssertionError("bad arguments must not acquire a token")

    monkeypatch.setattr(mcp_call, "get_token", _fail)
    monkeypatch.setattr(sys, "argv", ["mcp_call.py", "list_stores_api", "[1, 2]"])
    assert mcp_call.main() == 2
