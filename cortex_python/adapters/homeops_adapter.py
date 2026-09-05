"""HomeOps adapter for CORTEX VacuumOps.

Calls:
  GET  /api/vacuum/units             — parse zone scores from data[].zones[]
  POST /api/vacuum/trigger           — dispatch a mission
  POST /api/decisions/vacuumops      — log a decision entry (fire-and-forget)
  GET  /api/cortex/vacuumops-settings — live kill switches, all fail-closed:
       mop_enabled (mop-cadence gate) + opportunity_actuate (predictive patience)

All calls use:
  Authorization: Bearer {settings.cortex_api_key}
  Base URL: settings.homeops_base_url

Spec: C:/Jarvis/Team/TARS/cortex_vacuumops_module_spec.md §11
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

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


def _bool_setting(data: dict, key: str) -> bool:
    """Read one kill switch out of the settings payload. Fail-CLOSED.

    ONLY a literal `True` is True. A missing key (HomeOps predates the column),
    a null, and — importantly — the STRING "true" or the integer 1 all resolve
    to False rather than being truthy-coerced: a flag that actuates real
    hardware must not be turned on by a serialization accident.
    """
    value = data.get(key)
    if not isinstance(value, bool):
        log.warning("homeops_vacuumops_settings_flag_missing_or_not_bool", key=key, value=value)
        return False
    return value


# HomeOps adapter timeout
_HOMEOPS_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0)


@dataclass(frozen=True)
class VacuumOpsLiveSettings:
    """Every live, DB-backed VacuumOps kill switch, read in ONE round trip.

    HomeOps serves all of these from a single row of `cortex_vacuumops_settings`
    via one `GET /api/cortex/vacuumops-settings`, so CORTEX reads them together
    rather than issuing a request per flag. Two flags today; the record exists so
    a third costs a field rather than another per-tick HTTP call.

    ⚠ `read_ok` IS NOT "is anything enabled". It is "did we actually hear back
    from HomeOps". Both booleans below fail CLOSED to False, which means a
    confirmed-off switch and an unreachable HomeOps produce byte-identical
    values — and those are different facts. The mop gate can live with the
    conflation (its shadow reason covers both), but `r1.opportunity_check` holds
    invariant 3: "every degraded path returns PASS with a reason that NAMES the
    degradation." Without this flag the rule could not tell an operator's
    deliberate "not yet" from a silent outage, which is the exact shape of the
    2026-08-31 invisible-no-op root cause. Consumers that do not care may ignore
    it; consumers that log a reason string must not.
    """

    mop_enabled: bool = False
    opportunity_actuate: bool = False
    read_ok: bool = False


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

    async def get_vacuumops_settings(self) -> VacuumOpsLiveSettings:
        """Fetch every live, DB-backed VacuumOps kill switch in ONE round trip.

        GET /api/cortex/vacuumops-settings
        Response: { data: {
            mop_enabled,          mop_enabled_updated_at,          mop_enabled_updated_by,
            opportunity_actuate,  opportunity_actuate_updated_at,  opportunity_actuate_updated_by
        } }

        ONE CALL, NOT ONE PER FLAG. HomeOps serves both switches from the same
        row of `cortex_vacuumops_settings`, and the loop needs both on the same
        tick, so reading them separately would double the per-tick request count
        for zero added freshness — and would additionally let the two flags come
        from two different instants, which is a state the DB row cannot actually
        be in. `get_vacuumops_mop_enabled()` and
        `get_vacuumops_opportunity_actuate()` below are thin wrappers over this
        method, kept for callers that want exactly one flag; the loop's per-tick
        path calls this one.

        Called fresh every loop tick by vacuumops_synth.build_snapshot() — no
        cache/TTL on this side. The adaptive tick interval (60-300 s, see
        loop.next_interval) is the only staleness bound, matching the existing
        unit-level dry_run read path (get_zone_data(), same adapter, uncached
        too).

        Fail-CLOSED on every ambiguity, per flag independently:
          - Network error / timeout / non-2xx status   -> all False, read_ok=False
          - Malformed JSON / missing "data" object      -> all False, read_ok=False
          - A key absent or not a bool                  -> THAT flag False, read_ok=True
          - Confirmed True or False                     -> that value, no log noise

        Note the third case carefully: a HomeOps that answers but predates the
        `opportunity_actuate` column is NOT a degraded read. We heard back, the
        answer is "this switch does not exist here", and the fail-closed
        interpretation of that is a confirmed off — so read_ok stays True. Only
        "we never got an answer" clears read_ok. See VacuumOpsLiveSettings for
        why the distinction is load-bearing rather than cosmetic.

        Never raises — a HomeOps outage on this read must not take down the tick
        (zone scores are the only hard dependency; see build_snapshot's §8.5
        docstring). This mirrors get_zone_metadata()'s try/except-and-degrade
        shape below, not get_zone_data()'s raise-on-failure shape: zone scores
        are load-bearing for the whole tick, these flags are not.
        """
        try:
            async with self._client() as client:
                r = await client.get("/api/cortex/vacuumops-settings")
                r.raise_for_status()
                body = r.json()
        except Exception as exc:
            log.warning("homeops_get_vacuumops_settings_failed", error=str(exc))
            return VacuumOpsLiveSettings(read_ok=False)

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            log.warning("homeops_vacuumops_settings_malformed", body=body)
            return VacuumOpsLiveSettings(read_ok=False)

        return VacuumOpsLiveSettings(
            mop_enabled=_bool_setting(data, "mop_enabled"),
            opportunity_actuate=_bool_setting(data, "opportunity_actuate"),
            read_ok=True,
        )

    async def get_vacuumops_mop_enabled(self) -> bool:
        """The live mop-cadence gate kill switch. Fail-closed to False.

        Thin wrapper over get_vacuumops_settings() — see that method for the
        endpoint, the full fail-closed matrix and why the flags are fetched
        together. Kept as its own method because the mop gate's contract is
        "a bool, False on any doubt" and nothing about it needs the record.

        Replaces the CORTEX_VACUUMOPS_MOP_ENABLED env var (formerly read once at
        process start via Settings/VacuumOpsConfig). Wet-mopping is a physical
        action on real floors that runs unsupervised, so anything other than a
        confirmed `true` resolves to False.
        """
        return (await self.get_vacuumops_settings()).mop_enabled

    async def get_vacuumops_opportunity_actuate(self) -> bool:
        """The live predictive-patience actuation switch. Fail-closed to False.

        Thin wrapper over get_vacuumops_settings() — see that method for the
        endpoint and the full fail-closed matrix.

        This is PR A4's flip, moved out of the source tree. It was a static
        `opportunity_actuate` field on the job descriptors (jobs.py), which made
        turning predictive patience on a code change, a review, a deploy — and
        made turning it OFF the same, at the exact moment someone would want it
        off fastest. It is now a DB row Carlos can flip, following the same
        migration `mop_enabled` already made (config.py's field docstring
        records that transition and the two precedents behind it).

        Fail-closed to False is the strictly conservative direction here, and it
        is worth naming which direction that is: False means the rule computes
        its verdict and logs it but cannot withhold a dispatch — i.e. an
        unreachable HomeOps degrades to CURRENT, pre-A4 behaviour, never to a
        robot silently declining to clean because a settings read timed out.
        """
        return (await self.get_vacuumops_settings()).opportunity_actuate

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

    async def get_mission_stats(
        self, robot_id: int, zone_id: int | None = None
    ) -> dict[str, Any] | None:
        """Aggregate mission-duration stats for a robot, optionally zone-scoped.

        GET /api/vacuum/missions/log/stats?robot_id=<id>[&zone_id=<id>]

        Feeds `opportunity.duration_estimate()` (PR A2/A3): how many minutes to
        reserve for a mission when asking whether it fits in the next clear
        window. The fields that matter are `p75_active_duration_min` /
        `p90_active_duration_min` / `avg_active_duration_min`, shipped by A0
        (homeOps#206).

        ⚠ THE ACTIVE FIELDS, NEVER THE WALL-CLOCK ONES. `avg_duration_min`
        measures dispatch → mission-log close-out, including the return leg and
        a Roborock's double dock-bounce — on the Saros it reads 44.8 min against
        an actual 26.3 min of cleaning. Sizing a fit window off it would reserve
        ~70% more than a mission needs and turn every fit check into a deferral.
        The whole payload is returned here rather than a picked field, but
        `opportunity._read_active_minutes()` enforces the distinction
        structurally with a field whitelist — do not "helpfully" normalise a
        wall-clock figure into an active one on the way through.

        Returns None on any failure or a malformed payload. NOT an empty dict:
        `duration_estimate()` treats an absent payload as "no usable duration"
        and degrades the opportunity read to `unavailable`, which fails OPEN to
        current behaviour. An empty dict would travel the same path, but None
        says "we never got an answer" rather than "the robot has no history",
        and those are different facts in the decision log.
        """
        params: dict[str, int] = {"robot_id": robot_id}
        if zone_id is not None:
            params["zone_id"] = zone_id
        try:
            async with self._client() as client:
                r = await client.get("/api/vacuum/missions/log/stats", params=params)
                r.raise_for_status()
                payload = r.json()
        except Exception as exc:
            log.warning(
                "homeops_get_mission_stats_failed",
                robot_id=robot_id,
                zone_id=zone_id,
                error=str(exc),
            )
            return None

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            log.warning("homeops_get_mission_stats_malformed", robot_id=robot_id, zone_id=zone_id)
            return None
        return data

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
