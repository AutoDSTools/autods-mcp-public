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
    --env         staging | prod (required)
    --since       ISO-8601 UTC, or a relative window like ``30m`` / ``2h`` (required)
    --until       ISO-8601 UTC (default: now)
    --event       structured event name, or ``all`` (default: tool_call)
    --status      keep only entries whose upstream_status/status_code equals this
    --request-id  comma-separated request ids to keep
    --json        emit raw JSON lines instead of the table

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
from datetime import UTC, datetime, timedelta

BUCKET = "autods-cluster-logs"
APP_CONTAINER = "autods-mcp"

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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", required=True, choices=("staging", "prod"))
    parser.add_argument("--since", required=True, help="ISO-8601 UTC, or a relative window like 30m / 2h")
    parser.add_argument("--until", default=None, help="ISO-8601 UTC (default: now)")
    parser.add_argument("--event", default="tool_call", help="structured event name, or 'all'")
    parser.add_argument("--status", type=int, default=None)
    parser.add_argument("--request-id", default="", help="comma-separated request ids")
    parser.add_argument("--json", action="store_true", help="emit raw JSON lines")
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
    print(f"# {len(keys)} objects, {args.env}, {since:%Y-%m-%dT%H:%M}Z..{until:%Y-%m-%dT%H:%M}Z", file=sys.stderr)

    matched = 0
    for key in keys:
        for entry in _entries(_read_object(key)):
            if not _matches(entry, args, since, until):
                continue
            matched += 1
            print(json.dumps(entry) if args.json else _format(entry))

    print(f"# {matched} entries", file=sys.stderr)
    return 0 if matched else 2


if __name__ == "__main__":
    raise SystemExit(main())
