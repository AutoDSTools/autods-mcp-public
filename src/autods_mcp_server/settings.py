"""Application settings loaded from environment variables.

Phase A configures the foundation. Other phases (B Cognito JWT, C OAuth
shim, D MCP runtime, E manifests) will extend this module with their own
fields, but the structure here is the contract.
"""

from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import Field, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class McpEnv(StrEnum):
    local = "local"
    staging = "staging"
    prod = "prod"


# Default manifest directory: the repo-root ``manifests/`` bundle, resolved from
# this file's location so it works regardless of the process CWD. Phase E owns
# the production manifest set; Phase D ships the vendored ``products.json`` here.
_DEFAULT_MANIFEST_DIR = Path(__file__).resolve().parents[2] / "manifests"

# ``base_url_key`` (manifest/operation field) -> the Settings attribute holding
# that upstream's base URL. The dispatcher resolves routing through this map, so
# adding an upstream is: add the URL field below + an entry here.
_BASE_URL_KEY_TO_ATTR: dict[str, str] = {
    "autods_api": "autods_api_base_url",
    "products_research": "products_research_base_url",
}


# Origins shared by every non-local environment. Staging additionally
# accepts dev/inspector clients; prod is the narrow set below.
_PROD_ORIGINS: tuple[str, ...] = (
    "https://claude.com",
    "https://claude.ai",
    "https://app.cursor.com",
)
_STAGING_EXTRA_ORIGINS: tuple[str, ...] = ("https://inspector.modelcontextprotocol.io",)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    mcp_env: McpEnv = Field(validation_alias="MCP_ENV")

    autods_api_base_url: str = Field(
        default="https://api.autods.com",
        validation_alias="AUTODS_API_BASE_URL",
    )
    products_research_base_url: str = Field(
        default="https://products-research.autods.com",
        validation_alias="PRODUCTS_RESEARCH_BASE_URL",
    )

    cognito_user_pool_id: str = Field(validation_alias="COGNITO_USER_POOL_ID")
    cognito_region: str = Field(default="us-west-2", validation_alias="COGNITO_REGION")
    allowed_cognito_client_ids: list[str] = Field(
        default_factory=list,
        validation_alias="ALLOWED_COGNITO_CLIENT_IDS",
    )

    # Cognito Hosted UI domain — used to build authorize/token endpoints
    # in the AS metadata document. Typically
    # "<prefix>.auth.<region>.amazoncognito.com" or a custom-domain CNAME.
    # Bare hostname OR full URL; we normalise below. Required in every
    # environment — the discovery surface is dead without it.
    cognito_domain: str = Field(validation_alias="COGNITO_DOMAIN")

    # The pre-created public Cognito client_id the DCR shim returns to MCP
    # clients. Required in every environment, and must also be in
    # allowed_cognito_client_ids (validated below) so tokens minted via this
    # flow actually authenticate against us.
    cognito_public_client_id: str = Field(validation_alias="COGNITO_PUBLIC_CLIENT_ID")

    # OAuth scopes published in PRM + AS metadata and requested by clients.
    mcp_oauth_scopes: list[str] = Field(
        default_factory=lambda: [
            "email",
            "openid",
            "phone",
            "profile",
        ],
        validation_alias="MCP_OAUTH_SCOPES",
    )

    # Allowlist of redirect URIs the DCR shim accepts. Must mirror the URIs
    # pre-registered on the Cognito public client — registering a URI not
    # in Cognito's list would let the authorize step fail downstream.
    mcp_registration_redirect_uris: list[str] = Field(
        default_factory=list,
        validation_alias="MCP_REGISTRATION_REDIRECT_URIS",
    )

    # Override of computed allowed origins. Comma-separated string, JSON
    # list, or list[str] — pydantic-settings handles the parsing.
    allowed_origins_override: list[str] = Field(
        default_factory=list,
        validation_alias="ALLOWED_ORIGINS",
    )

    # Public hostname the server is reachable on (used by the Origin
    # middleware for DNS-rebinding defense). Empty in local disables the
    # Host check.
    public_hostname: str | None = Field(default=None, validation_alias="PUBLIC_HOSTNAME")

    # ALB terminates TLS in staging/prod and forwards plaintext to the
    # container. We refuse to boot non-local without this acknowledgment.
    force_https: bool = Field(default=False, validation_alias="FORCE_HTTPS")

    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    # Redis connection URL (e.g. ``redis://host:6379/0`` or ``rediss://`` for
    # TLS; auth is carried in the URL userinfo). Backs the per-user rate
    # limiter (F1) so the limit is enforced across all replicas, not
    # per-process. Required in staging/prod (validated below); optional in
    # local, where an unset value falls back to an in-process limiter.
    redis_url: str | None = Field(default=None, validation_alias="REDIS_URL")

    # Per-user rate-limit ceilings (token buckets keyed by ``user.sub``). Both
    # apply simultaneously — a call is allowed only if it fits under both.
    rate_limit_per_minute: int = Field(default=60, validation_alias="RATE_LIMIT_PER_MINUTE")
    rate_limit_per_hour: int = Field(default=1000, validation_alias="RATE_LIMIT_PER_HOUR")

    # Directory the MCP runtime loads tool manifests from. Defaults to the
    # bundled ``manifests/`` (which carries the vendored products manifest);
    # point it at an empty dir to run the transport with zero tools.
    mcp_manifest_dir: Path = Field(default=_DEFAULT_MANIFEST_DIR, validation_alias="MCP_MANIFEST_DIR")

    # Mixpanel project token. Unset → the analytics integration is a no-op (the
    # local default), so the server boots and runs with tracking disabled and no
    # token required. Set in staging/prod to emit the auth + tool-call events.
    mixpanel_token: str | None = Field(default=None, validation_alias="MIXPANEL_TOKEN")

    # TTL for *negative* entries in the Cognito attribute cache (a user whose
    # ``custom:autods_user_id`` couldn't be resolved, or a transient lookup
    # failure). Negatives expire so not-yet-backfilled users and transient errors
    # get retried. Default 6h.
    cognito_attr_negative_cache_ttl_seconds: int = Field(
        default=21600,
        validation_alias="COGNITO_ATTR_NEGATIVE_CACHE_TTL_SECONDS",
    )

    # TTL for *positive* entries in the Cognito attribute cache. The
    # cognito-subject → autods_user_id mapping is immutable, but the cached
    # ``email`` is not (a user can change it), so positives expire and refresh
    # rather than being cached forever — bounding how stale a logged email can
    # be. Default 24h.
    cognito_attr_positive_cache_ttl_seconds: int = Field(
        default=86400,
        validation_alias="COGNITO_ATTR_POSITIVE_CACHE_TTL_SECONDS",
    )

    # Self-hosted Sentry DSN (``https://<key>@sentry.autods.com/<id>``),
    # delivered via External Secrets in staging/prod and unset locally. Kept
    # deliberately optional — no boot-time validator — so a missing DSN (or a
    # local run) makes ``init_sentry`` a no-op instead of failing startup.
    sentry_url: str | None = Field(default=None, validation_alias="SENTRY_URL")
    # Sentry environment tag (prod/staging). Defaults to ``mcp_env`` when unset
    # (see the validator below); staging/prod set it explicitly via the chart.
    # The release is derived from ``__version__`` in ``init_sentry`` — not an env
    # var — so it always matches the deployed code.
    sentry_environment: str | None = Field(default=None, validation_alias="SENTRY_ENVIRONMENT")

    def upstream_base_url(self, base_url_key: str) -> str:
        """Resolve a manifest ``base_url_key`` to the upstream's base URL (D6).

        Raises:
            ValueError: if the key isn't a known upstream — a manifest
                packaging error we surface rather than silently mis-route.
        """
        attr = _BASE_URL_KEY_TO_ATTR.get(base_url_key)
        if attr is None:
            raise ValueError(f"Unknown base_url_key {base_url_key!r}; expected one of {sorted(_BASE_URL_KEY_TO_ATTR)}.")
        return getattr(self, attr)

    @computed_field
    @property
    def allowed_origins(self) -> list[str]:
        """Origins this server accepts on /mcp + /.well-known/* routes.

        Local env: any http://localhost:* (matched via the glob below).
        Staging: prod set + inspector/dev tooling.
        Prod: prod set only.
        """
        if self.allowed_origins_override:
            return list(self.allowed_origins_override)

        match self.mcp_env:
            case McpEnv.local:
                return ["http://localhost:*", "http://127.0.0.1:*"]
            case McpEnv.staging:
                return [*_PROD_ORIGINS, *_STAGING_EXTRA_ORIGINS]
            case McpEnv.prod:
                return list(_PROD_ORIGINS)

    @computed_field
    @property
    def is_local(self) -> bool:
        return self.mcp_env == McpEnv.local

    @computed_field
    @property
    def cognito_issuer(self) -> str:
        """Token `iss` claim Cognito mints for this user pool."""
        return f"https://cognito-idp.{self.cognito_region}.amazonaws.com/{self.cognito_user_pool_id}"

    @computed_field
    @property
    def cognito_jwks_url(self) -> str:
        """Public JWKS URL Cognito publishes for this user pool."""
        return f"{self.cognito_issuer}/.well-known/jwks.json"

    @computed_field
    @property
    def cognito_hosted_ui_base_url(self) -> str:
        """Normalised Cognito Hosted UI base URL (no trailing slash).

        Accepts both bare hostnames ("autods.auth.us-west-2.amazoncognito.com")
        and full URLs. ``cognito_domain`` is required, so this always resolves.
        """
        domain = self.cognito_domain.strip().rstrip("/")
        if "://" not in domain:
            domain = f"https://{domain}"
        return domain

    @computed_field
    @property
    def cognito_authorization_endpoint(self) -> str:
        return f"{self.cognito_hosted_ui_base_url}/oauth2/authorize"

    @computed_field
    @property
    def cognito_token_endpoint(self) -> str:
        return f"{self.cognito_hosted_ui_base_url}/oauth2/token"

    @model_validator(mode="after")
    def _require_force_https_in_non_local(self) -> Self:
        """A5 startup check: refuse to boot in staging/prod without FORCE_HTTPS=true.

        This signals the operator understands ALB terminates TLS and that
        X-Forwarded-Proto is trusted for the request-level guard.
        """
        if self.mcp_env != McpEnv.local and not self.force_https:
            raise ValueError(
                f"MCP_ENV={self.mcp_env.value} requires FORCE_HTTPS=true "
                "(ALB terminates TLS; X-Forwarded-Proto is trusted)."
            )
        return self

    @model_validator(mode="after")
    def _require_public_hostname_in_non_local(self) -> Self:
        """A4 startup check: refuse to boot in staging/prod without PUBLIC_HOSTNAME specified."""
        if self.mcp_env != McpEnv.local and not self.public_hostname:
            raise ValueError(f"MCP_ENV={self.mcp_env.value} requires PUBLIC_HOSTNAME value")
        return self

    @model_validator(mode="after")
    def _require_redis_url_in_non_local(self) -> Self:
        """F0 startup check: staging/prod run multiple replicas, so the rate
        limiter's state must live in a shared Redis — an in-process limiter
        would enforce the ceiling per-replica, not per-user cluster-wide.

        Local may omit ``REDIS_URL`` and fall back to the in-process limiter.
        """
        if self.mcp_env != McpEnv.local and not self.redis_url:
            raise ValueError(
                f"MCP_ENV={self.mcp_env.value} requires REDIS_URL "
                "(the rate limiter is shared across replicas via Redis)."
            )
        return self

    @model_validator(mode="after")
    def _default_sentry_environment(self) -> Self:
        """RD-66: default the Sentry environment tag to the deployment env.

        When ``SENTRY_ENVIRONMENT`` isn't set explicitly (staging/prod set it via
        the chart), fall back to ``mcp_env`` so any event carries a meaningful
        environment rather than an empty string.
        """
        if not self.sentry_environment:
            self.sentry_environment = self.mcp_env.value
        return self

    @model_validator(mode="after")
    def _public_client_id_must_be_allowed(self) -> Self:
        """The DCR shim's public client_id must be among the JWT-acceptance set.

        Otherwise tokens minted via the public OAuth flow would fail
        verification (silent misconfiguration that only surfaces on first
        end-to-end login attempt).
        """
        if self.cognito_public_client_id not in self.allowed_cognito_client_ids:
            raise ValueError(
                f"COGNITO_PUBLIC_CLIENT_ID={self.cognito_public_client_id!r} is not in "
                "ALLOWED_COGNITO_CLIENT_IDS; tokens minted via this client would be rejected."
            )
        return self


_settings: Settings | None = None


def get_settings() -> Settings:
    """Lazy singleton so tests can monkeypatch env before instantiation."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings


def reset_settings_cache() -> None:
    """Test hook: drop the cached Settings so the next get_settings() re-reads env."""
    global _settings
    _settings = None
