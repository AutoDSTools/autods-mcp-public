# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# uv lives in /usr/local/bin/uv after this stage.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (no project source) for cache efficiency.
COPY pyproject.toml uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev || \
    uv sync --no-install-project --no-dev

# Now copy the project source and install the package itself.
COPY src ./src
COPY manifests ./manifests
COPY README.md ./README.md
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev

EXPOSE 8000

ENV PATH="/app/.venv/bin:${PATH}"

CMD ["uvicorn", "--factory", "autods_mcp_server.app:create_app", "--host", "0.0.0.0", "--port", "8000"]
