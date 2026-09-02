.PHONY: install run test release-checks release-checks-c lint fmt

install:
	uv sync

run:
	uv run uvicorn --factory autods_mcp_server.app:create_app --reload

test:
	uv run pytest

# docs/release-checks.md section S against a deployed build (staging by
# default; override with MCP_RELEASE_BASE_URL). Read-only and safe anywhere;
# no credentials needed — section S is the unauthenticated surface.
release-checks:
	RUN_RELEASE_CHECKS=1 uv run pytest tests/e2e/test_release_checks_s.py -v -rs

# docs/release-checks.md section C — the authenticated handshake payload, diffed
# against what this checkout's manifests build. Needs a token (MCP_TOKEN, or the
# E2E_COGNITO_* password grant) but no fixtures. Read-only and safe anywhere.
# Run it from the *released* commit: from a different one the diff reports the
# checkout's own manifests as drift.
release-checks-c:
	RUN_RELEASE_CHECKS=1 uv run pytest tests/e2e/test_release_checks_c.py -v -rs

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .
