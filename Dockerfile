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

WORKDIR /app

# Copy the entire venv from the builder
COPY --from=builder /app/.venv .venv/

# Copy application code and data
COPY --from=builder /app/src/ src/
COPY --from=builder /app/scripts/ scripts/
COPY --from=builder /app/runbooks/ runbooks/

# Put the venv on PATH so `python`, `uvicorn`, `streamlit` resolve from it
ENV PATH="/app/.venv/bin:$PATH"

# Claude Agent SDK: the bundled CLI binary needs to be executable.
# The pip wheel includes it at _bundled/claude inside the package.
# Ensure it has execute permission (some container runtimes strip it).
RUN chmod +x .venv/lib/python*/site-packages/claude_agent_sdk/_bundled/claude 2>/dev/null || true

EXPOSE 8000 8501

# Default: run the FastAPI API server
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
