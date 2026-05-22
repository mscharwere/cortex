"""LiteLLM HTTPX client — shared async client for all CORTEX → LiteLLM calls.

TIMEOUT RATIONALE
-----------------
``gemma4:31b`` on MS-S1 MAX has a cold-start of ~90-120 s (first request after
idle unloads the model weights).  A 5 s or 30 s default would time out before
the model finishes loading.  The connect timeout is kept short (10 s) to fail
fast on network issues; the read timeout is set to 180 s to accommodate the
full cold-start window.

HEALTH / LIVENESS
-----------------
Do NOT use LiteLLM's ``/health`` endpoint for liveness checks — it probes all
configured backends synchronously and will hang for the full timeout duration
if any backend is slow or unreachable.

Use ``GET /v1/models`` instead:
    http://ollama.perwnet.com:4000/v1/models

``/v1/models`` is fast, stateless, and returns immediately regardless of
backend availability.  Acceptable for container healthchecks, HA template
sensors, and any monitoring code that needs to confirm LiteLLM is reachable.

Usage
-----
    from cortex_python.adapters.litellm_client import get_litellm_client

    async with get_litellm_client(settings) as client:
        response = await client.post("/chat/completions", json={...})
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import httpx

from cortex_python.config.settings import Settings

# connect=10s  — fail fast on DNS / TCP issues
# read=180s    — covers gemma4:31b cold-start (~90-120 s observed)
# write=30s    — generous for large prompt payloads
# pool=5s      — don't wait long for a free connection slot
_LITELLM_TIMEOUT = httpx.Timeout(
    connect=10.0,
    read=180.0,
    write=30.0,
    pool=5.0,
)


def build_litellm_client(settings: Settings) -> httpx.AsyncClient:
    """Return a configured ``httpx.AsyncClient`` pointed at LiteLLM.

    Callers that manage the lifecycle manually (e.g. FastAPI lifespan) should
    call ``await client.aclose()`` when done.  For one-shot use, prefer the
    ``get_litellm_client`` async context-manager below.
    """
    headers: dict[str, str] = {}
    if settings.litellm_api_key:
        headers["Authorization"] = f"Bearer {settings.litellm_api_key}"

    return httpx.AsyncClient(
        base_url=settings.litellm_base_url,
        timeout=_LITELLM_TIMEOUT,
        headers=headers,
        # Follow redirects in case the proxy URL changes during deploys.
        follow_redirects=True,
    )


@asynccontextmanager
async def get_litellm_client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """Async context-manager that yields a LiteLLM client and closes it on exit.

    Example::

        async with get_litellm_client(get_settings()) as client:
            r = await client.get("/v1/models")
            r.raise_for_status()
    """
    client = build_litellm_client(settings)
    try:
        yield client
    finally:
        await client.aclose()
