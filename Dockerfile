# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# uv lives in /usr/local/bin/uv after this stage.
#
# Floor is 0.6.0: the project's own `uv.lock` entry carries no `version` field
# now that the version is dynamic (see pyproject), and 0.5.x rejects the whole
# lock with `missing field version`. Verified — 0.5.11 fails, 0.6.0 parses it.
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (no project source) for cache efficiency.
COPY pyproject.toml uv.lock* ./
# `--frozen` with no fallback: a lock that doesn't match pyproject must fail the
# build, not silently re-resolve to whatever is newest on PyPI. The old
# `|| uv sync ...` fallback never actually rescued anything — when the lock was
# unreadable both branches failed alike — it only stood ready to ship an image
# built from an unpinned resolve.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Now copy the project source and install the package itself.
COPY src ./src
COPY manifests ./manifests
COPY README.md ./README.md
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

EXPOSE 8000

ENV PATH="/app/.venv/bin:${PATH}"

# --timeout-graceful-shutdown (F5): on SIGTERM uvicorn stops accepting new
# connections and waits up to this many seconds for in-flight tool calls to
# finish before forcing exit. Keep ≤ Kubernetes terminationGracePeriodSeconds.
CMD ["uvicorn", "--factory", "autods_mcp_server.app:create_app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "30"]
