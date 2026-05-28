"""Application settings loaded from environment variables.

Phase A configures the foundation. Other phases (B Cognito JWT, C OAuth
shim, D MCP runtime, E manifests) will extend this module with their own
fields, but the structure here is the contract.
"""

from enum import StrEnum
from typing import Self

from pydantic import Field, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class McpEnv(StrEnum):
    local = "local"
    staging = "staging"
    prod = "prod"


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

    cognito_user_pool_id: str = Field(default="", validation_alias="COGNITO_USER_POOL_ID")
    cognito_region: str = Field(default="us-west-2", validation_alias="COGNITO_REGION")
    allowed_cognito_client_ids: list[str] = Field(
        default_factory=list,
        validation_alias="ALLOWED_COGNITO_CLIENT_IDS",
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
