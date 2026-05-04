# Stage 1: builder — install dependencies into a venv
FROM python:3.13-slim AS builder

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files first (layer caching)
COPY pyproject.toml uv.lock ./

# Create venv and install production dependencies only (no dev group)
RUN uv sync --frozen --no-dev --no-install-project

# Copy source code and scripts for the project install
COPY src/ src/
COPY scripts/ scripts/
COPY runbooks/ runbooks/

# Install the project itself into the venv
RUN uv sync --frozen --no-dev


# Stage 2: runtime — slim image with just the venv + source
FROM python:3.13-slim

# Non-root user. uid/gid 1001 matches the ownership expected on the host-mounted
# chroma_data, conversations, and .claude volumes. Both the API server and the
# `make ingest` setup container run from this image, so baking the user in keeps
# files written by ingest readable+writable by the runtime (chromadb 1.x opens
# the sqlite read-write even on read paths, so a uid mismatch breaks RAG).
RUN groupadd --system --gid 1001 app \
 && useradd --system --uid 1001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

# Copy the entire venv from the builder, owned by the app user
COPY --from=builder --chown=1001:1001 /app/.venv .venv/

# Copy application code and data, owned by the app user
COPY --from=builder --chown=1001:1001 /app/src/ src/
COPY --from=builder --chown=1001:1001 /app/scripts/ scripts/
COPY --from=builder --chown=1001:1001 /app/runbooks/ runbooks/

# Ensure /app itself is owned by the app user (mount points and HOME=/app)
RUN chown 1001:1001 /app

# Put the venv on PATH so `python`, `uvicorn` resolve from it
ENV PATH="/app/.venv/bin:$PATH"

# Claude Agent SDK: the bundled CLI binary needs to be executable.
# The pip wheel includes it at _bundled/claude inside the package.
# Ensure it has execute permission (some container runtimes strip it).
RUN chmod +x .venv/lib/python*/site-packages/claude_agent_sdk/_bundled/claude 2>/dev/null || true

USER 1001:1001

EXPOSE 8000

# Default: run the FastAPI API server
# Single worker: SDK query() is async (subprocess-based) so one event loop handles
# concurrent requests fine.  Multiple workers would break the asyncio.Lock on OAuth
# refresh and double memory usage for no throughput benefit.
# --timeout-keep-alive prevents idle connections from being dropped mid-response.
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "130"]
