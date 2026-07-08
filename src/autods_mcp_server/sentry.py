"""Sentry error/performance reporting (RD-66).

Wires the public MCP server to the company's self-hosted Sentry
(``sentry.autods.com``), mirroring ``UserService/src/integration/sentry.py``.
FastAPI/Starlette only — this service has no Celery/SQLAlchemy.

``init_sentry`` is a **no-op** in local dev or when ``SENTRY_URL`` is unset, so
local and test runs send no events. Beyond the bare init, the helpers here
attach the request / user / tool-call context that makes an event actionable,
and explicitly capture the *handled* upstream/internal failures: the service
returns those as ``CallToolResult(isError=True)`` envelopes (see ``errors.py``)
rather than raising, so Sentry's automatic exception capture would never see
them.

**The bearer token must never reach Sentry.** ``UserContext.raw_token`` and the
forwarded ``Authorization`` header are never passed into any scope here, and we
leave ``send_default_pii`` off (its default) so the SDK doesn't auto-attach
request cookies / body / client IP. The SDK also scrubs ``Authorization`` by
default. We identify the user explicitly (``set_user`` sends the fields we pass
regardless of the PII flag), so the user's stable id + email still land on
events without opting into blanket PII capture.

All helpers below short-circuit when Sentry isn't initialized, so callers on the
hot path (auth, every tool call) don't pay for context assembly when the
integration is a no-op — i.e. locally.
"""

import copy
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.scrubber import EventScrubber
from sentry_sdk.utils import AnnotatedValue

from autods_mcp_server import __version__
from autods_mcp_server.auth import UserContext
from autods_mcp_server.logging import get_logger
from autods_mcp_server.settings import Settings

_logger = get_logger("autods_mcp_server.sentry")

# Fraction of requests traced for performance. Matches the sibling services
# (UserService uses 0.01) — MCP traffic is upstream-REST-forwarding, so a low
# rate keeps overhead negligible while still surfacing latency outliers.
_TRACES_SAMPLE_RATE = 0.01


class _SensitiveDataScrubber(EventScrubber):
    """``EventScrubber`` that matches denylisted names as substrings.

    Reuses the SDK's maintained denylist, recursion, and ``[Filtered]`` masking,
    but treats a key as sensitive when a denylist entry appears *anywhere* in it,
    not only on an exact match. The base class's exact match misses the compound
    keys tool arguments and upstream bodies commonly carry — ``access_token``,
    ``api_secret``, ``user_password`` — so substring matching closes that gap. It
    errs toward over-redaction, the safe direction for a control whose job is to
    keep secrets out of Sentry.

    Used both as the SDK ``event_scrubber`` (request headers, stack-frame vars, …)
    and, via :meth:`scrub_payload`, on the custom ``contexts`` we attach — the
    base class only walks known event sections, never custom contexts.
    """

    def scrub_dict(self, d: object) -> None:
        # Mirrors EventScrubber.scrub_dict but with substring key matching; the
        # inherited scrub_list calls back into this override for nested lists.
        if not isinstance(d, dict):
            return
        for k, v in d.items():
            if isinstance(k, str) and any(marker in k.lower() for marker in self.denylist):
                d[k] = AnnotatedValue.substituted_because_contains_sensitive_data()
            elif self.recursive:
                self.scrub_dict(v)
                self.scrub_list(v)

    def scrub_payload(self, value: Any) -> Any:
        """Return a scrubbed *copy* of an arbitrary JSON-ish payload.

        Copies first: the live ``arguments`` dict is the dispatcher's upstream
        request body, so scrubbing must never mutate it. ``scrub_dict`` /
        ``scrub_list`` are no-ops on non-container values, so scalars pass through.
        """
        scrubbed = copy.deepcopy(value)
        self.scrub_dict(scrubbed)
        self.scrub_list(scrubbed)
        return scrubbed


# One scrubber, shared as the SDK event_scrubber and for our custom contexts, so
# there's a single denylist configuration. Recursive so nested payloads are
# walked.
_SCRUBBER = _SensitiveDataScrubber(recursive=True)


def init_sentry(settings: Settings) -> None:
    """Initialise the Sentry SDK for a deployed environment.

    No-op when running locally or when ``SENTRY_URL`` is empty, so local/test
    runs never emit events (mirrors ``UserService``'s dev/testing/docker skip).
    """
    if settings.is_local or not settings.sentry_url:
        _logger.debug("sentry_disabled", env=settings.mcp_env.value, has_dsn=bool(settings.sentry_url))
        return

    sentry_sdk.init(
        dsn=settings.sentry_url,
        integrations=[
            StarletteIntegration(transaction_style="url"),
            FastApiIntegration(transaction_style="url"),
        ],
        environment=settings.sentry_environment,
        # Release is the deployed code version (from __init__), so it always
        # matches what's running — no env var / chart plumbing needed.
        release=f"autods-mcp-public@{__version__}",
        traces_sample_rate=_TRACES_SAMPLE_RATE,
        # Substring-matching scrubber (see _SensitiveDataScrubber) so sensitive
        # keys are redacted even when nested or compound (e.g. access_token).
        event_scrubber=_SCRUBBER,
        # RD-71: never let the Starlette/FastAPI integration read the request
        # body. Its request-info extractor consumes the ASGI receive stream
        # *before* the route runs; the /mcp Streamable-HTTP transport then reads
        # the body itself, finds it already drained, and blocks on receive until
        # the ingress read-timeout — surfacing to clients as a failed connect.
        # "never" makes the extractor skip the body read entirely (which also
        # matches our rule that request bodies must never reach Sentry).
        max_request_body_size="never",
    )
    _logger.info(
        "sentry_initialized",
        environment=settings.sentry_environment,
        release=f"autods-mcp-public@{__version__}",
    )


