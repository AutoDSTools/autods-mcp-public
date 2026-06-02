"""Operation dispatcher (D4) with multi-source upstream routing (D6).

Given an ``operation_id``, the validated tool ``arguments``, and the
authenticated :class:`UserContext`, the dispatcher:

1. looks up the manifest operation in the registry,
2. resolves the upstream base URL from its ``base_url_key`` via ``Settings``,
3. builds the upstream request — path-param substitution, query params, header
   params, and the JSON body,
4. forwards the caller's ``Authorization: Bearer ...`` so the upstream applies
   the user's own permissions (the public server never holds privileged creds),
5. returns a structured envelope the MCP tool call serialises back to the client.

Input shape is already validated against the tool's ``inputSchema`` by the MCP
SDK before we're called; the parameter/body checks here are defensive so the
dispatcher is safe to call directly (e.g. from tests) too.
"""

from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from autods_mcp_server.auth import UserContext
from autods_mcp_server.manifests import ManifestRegistry
from autods_mcp_server.manifests.schema import ManifestOperation
from autods_mcp_server.settings import Settings

# Default per-request timeout for upstream calls. Bulk operations on AutoDSApi
# can be slow; the value is generous but bounded so a hung upstream can't pin a
# worker indefinitely.
_DEFAULT_TIMEOUT_SECONDS = 30.0


class DispatchError(Exception):
    """Base class for dispatch failures surfaced as MCP tool errors."""


class UnknownOperationError(DispatchError):
    """No manifest operation matches the requested ``operation_id``."""


class MissingArgumentError(DispatchError):
    """A required path parameter or request body was absent from ``arguments``."""


class UpstreamRequestError(DispatchError):
    """The upstream call failed at the transport level (timeout, connection,
    redirect-limit, etc.) before any HTTP response was received.

    A non-2xx *response* is not an error — it travels back in the
    :class:`DispatchResult` envelope. This is only raised when ``httpx`` could
    not complete the round-trip at all, so the transport surfaces a clean MCP
    tool error instead of an unhandled exception escaping ``call_tool``.
    """


class DispatchResult(BaseModel):
    """Structured envelope returned for every tool call.

    ``ok`` mirrors the upstream's 2xx-ness; ``data`` is the parsed JSON body
    (or raw text when the upstream didn't return JSON). The envelope shape is
    stable so clients can branch on ``ok``/``status`` without parsing prose.
    """

    operation_id: str
    status: int
    ok: bool
    data: Any = None


class OperationDispatcher:
    """Forwards MCP tool calls to the right upstream REST operation."""

    def __init__(
        self,
        registry: ManifestRegistry,
        settings: Settings,
        client: httpx.AsyncClient,
    ) -> None:
        self._registry = registry
        self._settings = settings
        self._client = client

    def _build_request(
        self,
        operation: ManifestOperation,
        arguments: dict[str, Any],
        user_context: UserContext,
    ) -> httpx.Request:
        base_url = self._settings.upstream_base_url(operation.base_url_key or "autods_api")

        path = operation.path
        query: dict[str, str] = {}
        # The caller's own bearer token — never the server's. ``accept`` nudges
        # upstreams toward JSON; per-operation header params may override it.
        headers: dict[str, str] = {
            "authorization": f"Bearer {user_context.raw_token.get_secret_value()}",
            "accept": "application/json",
        }

        for parameter in operation.parameters:
            value = arguments.get(parameter.name)
            if value is None:
                if parameter.required:
                    raise MissingArgumentError(
                        f"Operation '{operation.operation_id}' requires parameter '{parameter.name}'."
                    )
                continue
            if parameter.location == "path":
                # Encode the segment but keep it a single path component.
                path = path.replace(f"{{{parameter.name}}}", quote(str(value), safe=""))
            elif parameter.location == "query":
                query[parameter.name] = str(value)
            else:  # header
                headers[parameter.name] = str(value)

        body = arguments.get("body")
        if operation.request_body_required and body is None:
            raise MissingArgumentError(f"Operation '{operation.operation_id}' requires a request body.")

        url = f"{base_url.rstrip('/')}{path}"
        json_body = body if (operation.has_json_body and body is not None) else None
        return self._client.build_request(
            operation.method.upper(),
            url,
            params=query or None,
            json=json_body,
            headers=headers,
        )

    @staticmethod
    def _parse_response(response: httpx.Response) -> Any:
        """Return parsed JSON when possible, else the raw text body."""
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                return response.json()
            except ValueError:
                pass
        return response.text or None

    async def dispatch(
        self,
        operation_id: str,
        arguments: dict[str, Any],
        user_context: UserContext,
    ) -> DispatchResult:
        """Execute one upstream operation on behalf of the authenticated user.

        Raises:
            UnknownOperationError: ``operation_id`` is not registered.
            MissingArgumentError: a required path param / body is missing.
            UpstreamRequestError: the upstream round-trip failed (timeout,
                connection error, …) before a response was received.
        """
        operation = self._registry.get(operation_id)
        if operation is None:
            raise UnknownOperationError(f"Unknown operation_id '{operation_id}'.")

        request = self._build_request(operation, arguments, user_context)
        try:
            response = await self._client.send(request)
        except httpx.HTTPError as exc:
            raise UpstreamRequestError(
                f"Upstream request for operation '{operation_id}' failed: {exc}"
            ) from exc
        return DispatchResult(
            operation_id=operation_id,
            status=response.status_code,
            ok=response.is_success,
            data=self._parse_response(response),
        )


def create_http_client(timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS) -> httpx.AsyncClient:
    """Construct the shared upstream HTTP client.

    ``follow_redirects=False`` keeps the forwarded bearer token from leaking to
    an unexpected host on a 3xx redirect.
    """
    return httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)
