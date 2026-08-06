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
# RD-82 spike (TEMPORARY — revert with the rest of the probe commit). The probe
# module is imported only when MCP_IMAGE_PROBE=true; without this COPY the
# flag-on path would die at boot with ModuleNotFoundError.
COPY spike ./spike
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev

EXPOSE 8000

ENV PATH="/app/.venv/bin:${PATH}"
# RD-82 spike (TEMPORARY): /app is not implicitly on sys.path under the uvicorn
# console script, so `import spike.probe_extension` needs it explicitly.
ENV PYTHONPATH=/app

# --timeout-graceful-shutdown (F5): on SIGTERM uvicorn stops accepting new
# connections and waits up to this many seconds for in-flight tool calls to
# finish before forcing exit. Keep ≤ Kubernetes terminationGracePeriodSeconds.
CMD ["uvicorn", "--factory", "autods_mcp_server.app:create_app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "30"]