def identify_user(user_context: UserContext, *, client_id: str | None) -> None:
    """Attach the authenticated caller to the current Sentry scope.

    Keyed on the stable AutoDS user id when resolved (RD-63), falling back to the
    Cognito ``sub``. ``email`` is included when known — sent explicitly via
    ``set_user`` (not blanket PII). ``client_id`` (which OAuth client — Claude /
    Cursor / inspector) and the Cognito groups become searchable tags. The
    bearer token is never referenced here.
    """
    if not sentry_sdk.is_initialized():
        return

    user: dict[str, Any] = {
        "id": user_context.autods_user_id or user_context.sub,
        "cognito_sub": user_context.sub,
    }
    if user_context.email:
        user["email"] = user_context.email
    sentry_sdk.set_user(user)

    sentry_sdk.set_tag("cognito_sub", user_context.sub)
    if client_id:
        sentry_sdk.set_tag("client_id", client_id)
    if user_context.groups:
        sentry_sdk.set_tag("cognito_groups", ",".join(user_context.groups))


def set_request_id(request_id: str) -> None:
    """Tag the current scope with the request id so Sentry events correlate 1:1
    with the JSON access log lines (both keyed on ``request_id``)."""
    if not sentry_sdk.is_initialized():
        return
    sentry_sdk.set_tag("request_id", request_id)


def set_tool_context(*, tool_name: str, operation_id: str, arguments: dict[str, Any]) -> None:
    """Record the MCP tool call the client was making on the current scope.

    The single ``POST /mcp`` handler dispatches every tool, so the Starlette /
    FastAPI integrations can't distinguish them — this makes ``tool_name`` /
    ``operation_id`` searchable tags and stashes the validated ``arguments`` as
    context. ``arguments`` may carry sensitive values, so they're passed through
    the shared substring scrubber (see :class:`_SensitiveDataScrubber`) which
    redacts sensitive keys anywhere in the payload; per-tool redaction can be
    layered on later if a tool needs it.
    """
    if not sentry_sdk.is_initialized():
        return
    sentry_sdk.set_tag("mcp.tool", tool_name)
    sentry_sdk.set_tag("mcp.operation_id", operation_id)
    sentry_sdk.set_context(
        "mcp_tool_call",
        {"tool": tool_name, "operation_id": operation_id, "arguments": _SCRUBBER.scrub_payload(arguments)},
    )


def _upstream_context(upstream_url: str | None, upstream_status: int | None, detail: Any) -> dict[str, Any]:
    context: dict[str, Any] = {"url": upstream_url, "status": upstream_status}
    if detail is not None:
        context["detail"] = _SCRUBBER.scrub_payload(detail)
    return context


def capture_tool_exception(
    exc: BaseException,
    *,
    error_type: str,
    tool_name: str,
    upstream_url: str | None = None,
    upstream_status: int | None = None,
) -> None:
    """Capture a handled exception raised during a tool call.

    Used for the failure modes ``call_tool`` catches and turns into an
    ``isError`` envelope (upstream unreachable, internal dispatch error) — the
    automatic capture never fires because the exception doesn't escape the
    handler. Isolated on a forked scope so the extra tags don't bleed into later
    events; the user / request-id tags from the enclosing scope are inherited.
    """
    if not sentry_sdk.is_initialized():
        return
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("error_type", error_type)
        scope.set_tag("mcp.tool", tool_name)
        scope.set_context("upstream", _upstream_context(upstream_url, upstream_status, None))
        sentry_sdk.capture_exception(exc)


def capture_tool_error(
    message: str,
    *,
    error_type: str,
    tool_name: str,
    upstream_url: str | None = None,
    upstream_status: int | None = None,
    detail: Any = None,
) -> None:
    """Capture a handled upstream failure that produced no live exception.

    An upstream 5xx (or unexpected 3xx) comes back as a parsed response body, not
    a raised error, so there is nothing for ``capture_exception`` to grab — we
    send an error-level message carrying the server-side-only ``detail`` (the
    ``MappedUpstreamError.log_full`` body, deliberately not shown to the client).
    """
    if not sentry_sdk.is_initialized():
        return
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("error_type", error_type)
        scope.set_tag("mcp.tool", tool_name)
        scope.set_context("upstream", _upstream_context(upstream_url, upstream_status, detail))
        sentry_sdk.capture_message(message, level="error")
