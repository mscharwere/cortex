"""CORTEX-Python FastAPI application.

Phase 0 Item 1 — minimum-viable service exposing ``/health`` and ``/version``.
Adapter routes, ingest webhooks, persona endpoints, and the AIT overflow
surface land in later Phase 0 / Phase 1 items.

Spec: ``C:/Jarvis/Team/TARS/cortex_architecture.md`` (v3.1).
"""

from __future__ import annotations

from importlib import metadata

from fastapi import FastAPI

from cortex_python import __version__

app = FastAPI(
    title="CORTEX-Python",
    description="Cross-App Intelligence Fabric runtime (NAS brain).",
    version=__version__,
)


def _resolve_version() -> str:
    """Return the installed package version, falling back to module constant."""
    try:
        return metadata.version("cortex-python")
    except metadata.PackageNotFoundError:
        return __version__


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe used by Docker healthcheck + ``sensor.cortex_*``."""
    return {"status": "ok"}


@app.get("/version")
async def version() -> dict[str, str]:
    """Build/version surface for debugging + briefings."""
    return {"version": _resolve_version()}
