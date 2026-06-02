.PHONY: install run test lint fmt

install:
	uv sync

run:
	uv run uvicorn --factory autods_mcp_server.app:create_app --reload

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .
