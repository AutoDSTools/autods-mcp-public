"""Read the deployed server's structured logs out of the cluster log archive.

The pods ship stdout to S3, so a release check (docs/release-checks.md O3/O4) can
verify the audit trail without cluster access — only AWS credentials that can read
``s3://autods-cluster-logs``.

Usage:
    uv run python scripts/fetch_logs.py --env staging --since 30m
    uv run python scripts/fetch_logs.py --env staging --since 30m --event request --status 500
    uv run python scripts/fetch_logs.py --env prod --since 2026-08-26T11:00 --until 2026-08-26T11:30
    uv run python scripts/fetch_logs.py --env staging --since 2h --request-id 7ffaa867...,44e9ad74... --json

Options:
    --env                 staging | prod (required)
    --since               ISO-8601 UTC, or a relative window like ``30m`` / ``2h`` (required)
    --until               ISO-8601 UTC (default: now)
    --event               structured event name, or ``all`` (default: all)
    --status              keep only entries whose upstream_status/status_code equals this
    --request-id          comma-separated request ids to keep
    --json                emit raw JSON lines instead of the table
    --assert-audit-shape  grade the matched ``tool_call`` lines against O3's contract
                          and exit non-zero on a violation
    --expect-tool-calls   with the above, also require exactly N ``tool_call`` lines

Two things that have produced wrong readings, both now surfaced in the output:

* ``--event`` used to default to ``tool_call``, so a call that looked unfiltered
  silently hid every ``request`` line and made O4's 500-flood check answer "no
  data" instead of "no 500s". The default is ``all``; the active filters are
  echoed in the header, and a run that matches nothing says which filter (or the
  archive lag) is the likely reason.
* A **relative** ``--since`` is resolved once, when the process starts. A wide
  window takes minutes to scan, so back-to-back relative queries do *not* cover
  the same period — pass absolute ``--since``/``--until`` when the window has to
  be reproducible (e.g. joining a report to a run).

Layout of the archive, which is what this script encapsulates:

* ``logs/<env>/autods-mcp/<YYYY>/<MM>/<DD>/<YYYYMMDDHHMM>_<seq>_<hash>_gz`` — one
  object per 5-minute bucket per shipper, **gzipped despite having no ``.gz``
  extension**. The 12-digit key prefix is the bucket's UTC minute, which is how a
  time window is narrowed to a handful of objects instead of a whole day.
* Each line is ``<ts>\\t<namespace>.<pod>.<container>\\t<envelope-json>``, and the
  application's own structured line is a JSON *string* inside the envelope's
  ``message``. Both the ``nginx`` and ``autods-mcp`` containers ship here; only the
  latter carries ``request`` / ``tool_call`` / ``upstream_error_detail``.
"""

import argparse
import gzip
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta

BUCKET = "autods-cluster-logs"
APP_CONTAINER = "autods-mcp"

# O3's contract: the fields every ``tool_call`` line must carry. ``cognito_username``
# actually holds ``claims.sub`` (the immutable UUID), not a username — there is no
# ``user_sub`` key, so grepping for one finds nothing on a perfectly healthy line.
_TOOL_CALL_FIELDS = (
    "request_id",
    "cognito_username",
    "autods_user_id",
    "tool_name",
    "op_id",
    "upstream_status",
    "upstream_url",
    "latency_ms",
)

# Substrings that must never appear anywhere in the logs. Two different promises:
# request/response bodies are never logged, and the caller's bearer token never
# leaves the process. Both are asserted over the *raw* matched lines rather than
# per-field, so a body smuggled into a new key is still caught.
#
# Deliberately not included: ``email``, which ``tool_call`` carries on purpose
# alongside ``autods_user_id``; and ``upstream_url``, which is a path and query,
# never a payload.
#
# ``"authorization"`` is matched as a **JSON key**, quotes included, not as a bare
# word: the AS-metadata discovery route is ``/.well-known/oauth-authorization-server``,
# so every healthy window contains the substring in a ``request`` entry's ``path``.
# Broadening this back to a bare word makes the check fail on a clean archive.
_FORBIDDEN_SUBSTRINGS = (
    '"authorization"',
    "bearer ",
    "access_token",
    "id_token",
    "request_body",
    "response_body",
    "new_products",
    "intercom_user_jwt",
    "autods_store_token",
)

# ``202608261105_0_87c2_gz`` — the leading 12 digits are the UTC minute bucket.
_KEY_TIME_RE = re.compile(r"/(\d{12})_")
_RELATIVE_RE = re.compile(r"^(\d+)([mh])$")

# Objects cover a 5-minute bucket, so an entry at 11:07 lives in the 11:05 object:
# widen the lower bound by one bucket or the first entries of a window go missing.
_BUCKET_MINUTES = 5


def _parse_when(value: str, *, now: datetime) -> datetime:
    relative = _RELATIVE_RE.match(value)
    if relative:
        amount, unit = int(relative.group(1)), relative.group(2)
        return now - timedelta(minutes=amount if unit == "m" else amount * 60)
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _aws(args: list[str]) -> bytes:
    result = subprocess.run(["aws", *args], capture_output=True)
    if result.returncode != 0:
        raise SystemExit(f"aws {' '.join(args[:3])}… failed: {result.stderr.decode(errors='replace')[:500]}")
    return result.stdout


def _list_keys(env: str, since: datetime, until: datetime) -> list[str]:
    """Every archive key whose 5-minute bucket overlaps [since, until]."""
    lower = since - timedelta(minutes=_BUCKET_MINUTES)
    keys: list[str] = []
    day = since.date()
    while day <= until.date():
        prefix = f"logs/{env}/autods-mcp/{day:%Y/%m/%d}/"
        payload = _aws(["s3api", "list-objects-v2", "--bucket", BUCKET, "--prefix", prefix, "--output", "json"])
        for entry in json.loads(payload or "{}").get("Contents", []):
            match = _KEY_TIME_RE.search(entry["Key"])
            if not match:
                continue
            bucket_time = datetime.strptime(match.group(1), "%Y%m%d%H%M").replace(tzinfo=UTC)
            if lower <= bucket_time <= until:
                keys.append(entry["Key"])
        day += timedelta(days=1)
    return sorted(keys)


def _read_object(key: str) -> str:
    raw = _aws(["s3", "cp", f"s3://{BUCKET}/{key}", "-"])
    try:
        raw = gzip.decompress(raw)
    except (OSError, EOFError):
        pass  # a plain-text object; the archive is not consistent about this
    return raw.decode("utf-8", errors="replace")


def _entries(text: str):
    """Yield the application's structured log dicts from one object's text."""
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        source, envelope_json = parts[1], parts[2]
        if not source.endswith(f".{APP_CONTAINER}"):
            continue
        try:
            message = json.loads(envelope_json).get("message", "")
        except ValueError:
            continue
        # ``message`` is the app's own JSON line for structured events, and plain
        # text for anything logged outside structlog (tracebacks, uvicorn startup).
        if not message.startswith("{"):
            continue
        try:
            yield json.loads(message)
        except ValueError:
            continue


def _matches(entry: dict, args: argparse.Namespace, since: datetime, until: datetime) -> bool:
    timestamp = entry.get("timestamp")
    if timestamp:
        when = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if not since <= when <= until:
            return False
    if args.event != "all" and entry.get("event") != args.event:
        return False
    if args.status is not None and args.status not in (entry.get("upstream_status"), entry.get("status_code")):
        return False
    return not (args.request_ids and entry.get("request_id") not in args.request_ids)


def _describe_filters(args: argparse.Namespace) -> str:
    """Human-readable list of the filters actually in force, for the header/footer."""
    active = [f"event={args.event}"]
    if args.status is not None:
        active.append(f"status={args.status}")
    if args.request_ids:
        active.append(f"request-id={len(args.request_ids)} id(s)")
    return " ".join(active)


def _first(entry: dict, *keys: str, default: str = "-"):
    """First key present with a non-``None`` value — ``0`` / ``0.0`` are real values."""
    for key in keys:
        if entry.get(key) is not None:
            return entry[key]
    return default


def _format(entry: dict) -> str:
    timestamp = (entry.get("timestamp") or "")[:19]
    status = _first(entry, "upstream_status", "status_code")
    latency = _first(entry, "latency_ms", "duration_ms")
    name = entry.get("tool_name") or entry.get("path") or entry.get("op_id") or ""
    return (
        f"{timestamp}  {entry.get('request_id', '')[:8]}  {entry.get('event', ''):<20} "
        f"{name:<30} {str(status):>5}  {str(latency):>9}ms  {entry.get('error_type') or ''}"
    ).rstrip()


