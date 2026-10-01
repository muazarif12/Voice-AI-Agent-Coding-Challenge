# syntax=docker/dockerfile:1
# One image that runs both the REST API and the voice worker (see start.sh).
ARG PYTHON_VERSION=3.13
FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS base
ENV PYTHONUNBUFFERED=1
# Model files downloaded at build time are kept inside /app, so the runtime stage (which
# copies only /app) still has them and the worker never downloads anything at startup.
ENV HF_HOME=/app/.cache/huggingface

# --- Build stage: install dependencies and pre-download model files ---
FROM base AS build

# Compilers for dependencies that build native extensions; left out of the final image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so this layer stays cached when only the code changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# Then the source. .dockerignore keeps secrets, local databases and the host .venv out.
COPY . .
RUN uv sync --locked --no-dev

# Voice-activity, turn-detection and noise-cancellation models.
RUN uv run --no-sync python -m livekit.agents download-files

# --- Runtime stage: same base, without the compilers ---
FROM base
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH"

# Runs as root because Fly mounts the data volume (/data, see fly.toml) owned by root, and
# the SQLite database there must be writable.
EXPOSE 8000
CMD ["bash", "start.sh"]
