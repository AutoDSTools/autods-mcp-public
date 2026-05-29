# autods-mcp-server

Public MCP (Model Context Protocol) server for AutoDS. Exposes selected
AutoDSApi and ProductsResearch tooling to MCP-compatible clients
(Claude, Cursor, etc.).

This repo is the foundation for the Public MCP epic
([RD-50](https://autods.atlassian.net/browse/RD-50)). Phase A
([RD-51](https://autods.atlassian.net/browse/RD-51)) set up the repo
skeleton, the FastAPI app, structured logging, origin / HTTPS-only
middlewares and the local Docker workflow. Phase B
([RD-52](https://autods.atlassian.net/browse/RD-52)) adds the Cognito
JWT verification stack. MCP/OAuth transport lands in later phases.

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/) (dependency / virtualenv manager)
- Docker + docker-compose (for the local container workflow)

## Local development

First, copy the env template and fill in real values:

```bash
cp .env.example .env
# edit .env with your Cognito pool/region/client IDs
```

Then either:

```bash
uv sync
uv run uvicorn autods_mcp_server.app:app --reload
curl http://localhost:8000/health   # {"status":"ok"}
```

### Via Docker

```bash
docker compose up
curl http://localhost:8000/health
```

`docker-compose.yml` reads `.env` via `env_file`, so the same values
used by `uv run` are picked up by the container. Source is bind-mounted
into the container, so edits hot-reload.

## Configuration

All settings come from environment variables. See
`src/autods_mcp_server/settings.py` for the full schema. Key knobs:

| Variable | Required | Notes |
|---|---|---|
| `MCP_ENV` | yes | `local` / `staging` / `prod` |
| `COGNITO_USER_POOL_ID` | yes | Cognito user pool that mints tokens |
| `COGNITO_REGION` | no (default `us-west-2`) | Used to compute the JWKS URL and `iss` claim |
| `ALLOWED_COGNITO_CLIENT_IDS` | yes (JSON list) | Access tokens are accepted only if their `client_id` is in this list |
| `FORCE_HTTPS` | yes in non-local | Set to `true` to acknowledge ALB-terminated TLS |
| `PUBLIC_HOSTNAME` | yes in non-local | Pins the PRM URL host (defends against `Host` / `X-Forwarded-Host` injection) |
| `LOG_LEVEL` | no (default `INFO`) | |

Non-local environments enforce HTTPS via `X-Forwarded-Proto` and refuse
to boot without `FORCE_HTTPS=true` and `PUBLIC_HOSTNAME` set.

## Authentication (Phase B)

The server is an OAuth resource server. Protected routes depend on
`autods_mcp_server.auth.get_current_user`, which:

1. Extracts `Authorization: Bearer <token>`.
2. Verifies the JWT against the JWKS document published by the
   configured Cognito user pool — signature (RS256), `iss`, `exp` are
   checked by PyJWT; `client_id` is checked manually against
   `ALLOWED_COGNITO_CLIENT_IDS` because Cognito access tokens carry
   `client_id` instead of `aud`.
3. Returns a `UserContext` with `sub`, `email`, `groups`, and the raw
   token wrapped in `pydantic.SecretStr`.

On failure, the response is HTTP 401 with an RFC 6750-compliant
`WWW-Authenticate: Bearer resource_metadata="<url>"` challenge that
points clients at the (future, Phase C/C2) protected-resource metadata
document.

If Cognito itself is unreachable or returns a malformed JWKS, the
response is HTTP 503 instead — clients should retry rather than
re-authenticate.

The `JWKSClient` caches keys for `ttl_seconds` (default 24h), refreshes
on unknown-`kid`, and rate-limits all refresh attempts (success or
failure) to one per `min_refresh_interval` (default 30s) to bound both
unknown-kid amplification and Cognito-outage amplification.

## Lint / format / test

```bash
uv run ruff check .
uv run ruff format .
uv run pytest
```

Coverage (line + branch, with missing-line report):

```bash
uv run --with pytest-cov pytest --cov=src/autods_mcp_server --cov-report=term-missing --cov-branch
```
