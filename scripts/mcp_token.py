"""Print the MCP access token an already-authorized client holds, per environment.

Release-check section C needs a bearer token for the host under test, and the
only token that is valid for **production** is the one a real client minted
during its browser sign-in — ``mcp_call.py``'s own OAuth flow reads ``.env``
and therefore always signs in against *staging* (see the gotcha in
``CLAUDE.md``). This reads the token the Claude client already cached, so a
production section-C run needs no new secrets and no second sign-in.

Usage:
    MCP_TOKEN=$(uv run python scripts/mcp_token.py autods-public-prod) make release-checks-c
    uv run python scripts/mcp_token.py autods-public-prod --info   # target + expiry, no token

The default mode prints **only** the token, so it is safe to capture with
``$(...)`` and never needs to be pasted anywhere. ``--info`` prints the
non-secret fields (which host, which issuer, when it expires) so a target can
be confirmed *without* putting the token on screen or in a transcript — prefer
it whenever you only need to know whether a usable token exists.

This deliberately reads one field out of the client's credential store rather
than dumping the store: those entries carry live access *and refresh* tokens,
which is why ``docs/release-checks.md`` tells you never to print the raw MCP
server config.
"""

import argparse
import json
import sys
import time
from pathlib import Path

# Where the Claude CLI keeps per-MCP-server OAuth state on Linux. On macOS the
# client may hold these in the login keychain instead, in which case there is
# no file here and the token has to come from the client itself.
DEFAULT_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"

# Only this key is ever read. The same file holds the user's own claude.ai
# session under a sibling key, which is none of this script's business.
_MCP_SECTION = "mcpOAuth"


def _load_entries(path: Path) -> dict:
    """Return the ``mcpOAuth`` section, or exit with an actionable message."""
    if not path.exists():
        raise SystemExit(
            f"No credential store at {path}. Authorize the server in your client first "
            "(`claude mcp add ...` then `/mcp`); on macOS the client may keep these in the "
            "login keychain, in which case export MCP_TOKEN by hand instead."
        )
    try:
        return json.loads(path.read_text()).get(_MCP_SECTION) or {}
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Could not read {path}: {exc}")


def _select(entries: dict, alias: str) -> dict:
    """Find the single entry for ``alias``, or exit naming what is available.

    Entries are keyed by ``<serverName>|<hash>``, so the alias is matched on the
    ``serverName`` field rather than the key. More than one match means the same
    alias is registered at two scopes and there is no safe pick — say so instead
    of guessing, because the wrong one is a token for the wrong environment.
    """
    matches = {key: value for key, value in entries.items() if value.get("serverName") == alias}
    if not matches:
        known = sorted({value.get("serverName", "?") for value in entries.values()})
        raise SystemExit(
            f"No cached authorization for {alias!r}. "
            + (f"Authorized aliases: {', '.join(known)}." if known else "No MCP server is authorized yet.")
        )
    if len(matches) > 1:
        raise SystemExit(
            f"{len(matches)} cached entries name {alias!r} ({', '.join(sorted(matches))}). "
            "Remove the stale one, or export MCP_TOKEN by hand — picking one here would risk "
            "handing you a token for the other environment."
        )
    return next(iter(matches.values()))


def _expiry(entry: dict) -> tuple[float, float]:
    """``(expires_at_epoch_seconds, seconds_remaining)``; ``expiresAt`` is in ms."""
    expires_at = float(entry.get("expiresAt") or 0) / 1000
    return expires_at, expires_at - time.time()


def _print_info(alias: str, entry: dict) -> int:
    expires_at, remaining = _expiry(entry)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_at)) if expires_at else "unknown"
    print(f"alias:        {alias}")
    print(f"serverUrl:    {entry.get('serverUrl', '?')}")
    print(f"issuer:       {entry.get('issuer', '?')}")
    print(f"redirectUri:  {entry.get('redirectUri', '?')}")
    print(f"expiresAt:    {stamp}")
    if remaining > 0:
        print(f"usable:       yes ({int(remaining // 60)} min left)")
    else:
        print("usable:       NO - expired; re-run /mcp in your client to refresh it")
    print(f"accessToken:  <{len(entry.get('accessToken') or '')} chars, not printed>")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("alias", help="the MCP server alias, e.g. autods-public-prod")
    parser.add_argument(
        "--info",
        action="store_true",
        help="print the target and expiry instead of the token",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=DEFAULT_CREDENTIALS,
        help=f"credential store to read (default: {DEFAULT_CREDENTIALS})",
    )
    args = parser.parse_args()

    entry = _select(_load_entries(args.credentials), args.alias)

    if args.info:
        return _print_info(args.alias, entry)

    token = entry.get("accessToken")
    if not token:
        raise SystemExit(f"The cached entry for {args.alias!r} holds no access token; re-run /mcp in your client.")

    _, remaining = _expiry(entry)
    if remaining <= 0:
        raise SystemExit(
            f"The cached token for {args.alias!r} expired "
            f"{int(-remaining // 60)} min ago. Re-run /mcp in your client, then try again."
        )
    # A short-lived token is worse than no token here: section C would start,
    # then 401 partway through and read exactly like a broken release.
    if remaining < 120:
        print(
            f"warning: this token expires in {int(remaining)}s - refresh it before a long run",
            file=sys.stderr,
        )
    # Printing to a terminal is almost never what you want: capture it with
    # $(...) so it never lands in scrollback or a transcript.
    if sys.stdout.isatty():
        print(
            "warning: writing a live access token to your terminal; "
            "prefer MCP_TOKEN=$(uv run python scripts/mcp_token.py ...) or --info",
            file=sys.stderr,
        )

    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