def _grade_audit_shape(entries: list[dict], expected_tool_calls: int | None) -> list[str]:
    """Grade O3's three assertions over the matched entries; return the findings.

    O3 asks for one ``tool_call`` line per call the run made, each carrying the
    documented fields, and no request or response bodies anywhere. All three were
    read by hand from the table before this existed, which is how a *false pass*
    happens: the fields are easy to skim past, and "no bodies" is not something a
    human verifies by looking at 90 lines.

    Returns findings rather than raising so the caller can print every one.
    """
    findings: list[str] = []
    tool_calls = [entry for entry in entries if entry.get("event") == "tool_call"]

    for entry in tool_calls:
        missing = [field for field in _TOOL_CALL_FIELDS if field not in entry]
        if missing:
            findings.append(
                f"tool_call {entry.get('request_id', '?')[:8]} ({entry.get('tool_name', '?')}) is missing {missing}"
            )

    # One line per call, keyed on request_id: a duplicated audit line and a
    # missing one are different bugs, and the count alone hides both.
    seen: Counter = Counter(entry.get("request_id") for entry in tool_calls)
    duplicated = {rid: count for rid, count in seen.items() if count > 1}
    if duplicated:
        findings.append(f"more than one tool_call line for request_id(s): {duplicated}")

    if expected_tool_calls is not None and len(tool_calls) != expected_tool_calls:
        findings.append(
            f"expected {expected_tool_calls} tool_call line(s), found {len(tool_calls)} "
            "(the archive lags a few minutes — retry before reading a shortfall as a lost audit line)"
        )

    haystack = json.dumps(entries).lower()
    leaked = [needle for needle in _FORBIDDEN_SUBSTRINGS if needle in haystack]
    if leaked:
        findings.append(f"forbidden substring(s) present in the logs: {leaked}")

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", required=True, choices=("staging", "prod"))
    parser.add_argument("--since", required=True, help="ISO-8601 UTC, or a relative window like 30m / 2h")
    parser.add_argument("--until", default=None, help="ISO-8601 UTC (default: now)")
    parser.add_argument("--event", default="all", help="structured event name, or 'all' (default: all)")
    parser.add_argument("--status", type=int, default=None)
    parser.add_argument("--request-id", default="", help="comma-separated request ids")
    parser.add_argument("--json", action="store_true", help="emit raw JSON lines")
    parser.add_argument(
        "--assert-audit-shape",
        action="store_true",
        help="grade the matched tool_call lines against O3's contract; exit 3 on a violation",
    )
    parser.add_argument(
        "--expect-tool-calls",
        type=int,
        default=None,
        help="with --assert-audit-shape, require exactly this many tool_call lines",
    )
    args = parser.parse_args()

    now = datetime.now(UTC)
    since = _parse_when(args.since, now=now)
    until = _parse_when(args.until, now=now) if args.until else now
    args.request_ids = {rid.strip() for rid in args.request_id.split(",") if rid.strip()}

    keys = _list_keys(args.env, since, until)
    if not keys:
        print(
            f"no log objects for {args.env} in {since:%Y-%m-%dT%H:%M}Z..{until:%Y-%m-%dT%H:%M}Z "
            "(the archive lags by a few minutes — retry shortly)",
            file=sys.stderr,
        )
        return 1
    # Echo the filters, not just the window: a silently-applied --event filter is
    # what made a "no data" answer look like "nothing happened" (see the docstring).
    print(
        f"# {len(keys)} objects, {args.env}, {since:%Y-%m-%dT%H:%M}Z..{until:%Y-%m-%dT%H:%M}Z"
        f", filters: {_describe_filters(args)}",
        file=sys.stderr,
    )

    matched = 0
    seen_events: Counter = Counter()
    kept: list[dict] = []
    for key in keys:
        for entry in _entries(_read_object(key)):
            seen_events[entry.get("event")] += 1
            if not _matches(entry, args, since, until):
                continue
            matched += 1
            if args.assert_audit_shape:
                kept.append(entry)
            print(json.dumps(entry) if args.json else _format(entry))

    if matched:
        # Per-event tally, so "exactly one tool_call line per call" (O3) is
        # readable off the footer instead of being counted by hand.
        tally = ", ".join(f"{name}={count}" for name, count in sorted(seen_events.items()) if name)
        print(f"# {matched} entries matched; scanned events: {tally}", file=sys.stderr)
        if args.assert_audit_shape:
            findings = _grade_audit_shape(kept, args.expect_tool_calls)
            if findings:
                print(f"# AUDIT SHAPE: {len(findings)} finding(s)", file=sys.stderr)
                for finding in findings:
                    print(f"#   - {finding}", file=sys.stderr)
                return 3
            graded = sum(1 for entry in kept if entry.get("event") == "tool_call")
            print(
                f"# AUDIT SHAPE: ok — {graded} tool_call line(s), one per request_id, all "
                f"{len(_TOOL_CALL_FIELDS)} documented fields present, no bodies or tokens",
                file=sys.stderr,
            )
        return 0

    print(f"# 0 entries matched out of {sum(seen_events.values())} scanned", file=sys.stderr)
    if seen_events:
        present = ", ".join(f"{name}={count}" for name, count in sorted(seen_events.items()) if name)
        print(f"#   the window does hold events ({present}) — a filter excluded them all.", file=sys.stderr)
        print(f"#   active filters: {_describe_filters(args)}", file=sys.stderr)
    else:
        print(
            "#   the objects held no app events at all — the archive lags a few "
            "minutes, so retry, or widen the window.",
            file=sys.stderr,
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
