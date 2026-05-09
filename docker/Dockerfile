# CORTEX-Python — Phase 0 Item 1
#
# Multi-stage build mirrors the family-app pattern (familyOps, familyAuth):
#   builder stage installs deps + builds wheel; runtime stage copies the
#   built artifact and runs uvicorn. Python 3.13 slim base instead of
#   node:20-alpine.
#
# Build context = repo root (so we can COPY cortex_python/ + pyproject).
# See docker/docker-compose.yml.

# ---------- Stage 1: builder ----------
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install build deps minimal — slim image already has what we need for
# pure-Python wheels (FastAPI, Pydantic v2, httpx, uvicorn).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy package metadata first for better layer caching.
COPY cortex_python/pyproject.toml ./cortex_python/pyproject.toml
COPY cortex_python ./cortex_python

# Build the wheel into /build/dist.
RUN pip install --upgrade pip build \
    && cd cortex_python \
    && python -m build --wheel --outdir /build/dist

# ---------- Stage 2: runtime ----------
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/Los_Angeles

# tzdata so TZ resolves; wget for the healthcheck (mirrors familyOps pattern).
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata \
        wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the wheel built in stage 1.
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install /tmp/*.whl && rm -f /tmp/*.whl

EXPOSE 8000

# uvicorn runs the FastAPI app declared in cortex_python/api/main.py.
# Eventually migrations + adapters bootstrap will live in an entrypoint
# script (Item 2 owns MariaDB migrations); for now, just serve.
CMD ["uvicorn", "cortex_python.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
