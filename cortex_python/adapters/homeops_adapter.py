"""HomeOps adapter for CORTEX VacuumOps.

Calls:
  GET  /api/vacuum/units          — parse zone scores from data[].zones[]
  POST /api/vacuum/trigger        — dispatch a mission
  POST /api/decisions/vacuumops   — log a decision entry (fire-and-forget)

All calls use:
  Authorization: Bearer {settings.cortex_api_key}
  Base URL: settings.homeops_base_url

Spec: C:/Jarvis/Team/TARS/cortex_vacuumops_module_spec.md §11
"""

from __future__ import annotations

import httpx
import structlog

from cortex_python.config.settings import Settings
from cortex_python.modules.vacuumops.schemas import DecisionEntry, ZoneInfo, ZoneMeta

# Room-key derivation helpers for ZoneInfo.room_key
_ROOM_KEY_OVERRIDES: dict[str, str] = {
    "Daniel's Room": "daniel_room",
    "Dining Table": "dining_room",
}
_NO_ROOM_ZONES: frozenset[str] = frozenset({"Litter Box", "Prep Area", "Kids Table Area"})


def _derive_room_key(label: str) -> str | None:
    """Map a zone label to its snake_case ctx.rooms key, or None for sub-zones."""
    if label in _NO_ROOM_ZONES:
        return None
    if label in _ROOM_KEY_OVERRIDES:
        return _ROOM_KEY_OVERRIDES[label]
    return label.lower().replace(" ", "_").replace("'", "")

log = structlog.get_logger()

# HomeOps adapter timeout
_HOMEOPS_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0)


class HomeOpsAdapter:
    """Async HTTP adapter for HomeOps API calls from CORTEX VacuumOps."""

    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.homeops_base_url.rstrip("/")
        self._api_key = settings.cortex_api_key
        self._headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=_HOMEOPS_TIMEOUT,
            follow_redirects=True,
        )

    async def get_zone_data(self) -> tuple[dict[int, float], dict[int, ZoneInfo]]:
        """Fetch zone dirtiness scores and display metadata from HomeOps.

        GET /api/vacuum/units
        Parses response: data[].{id, floor, zones[].{id, label, score}}
        Returns:
          scores:    dict[zone_id → score]
          zone_info: dict[zone_id → ZoneInfo]

        Raises on error so the caller (synth) can handle the skip-tick path per §8.5.
        """
        async with self._client() as client:
            r = await client.get("/api/vacuum/units")
            r.raise_for_status()
            data = r.json()

        scores: dict[int, float] = {}
        zone_info: dict[int, ZoneInfo] = {}

        for unit in data.get("data", []):
            unit_id = unit.get("id")
            floor = unit.get("floor", "")
            if unit_id is None:
                continue
            for zone in unit.get("zones", []):
                zone_id = zone.get("id")
                label = zone.get("label")
                score = zone.get("score")
                if zone_id is None or label is None:
                    continue
                zone_id = int(zone_id)
                display = f"{floor} {label}".strip() if floor else label
                scores[zone_id] = float(score) if score is not None else 0.0
                zone_info[zone_id] = ZoneInfo(
                    label=label,
                    display=display,
                    unit_id=int(unit_id),
                    floor=floor,
                    room_key=_derive_room_key(label),
                )

        return scores, zone_info

    async def get_zone_metadata(self) -> dict[int, ZoneMeta]:
        """Fetch per-zone structural metadata from HomeOps.

        GET /api/vacuum/zones
        Returns a dict keyed by zone_id (int) → ZoneMeta.

        Fields mapped from response: id, unit_id, floor_type, debris_profile,
        contained_by, dispatchable (all added in HomeOps PRs #67+#68).
        child_zones is computed here as the reverse of contained_by (single pass, O(n)).

        Returns {} on failure — caller (synth) treats missing metadata as degraded
        but does NOT skip the tick (zone scores are the hard dependency, not metadata).
        """
        try:
            async with self._client() as client:
                r = await client.get("/api/vacuum/zones")
                r.raise_for_status()
                data = r.json()
        except Exception as exc:
            log.warning("homeops_get_zone_metadata_failed", error=str(exc))
            return {}

        zones: dict[int, ZoneMeta] = {}
        for z in data.get("data", []):
            zone_id = z.get("id")
            unit_id = z.get("unit_id")
            if zone_id is None or unit_id is None:
                continue
            zones[zone_id] = ZoneMeta(
                zone_id=int(zone_id),
                unit_id=int(unit_id),
                floor_type=z.get("floor_type"),
                debris_profile=z.get("debris_profile") or [],
                contained_by=z.get("contained_by"),
                dispatchable=z.get("dispatchable", True),
                low_disruption=bool(z.get("low_disruption", False)),
                # Spec §3: new column; seeded true for Litter Box by HomeOps migration 20260530000000.
                occupancy_sensor=z.get("occupancy_sensor"),
                # Spec §1.1: already in HomeOps API response (migration 014); was dropped here.
                # Now mapped so Override 2 can resolve the zone's parent room key.
                child_zones=[],  # populated below
            )

        # Compute child_zones reverse index in one pass
        for meta in zones.values():
            if meta.contained_by is not None and meta.contained_by in zones:
                zones[meta.contained_by].child_zones.append(meta.zone_id)

        return zones

    async def trigger_vacuum(
        self,
        robot: str,
        zones: list[dict],
        trigger_metadata: dict,
        dry_run: bool,
    ) -> dict:
        """POST /api/vacuum/trigger — dispatch a multi-zone mission.

        Request body matches spec §11.1:
          {
            "robot": "ethan",
            "zones": [...],
            "trigger_source": "cortex",
            "trigger_metadata": {...},
            "dry_run": false
          }

        Returns the HomeOps response dict.
        Raises httpx.HTTPStatusError on 4xx/5xx — caller handles per §10.2.
        """
        payload = {
            "robot": robot,
            "zones": zones,
            "trigger_source": "cortex",
            "trigger_metadata": trigger_metadata,
            "dry_run": dry_run,
        }
        async with self._client() as client:
            r = await client.post("/api/vacuum/trigger", json=payload)
            r.raise_for_status()
            return r.json()

    async def log_decision(self, entry: DecisionEntry) -> None:
        """POST /api/decisions/vacuumops — log a decision entry to HomeOps.

        Fire-and-forget. Logs errors but does NOT raise (spec: "log the error
        but do NOT abort the loop tick").
        """

        payload = {
            "tick_id": entry.tick_id,
            "timestamp": entry.timestamp,
            "robot": entry.robot,
            "zones": [
                {
                    "label": z.label,
                    "score": z.score,
                    "bundled": z.bundled,
                    "l1_confidence": z.l1_confidence,
                }
                for z in entry.zones
            ],
            "tier_reached": entry.tier_reached,
            "gate_failed": entry.gate_failed,
            "decision": entry.decision,
            "reason": entry.reason,
            "l1_confidence": entry.l1_confidence,
            "dry_run": entry.dry_run,
            "dispatched_at": entry.dispatched_at,
        }
        try:
            async with self._client() as client:
                r = await client.post("/api/decisions/vacuumops", json=payload)
                r.raise_for_status()
        except Exception as exc:
            log.error(
                "homeops_log_decision_failed",
                tick_id=entry.tick_id,
                decision=entry.decision,
                error=str(exc),
            )
            # Do NOT re-raise — fire-and-forget per spec
