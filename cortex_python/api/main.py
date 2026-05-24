"""CORTEX-Python FastAPI application.

Phase 0 Item 1 — minimum-viable service exposing ``/health`` and ``/version``.
Adapter routes, ingest webhooks, persona endpoints, and the AIT overflow
surface land in later Phase 0 / Phase 1 items.

Phase 1 — VacuumOps loop is registered as an asyncio background task in the
FastAPI lifespan (§8, cortex_vacuumops_module_spec.md).

Spec: ``C:/Jarvis/Team/TARS/cortex_architecture.md`` (v3.1).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import metadata

import structlog
from fastapi import FastAPI

from cortex_python import __version__
from cortex_python.config.settings import get_settings

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan — start background tasks on startup, cancel on shutdown.

    VacuumOps adaptive polling loop is registered here as a background task.
    It runs forever; exceptions inside ticks are caught at the loop level
    (the loop never dies). Lifespan cancels it on shutdown.
    """
    # Import lazily to avoid pulling VacuumOps deps into import-time scope
    from cortex_python.modules.vacuumops.loop import vacuumops_loop

    settings = get_settings()

    # Start VacuumOps loop as background task
    task = asyncio.create_task(
        vacuumops_loop(settings),
        name="vacuumops_loop",
    )
    log.info("vacuumops_loop.started", dry_run=settings.cortex_vacuumops_dry_run)

    yield  # application is running

    # Shutdown — cancel the loop task gracefully
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        log.info("vacuumops_loop.stopped")


app = FastAPI(
    title="CORTEX-Python",
    description="Cross-App Intelligence Fabric runtime (NAS brain).",
    version=__version__,
    lifespan=lifespan,
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
