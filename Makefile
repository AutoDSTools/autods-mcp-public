.PHONY: install run test release-checks lint fmt

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

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .
