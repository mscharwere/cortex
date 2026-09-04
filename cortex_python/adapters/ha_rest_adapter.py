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

from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from cortex_python.config.settings import Settings

log = structlog.get_logger()

# Entities that returned 404 or unavailable — log once, then suppress
_warned_missing: set[str] = set()

# HA REST API timeout — generous for entity state polls
_HA_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0)

# HA binary_sensor state strings meaning "on". Kept identical to
# synth/vacuumops_synth.py's _OCCUPIED_STATES so the prior learner's historical
# view of an entity and the gate's live view of it parse the same way — a drift
# between the two would make the learner model a signal the gate never sees.
_TRUTHY_STATES = ("on", "true", "1")

# States carrying no information. A window whose only records are these is
# indistinguishable from having no records at all, and must not be read as "off".
_NON_STATES = ("unavailable", "unknown", "none", "")


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
        start_str = start.isoformat()
        end_str = end.isoformat()
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

    async def get_state_history(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
    ) -> list[tuple[datetime, bool]] | None:
        """Fetch an entity's state-change timeline as (changed_at_utc, truthy) pairs.

        Uses GET /api/history/period/<start ISO>?filter_entity_id=...&end_time=...

        Added for the VacuumOps occupancy prior learner (spec §4.2 / PR A1),
        which derives each 30-minute slot's occupied FRACTION from state-change
        timestamps rather than from tick samples -- the loop returns a 300 s
        interval whenever a robot is cleaning, so a tick-sampled learner would be
        biased by exactly the windows the feature reasons about.

        Return contract, and the distinction the caller depends on:
          list  -- the call succeeded. May be EMPTY, which means the recorder
                   holds no data for this window (purged, or the entity did not
                   exist yet). Callers must treat empty as "unknown", never as
                   "the sensor read off the whole time".
          None  -- the call FAILED (timeout, transport error, non-2xx). The
                   learner does not advance its watermark on None, so the window
                   is retried rather than silently skipped.

        HA includes the state in effect AT start_time as the first element when
        data exists, which is what makes the leading segment of a window
        attributable. `minimal_response` and `no_attributes` are sent as bare
        valueless flags because HA tests for parameter PRESENCE, not value.
        `significant_changes_only` is deliberately NOT sent -- omitting it
        returns every state change, which is what an exact timeline needs.
        """
        start_utc = start.astimezone(UTC) if start.tzinfo else start.replace(tzinfo=UTC)
        end_utc = end.astimezone(UTC) if end.tzinfo else end.replace(tzinfo=UTC)

        async with self._client() as client:
            try:
                r = await client.get(
                    f"/api/history/period/{start_utc.isoformat()}",
                    params=[
                        ("filter_entity_id", entity_id),
                        ("end_time", end_utc.isoformat()),
                        ("minimal_response", ""),
                        ("no_attributes", ""),
                    ],
                )
                if r.status_code == 404:
                    # The history integration is not loaded, or the entity is
                    # unknown to the recorder. Either way this is a failed read,
                    # not an empty one -- do not let it be mistaken for "clear".
                    log.warning("ha_history_not_found", entity_id=entity_id)
                    return None
                r.raise_for_status()
                payload = r.json()
            except httpx.TimeoutException:
                log.warning("ha_history_timeout", entity_id=entity_id)
                return None
            except Exception as exc:
                log.error("ha_history_failed", entity_id=entity_id, error=str(exc))
                return None

        return _parse_history_payload(payload)

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

    @staticmethod
    def parse_history_payload(payload: Any) -> list[tuple[datetime, bool]]:
        """Exposed for tests — see the module-level _parse_history_payload."""
        return _parse_history_payload(payload)

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
                    and s.get("state") != "unavailable"
                ]
            except Exception as exc:
                log.error("ha_list_calendars_failed", error=str(exc))
                return []


def _parse_history_payload(payload: Any) -> list[tuple[datetime, bool]]:
    """Flatten HA's /api/history/period response into (changed_at_utc, truthy) pairs.

    HA returns a list of per-entity lists. With `minimal_response` only the FIRST
    element of each list carries the full state dict; the rest carry just
    `state` / `last_changed` / `last_updated`. Both shapes are handled here.

    Records whose state is unavailable/unknown/None are DROPPED rather than
    recorded as False. Dropping them means the surrounding known state extends
    across the gap, which models a momentarily-flapping integration far better
    than asserting the room emptied. It also keeps a window whose ONLY records
    are non-states parsing as empty, which the caller reads as "no data" instead
    of "clear" — the distinction the whole learner rests on.

    Records without a usable timestamp are dropped for the same reason: a
    transition that cannot be placed in time cannot be attributed to a slot.
    """
    if not isinstance(payload, list):
        return []

    out: list[tuple[datetime, bool]] = []
    for entity_series in payload:
        if not isinstance(entity_series, list):
            continue
        for record in entity_series:
            if not isinstance(record, dict):
                continue
            raw_state = str(record.get("state", "")).strip().lower()
            if raw_state in _NON_STATES:
                continue
            raw_ts = record.get("last_changed") or record.get("last_updated")
            if not isinstance(raw_ts, str):
                continue
            try:
                ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            ts = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
            out.append((ts, raw_state in _TRUTHY_STATES))

    out.sort(key=lambda item: item[0])
    return out
