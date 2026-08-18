"""HomeOps adapter for CORTEX VacuumOps.

Calls:
  GET  /api/vacuum/units             — parse zone scores from data[].zones[]
  POST /api/vacuum/trigger           — dispatch a mission
  POST /api/decisions/vacuumops      — log a decision entry (fire-and-forget)
  GET  /api/cortex/vacuumops-settings — live mop-cadence gate kill switch (fail-closed)

All calls use:
  Authorization: Bearer {settings.cortex_api_key}
  Base URL: settings.homeops_base_url

Spec: C:/Jarvis/Team/TARS/cortex_vacuumops_module_spec.md §11
"""

from __future__ import annotations

from datetime import datetime

import httpx
import structlog

from cortex_python.config.settings import Settings
from cortex_python.modules.vacuumops.schemas import DecisionEntry, ZoneInfo, ZoneMeta

# Explicit zone-label → ctx.rooms key mapping for every known zone.
# room_key=None means the zone has no parent room sensor (sub-zone);
# zone_active_use_check and door_open_check treat these as always clear.
# Add a new entry here whenever a zone is added to HomeOps or an HA sensor
# is renamed — never rely on convention-based derivation.
_ZONE_LABEL_TO_ROOM_KEY: dict[str, str | None] = {
    # Ethan 3F
    "Litter Box": None,
    "Loft": "loft",
    "Office": "office",
    "Gym": "gym",
    # Saros 1F
    "Kitchen": "kitchen",
    "Bathroom": "bathroom",
    "Living Room": "living_room",
    "Hallway": "hallway",
    "Prep Area": None,
    "Dining Table": "dining_room",
    # Sam 2F
    "Master Bathroom": "master_bath",
    "Master Bedroom": "master_bedroom",
    "Upper Hallway": "upper_hallway",
    "Carlitos Room": "carlitos_room",
    "Kids Table Area": None,
    "Daniel's Room": "daniel_room",
}

log = structlog.get_logger()


