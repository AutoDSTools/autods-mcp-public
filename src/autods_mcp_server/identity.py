"""Self-identity resolution (RD-68).

Resolves the **caller's own** AutoDS identity — user id, name, email — by calling
the AutoDSApi ``GET /users/list/`` operation with the caller's own forwarded
bearer token. Despite its name, that endpoint returns just the authenticated
user (a single-element list), so it acts as a "get me" lookup that needs no
privileged credentials: the server simply reuses the token it already forwards.

This is the identity source other features build on (e.g. analytics / log
enrichment). It reuses the :class:`OperationDispatcher` — same token forwarding,
upstream routing, and response parsing as any tool call — and **fails open**:
any dispatch error, non-2xx response, or malformed payload resolves to ``None``
and is logged, never raised, so identity resolution can never break auth or a
tool call.
"""

from dataclasses import dataclass
from typing import Any

from autods_mcp_server.auth import UserContext
from autods_mcp_server.dispatch import DispatchError, OperationDispatcher
from autods_mcp_server.logging import get_logger

_logger = get_logger("autods_mcp_server.identity")

# The manifest operation id for AutoDSApi ``GET /users/list/`` (see
# ``manifests/users.json``) — the "get me" lookup this resolver dispatches.
SELF_IDENTITY_OPERATION_ID = "get_current_user"


@dataclass(frozen=True)
class SelfIdentity:
    """The caller's own identity, as resolved from AutoDSApi."""

    user_id: str
    name: str | None = None
    email: str | None = None


class SelfIdentityResolver:
    """Resolve the caller's own ``user_id`` / ``name`` / ``email`` from AutoDSApi."""

    def __init__(
        self,
        dispatcher: OperationDispatcher,
        *,
        operation_id: str = SELF_IDENTITY_OPERATION_ID,
    ) -> None:
        self._dispatcher = dispatcher
        self._operation_id = operation_id

    async def resolve(self, user_context: UserContext) -> SelfIdentity | None:
        """Resolve ``user_context``'s own identity, or ``None`` (fail-open).

        Dispatches the ``get_current_user`` operation with the caller's forwarded
        token. **Never raises**: a dispatch failure, a non-2xx upstream response
        (e.g. a blocked account), or a payload we can't parse all degrade to
        ``None`` rather than propagating into the request path.
        """
        try:
            result = await self._dispatcher.dispatch(self._operation_id, {}, user_context)
        except DispatchError as exc:
            _logger.warning("self_identity_dispatch_failed", error=str(exc))
            return None
        except Exception as exc:  # noqa: BLE001 — resolution must never raise into auth/tool calls.
            _logger.warning("self_identity_unexpected_error", error=str(exc))
            return None

        if not result.ok:
            # A non-2xx (auth/permission/upstream) — don't surface it, just skip.
            _logger.warning("self_identity_lookup_non_2xx", status=result.status)
            return None

        return self._parse(result.data)

    @staticmethod
    def _parse(data: Any) -> SelfIdentity | None:
        """Extract the identity from the upstream payload (fail-open on shape).

        ``GET /users/list/`` returns the caller as a single-element list —
        ``[{"id": ..., "name": ..., "email": ...}]`` — but we also accept a bare
        object defensively. Anything else (empty, missing id, wrong type) yields
        ``None``.
        """
        record: Any = None
        if isinstance(data, list):
            record = data[0] if data else None
        elif isinstance(data, dict):
            record = data
        if not isinstance(record, dict):
            return None

        raw_id = record.get("id")
        if raw_id is None:
            return None

        return SelfIdentity(
            user_id=str(raw_id),
            name=record.get("name"),
            email=record.get("email"),
        )
