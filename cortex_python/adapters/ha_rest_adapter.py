"""HA REST adapter for CORTEX VacuumOps.

Phase 1: REST polling. WebSocket subscription upgrade is Phase 2.

Fetches HA entity states via GET /api/states/{entity_id} and calendar events
via GET /api/calendars/{entity_id}?start=...&end=... on each tick.

Also publishes the loop status sensor:
  POST /api/states/sensor.cortex_vacuumops_loop_status

All calls use:
  Authorization: Bearer {settings.homeassistant_token}
  Base URL: settings.homeassistant_url

On entity not found (404) or "unavailable" state → return None, log once,
don't crash.

Spec: C:/Jarvis/Team/TARS/cortex_vacuumops_module_spec.md §4 (ha_adapter)
"""

from __future__ import annotations

from datetime import datetime

import httpx
import structlog

from cortex_python.config.settings import Settings

log = structlog.get_logger()

# Entities that returned 404 or unavailable — log once, then suppress
_warned_missing: set[str] = set()

# HA REST API timeout — generous for entity state polls
_HA_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0)


class HARestAdapter:
    """REST-polling Home Assistant adapter.

    Phase 1: REST polling on each loop tick.
    Phase 2: WebSocket subscription upgrade for live state diffs.
    """

    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.homeassistant_url.rstrip("/")
        self._token = settings.homeassistant_token
        self._headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        """Build a fresh HTTPX async client for each call batch."""
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=_HA_TIMEOUT,
            follow_redirects=True,
        )

    async def get_entity_state(self, entity_id: str) -> dict | None:
        """Fetch the current state + attributes for one HA entity.

        Returns the full state dict on success.
        Returns None on 404, unavailable, or network error.
        Logs missing entities once; suppresses repeated warnings.
        """
        async with self._client() as client:
            try:
                r = await client.get(f"/api/states/{entity_id}")
                if r.status_code == 404:
                    if entity_id not in _warned_missing:
                        log.warning("ha_entity_not_found", entity_id=entity_id)
                        _warned_missing.add(entity_id)
                    return None
                r.raise_for_status()
                data = r.json()
                if data.get("state") in ("unavailable", "unknown"):
                    if entity_id not in _warned_missing:
                        log.warning(
                            "ha_entity_unavailable",
                            entity_id=entity_id,
                            state=data.get("state"),
                        )
                        _warned_missing.add(entity_id)
                    return None
                # Entity became available again — clear warning flag
                _warned_missing.discard(entity_id)
                return data
            except httpx.TimeoutException:
                log.warning("ha_get_entity_timeout", entity_id=entity_id)
                return None
            except Exception as exc:
                log.error("ha_get_entity_failed", entity_id=entity_id, error=str(exc))
                return None

    async def get_calendar_events(
        self,
        calendar_entity_id: str,
        start: datetime,
        end: datetime,
    ) -> list[dict]:
        """Fetch calendar events in a time window from one calendar entity.

        Uses GET /api/calendars/{entity_id}?start=<ISO>&end=<ISO>

        Returns a list of event dicts (may be empty).
        Returns [] on failure.
        """
        start_str = start.strftime("%Y-%m-%dT%H:%M:%S")
        end_str = end.strftime("%Y-%m-%dT%H:%M:%S")
        async with self._client() as client:
            try:
                r = await client.get(
                    f"/api/calendars/{calendar_entity_id}",
                    params={"start": start_str, "end": end_str},
                )
                if r.status_code == 404:
                    if calendar_entity_id not in _warned_missing:
                        log.warning("ha_calendar_not_found", entity_id=calendar_entity_id)
                        _warned_missing.add(calendar_entity_id)
                    return []
                r.raise_for_status()
                return r.json() or []
            except httpx.TimeoutException:
                log.warning("ha_calendar_timeout", entity_id=calendar_entity_id)
                return []
            except Exception as exc:
                log.error("ha_calendar_failed", entity_id=calendar_entity_id, error=str(exc))
                return []

    async def publish_loop_status(self, state: str, attributes: dict) -> None:
        """Update the CORTEX VacuumOps loop status HA sensor.

        Creates/updates: sensor.cortex_vacuumops_loop_status
        Uses POST /api/states/<entity_id> (HA REST API creates if not exists).

        State values per spec §4.6:
          "healthy" | "cooldown" | "dry_run" | "disabled" | "error"

        Called at end of every tick. Failure does NOT propagate — loop continues.
        """
        entity_id = "sensor.cortex_vacuumops_loop_status"
        payload = {
            "state": state,
            "attributes": {
                "friendly_name": "CORTEX VacuumOps Loop",
                **attributes,
            },
        }
        async with self._client() as client:
            try:
                r = await client.post(f"/api/states/{entity_id}", json=payload)
                r.raise_for_status()
            except Exception as exc:
                log.warning(
                    "ha_publish_loop_status_failed",
                    entity_id=entity_id,
                    state=state,
                    error=str(exc),
                )

    async def list_calendar_entities(self) -> list[str]:
        """Return all calendar.* entity IDs currently live in HA.

        Uses GET /api/states — filters for calendar.* entities.
        Returns [] on failure.
        """
        async with self._client() as client:
            try:
                r = await client.get("/api/states")
                r.raise_for_status()
                states = r.json()
                return [
                    s["entity_id"]
                    for s in states
                    if s.get("entity_id", "").startswith("calendar.")
                ]
            except Exception as exc:
                log.error("ha_list_calendars_failed", error=str(exc))
                return []