def _parse_ts(raw: object) -> datetime | None:
    """Parse a HomeOps timestamp field into a datetime, tolerantly.

    HomeOps serializes these as ISO8601 (often with a trailing "Z", which
    fromisoformat only accepts natively from 3.11+; normalized here anyway so a
    format change cannot take down the tick). Any unparseable value degrades to
    None, which the mop gate treats as "unknown" rather than "due".
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        log.warning("homeops_timestamp_parse_failed", raw=raw)
        return None


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

    async def get_zone_data(self) -> tuple[dict[int, float], dict[int, ZoneInfo], dict[str, bool]]:
        """Fetch zone dirtiness scores, display metadata, and per-unit dry_run flags from HomeOps.

        GET /api/vacuum/units
        Parses response: data[].{id, floor, nickname, dry_run, zones[].{id, label, score}}
        Returns:
          scores:         dict[zone_id → score]
          zone_info:      dict[zone_id → ZoneInfo]
          unit_dry_runs:  dict[robot_name → dry_run bool]
                          robot_name is the lowercased unit nickname (e.g. "ethan", "sam")

        unit_dry_runs is used by the loop to compute the effective dry_run flag per robot:
          effective = unit_dry_runs.get(robot, True)
        Defaults to True (dry_run) when a robot has no entry in the DB.

        Raises on error so the caller (synth) can handle the skip-tick path per §8.5.
        """
        async with self._client() as client:
            r = await client.get("/api/vacuum/units")
            r.raise_for_status()
            data = r.json()

            scores: dict[int, float] = {}
            zone_info: dict[int, ZoneInfo] = {}
            unit_dry_runs: dict[str, bool] = {}

            for unit in data.get("data", []):
                unit_id = unit.get("id")
                floor = unit.get("floor", "")
                robot_name = (unit.get("nickname") or "").strip().lower()
                if unit_id is None:
                    continue
                if not robot_name:
                    log.warning("unit_dry_run_skipped_no_nickname", unit_id=unit_id)
                    continue

                # Per-unit dry_run: default True (safe) if column absent (pre-migration).
                unit_dry_runs[robot_name] = bool(unit.get("dry_run", True))

                for zone in unit.get("zones", []):
                    zone_id = zone.get("id")
                    label = zone.get("label")
                    score = zone.get("score")
                    if zone_id is None or label is None:
                        continue
                    zone_id = int(zone_id)
                    display = f"{floor} {label}".strip() if floor else label
                    scores[zone_id] = float(score) if score is not None else 0.0
                    room_key = _ZONE_LABEL_TO_ROOM_KEY.get(label)
                    if label not in _ZONE_LABEL_TO_ROOM_KEY:
                        log.warning("zone_label_not_in_room_key_map", label=label, zone_id=zone_id)
                    zone_info[zone_id] = ZoneInfo(
                        label=label,
                        display=display,
                        unit_id=int(unit_id),
                        floor=floor,
                        room_key=room_key,
                    )

            return scores, zone_info, unit_dry_runs

    async def get_vacuumops_mop_enabled(self) -> bool:
        """Fetch the live, DB-backed mop-cadence gate kill switch from HomeOps.

        GET /api/cortex/vacuumops-settings
        Response: { data: { mop_enabled, mop_enabled_updated_at, mop_enabled_updated_by } }

        Replaces the CORTEX_VACUUMOPS_MOP_ENABLED env var (formerly read once at
        process start via Settings/VacuumOpsConfig). This is called fresh every
        loop tick by vacuumops_synth.build_snapshot() — no separate cache/TTL on
        this side. The adaptive tick interval (60-300s, see loop.next_interval)
        is the only staleness bound, matching the existing unit-level dry_run
        read path (get_zone_data(), same adapter, no caching either).

        Fail-CLOSED on every ambiguity, mirroring the module's stance everywhere
        else (mop.py module docstring, "DEGRADED CONTEXT" section): wet-mopping
        is a physical action on real floors that runs unsupervised, so anything
        other than a confirmed `true` resolves to False.
          - Network error / timeout / non-2xx status  -> False (logged)
          - Malformed JSON / missing "data" object     -> False (logged)
          - "mop_enabled" key absent or not a bool     -> False (logged)
          - Confirmed True or False                    -> that value, no log noise

        Never raises — a HomeOps outage on this read must not take down the tick
        (zone scores are the only hard dependency; see build_snapshot's §8.5
        docstring). This mirrors get_zone_metadata()'s try/except-and-degrade
        shape below, not get_zone_data()'s raise-on-failure shape — zone scores
        are load-bearing for the whole tick, this flag is not.
        """
        try:
            async with self._client() as client:
                r = await client.get("/api/cortex/vacuumops-settings")
                r.raise_for_status()
                body = r.json()
        except Exception as exc:
            log.warning("homeops_get_vacuumops_settings_failed", error=str(exc))
            return False

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            log.warning("homeops_vacuumops_settings_malformed", body=body)
            return False

        value = data.get("mop_enabled")
        if not isinstance(value, bool):
            log.warning("homeops_vacuumops_settings_mop_enabled_missing_or_not_bool", value=value)
            return False

        return value

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
                # Spec §3: new column; seeded true for Litter Box by HomeOps migration
                # 20260530000000.
                occupancy_sensor=z.get("occupancy_sensor"),
                # Spec §1.1: already in HomeOps API response (migration 014); was dropped here.
                # Now mapped so Override 2 can resolve the zone's parent room key.
                # Mop-cadence gate inputs (HomeOps migration 20260809000000).
                # last_mopped_at drives the 7-day schedule arm; mop_requested_at is
                # the signal arm. Both None when HomeOps predates the migration,
                # which the gate treats as degraded → declines to mop.
                last_mopped_at=_parse_ts(z.get("last_mopped_at")),
                mop_requested_at=_parse_ts(z.get("mop_requested_at")),
                # Absent on any HomeOps build predating the mop-tracking
                # migration → False → the gate declines rather than reading a
                # null last_mopped_at as "never mopped, deep-mop everything".
                mop_tracking_available=bool(z.get("mop_tracking_available", False)),
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
        mop: bool = False,
        mop_intensity: str | None = None,
    ) -> dict:
        """POST /api/vacuum/trigger — dispatch a multi-zone mission.

        Request body matches spec §11.1:
          {
            "robot": "ethan",
            "zones": [...],
            "trigger_source": "cortex",
            "trigger_metadata": {...},
            "dry_run": false,
            "mop": true,             # Roborock only, omitted when false
            "mop_intensity": "low"   # required by HomeOps when mop is true
          }

        mop/mop_intensity come from the mop-cadence gate (modules/vacuumops/mop.py).
        They are omitted entirely when mop is False: HomeOps defaults mop to false
        and sends "off" to HA, and the fields are ignored for non-Roborock units,
        so sending them unconditionally would only add noise to iRobot dispatches.

        Returns the HomeOps response dict.
        Raises httpx.HTTPStatusError on 4xx/5xx — caller handles per §10.2.
        """
        payload: dict = {
            "robot": robot,
            "zones": zones,
            "trigger_source": "cortex",
            "trigger_metadata": trigger_metadata,
            "dry_run": dry_run,
        }
        if mop:
            if not mop_intensity:
                # HomeOps returns 422 for mop=true without an intensity. Fail here
                # with a clear message rather than surfacing an opaque HTTP error.
                raise ValueError("mop_intensity is required when mop is True")
            payload["mop"] = True
            payload["mop_intensity"] = mop_intensity
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
                    "display": z.display,
                    "score": z.score,
                    "bundled": z.bundled,
                    "l1_confidence": z.l1_confidence,
                    "result": z.result,
                    "gate_failed": z.gate_failed,
                    "gate_reason": z.gate_reason,
                    "l1_decision": z.l1_decision,
                    "l1_reason": z.l1_reason,
                    "l1_defer_until_hint": z.l1_defer_until_hint,
                    "l1_passes": z.l1_passes,
                    "l1_intensity": z.l1_intensity,
                    "l1_params_reason": z.l1_params_reason,
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
            # Mop-cadence gate outcome — makes wet-vs-dry inspectable through
            # get_vacuum_decisions alongside the existing dispatch reasoning.
            "mop": entry.mop,
            "mop_intensity": entry.mop_intensity,
            "mop_reason": entry.mop_reason,
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
