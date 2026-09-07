"""VacuumOps synthesizer — builds ContextSnapshot for one loop tick.

Fetches:
  - Robot states (vacuum.ethan, vacuum.sam) from HA REST
  - Room states (occupancy + detected_activity) from HA REST
  - Person activity states from HA REST
  - Home context sensor from HA REST
  - Zone scores from HomeOps
  - Calendar events (2h window) from all calendar.* HA entities

Computes:
  - noise_budget (§6.3) — stored as ctx.noise_budget
  - quiet_hours_2f — sourced from sensor.home_context.attributes.quiet_hours
  - quiet_hours_1f — 1F-local courtesy window (utils.is_quiet_hours_1f), NOT the
    same signal as quiet_hours_2f

Graceful degradation per spec §8.5:
  - HA WS down: use safe defaults, mark ctx.degraded = True
  - HomeOps zone scores fail: RAISE (caller skips tick)
  - Calendar pull fails: ctx.upcoming_events = [], ctx.calendar_degraded = True

Spec: C:/Jarvis/Team/TARS/cortex_vacuumops_module_spec.md §8.5
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from cortex_python.config.settings import Settings
from cortex_python.modules.vacuumops.noise import noise_budget
from cortex_python.modules.vacuumops.schemas import (
    CalendarEvent,
    ContextSnapshot,
    GateReading,
    OccupancyReading,
    PersonActivity,
    RobotState,
    RoomActivity,
    ZoneMeta,
)
from cortex_python.modules.vacuumops.utils import is_quiet_hours_1f

if TYPE_CHECKING:
    from cortex_python.adapters.ha_rest_adapter import HARestAdapter
    from cortex_python.adapters.homeops_adapter import HomeOpsAdapter, VacuumOpsLiveSettings

log = structlog.get_logger()

# People tracked by CORTEX
_PEOPLE = ["carlos", "elena", "carlitos", "daniel", "iestaf"]

# Rooms tracked — mirrors FLOOR_ROOM_MAP in noise.py
_TRACKED_ROOMS = [
    # 1F (Saros 10R)
    "kitchen",
    "living_room",
    "hallway",
    "dining_room",
    "prep_area",
    "bathroom",
    # 2F (Sam j7+)
    "master_bedroom",
    "master_bathroom",
    "upper_hallway",
    "carlitos_room",
    "daniel_room",
    "kids_table_area",
    # 3F (Ethan j9+)
    "loft",
    "office",
    "gym",
    "family_room",
]

# Entity ID overrides for robots whose HA entity doesn't follow vacuum.{robot} convention.
_ROBOT_ENTITY_MAP: dict[str, dict[str, str]] = {
    "saros": {
        "vacuum": "vacuum.saros_10r",
        "battery": "sensor.saros_10r_battery",
    },
}

# NOTE — there is deliberately no _DOOR_ENTITY_MAP here any more, and no
# "binary_sensor.<room-key>-plus-a-suffix" naming-convention fallback. Entry gating is
# configured per zone in HomeOps (vac_zone_cleanliness.entry_gate_entity) and
# read here by entity id, exactly as occupancy_sensor already is.
#
# The removed pattern produced two real dispatch bugs in five weeks: a July 2026
# fetch-ordering bug (PR #40) and a room-key mismatch ("master_bath" vs
# "master_bathroom") that made Master Bathroom's gate resolve to "no sensor" →
# "treat as open". Both share one root cause — a room-key-indexed lookup that,
# when the key does not match, yields silence rather than an error. Do not
# reintroduce a hardcoded map or a name guess in any form; add the entity to the
# HomeOps column instead.


# Dedicated per-floor occupancy rollups from the area_occupancy HACS integration
# (custom_components/area_occupancy). These are purpose-built floor-level signals
# maintained by HA itself; CORTEX previously ignored them and re-derived floor
# state by OR-ing the per-room sensors in FLOOR_ROOM_MAP, which silently omitted
# every room with no convention-named entity. Verified live 2026-08-31: all three
# exist, all three carry device_class=occupancy and a real last_changed.
_FLOOR_OCCUPANCY_ENTITY: dict[str, str] = {
    "1F": "binary_sensor.first_floor_occupancy_status",
    "2F": "binary_sensor.second_floor_occupancy_status",
    "3F": "binary_sensor.third_floor_occupancy_status",
}

# HA binary_sensor state strings that mean "occupied". Kept identical to the
# set _fetch_room_activity has always used, so this refactor introduces no
# parsing drift alongside the behavioural changes.
_OCCUPIED_STATES = ("on", "true", "1")

# Entry-gate polarity. EXACTLY these two strings are meaningful; everything else
# — "unavailable", "unknown", "open", "closed", a missing entity — is unresolved
# and makes entry_gate_check block.
#
# The strictness is the point. The permissive tuple the door gate used
# (`state in ("on","true","open")`, everything else falsy → "closed"… but a
# missing entity → None → "treat as open") is what let a typo read as an open
# door. An entry gate may be a binary_sensor OR an input_boolean, and both
# domains report exactly "on"/"off", so no laxity is needed to cover the
# supported sources.
_GATE_PROCEED_STATE = "on"
_GATE_BLOCK_STATE = "off"


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _parse_last_changed(state: dict[str, Any] | None) -> datetime | None:
    """Extract HA's last_changed off a state payload as an aware-UTC datetime.

    Every HA state carries last_changed — the timestamp of the last state
    *transition* (as opposed to last_updated, which also moves on attribute-only
    churn). That distinction is exactly what the occupancy confirmation window
    needs: how long the sensor has actually been reporting its current value.

    Returns None on absence or unparseable input; callers treat None as "dwell
    unknown", never as "occupied".
    """
    if not state:
        return None
    raw = state.get("last_changed")
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        log.warning("ha_last_changed_parse_failed", raw=raw)
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


async def _fetch_occupancy_reading(ha_adapter: HARestAdapter, entity_id: str) -> OccupancyReading:
    """Read one occupancy binary_sensor directly, preserving availability + dwell.

    A missing or unavailable entity yields available=False — NOT occupied=False.
    The distinction is the whole point: the gate must fall through to a coarser
    signal on an absent sensor rather than reading absence as an empty room.
    """
    state = await ha_adapter.get_entity_state(entity_id)
    if state is None:
        return OccupancyReading(entity_id=entity_id, occupied=False, available=False)
    return OccupancyReading(
        entity_id=entity_id,
        occupied=str(state.get("state", "off")).lower() in _OCCUPIED_STATES,
        last_changed=_parse_last_changed(state),
        available=True,
    )


async def _fetch_floor_occupancy(ha_adapter: HARestAdapter) -> dict[str, OccupancyReading]:
    """Read the three area_occupancy floor rollups. Keys: "1F" | "2F" | "3F"."""
    out: dict[str, OccupancyReading] = {}
    for floor, entity_id in _FLOOR_OCCUPANCY_ENTITY.items():
        try:
            out[floor] = await _fetch_occupancy_reading(ha_adapter, entity_id)
        except Exception as exc:
            log.warning("floor_occupancy_fetch_failed", floor=floor, error=str(exc))
            out[floor] = OccupancyReading(entity_id=entity_id, occupied=False, available=False)
    return out


async def _fetch_occupancy_readings(
    ha_adapter: HARestAdapter, zone_metadata: dict[int, ZoneMeta]
) -> dict[str, OccupancyReading]:
    """Read every HomeOps-designated occupancy_sensor entity directly.

    Returns a map keyed by entity_id, so zones that legitimately share a sensor
    (all three Ethan kitchen zones point at the same presence sensor; the litter
    box shares the hallway group) cost one HA call between them — typically ~6
    calls, not one per zone. R1 looks up by zone_meta.occupancy_sensor.

    Reading the designated entity *directly* is the correction here. The previous
    code path recovered a room key from the entity id by stripping a known suffix
    and then read binary_sensor.{room}_occupancy_status — a different entity, and
    for several zones one that does not exist. The designated sensors that no
    suffix rule recovers are real and live (verified 2026-08-31), e.g.
    binary_sensor.emotion_kitchen_dining_table_presence and
    binary_sensor.master_bedroom_emotion_any_presence.
    """
    out: dict[str, OccupancyReading] = {}
    for meta in zone_metadata.values():
        entity_id = meta.occupancy_sensor
        if not entity_id or entity_id in out:
            continue
        try:
            out[entity_id] = await _fetch_occupancy_reading(ha_adapter, entity_id)
        except Exception as exc:
            log.warning("zone_occupancy_fetch_failed", entity_id=entity_id, error=str(exc))
            out[entity_id] = OccupancyReading(entity_id=entity_id, occupied=False, available=False)
    return out


async def _fetch_gate_reading(ha_adapter: HARestAdapter, entity_id: str) -> GateReading:
    """Read one entry-gate entity. Anything but a clean "on"/"off" is UNRESOLVED.

    Note the asymmetry with _fetch_occupancy_reading: an unreadable occupancy
    sensor falls through to a coarser tier, but an unreadable gate has no coarser
    tier and must block. So this returns resolved=False rather than a defaulted
    proceed value, and the caller is required to treat that as a block.

    The entity may be any domain — binary_sensor for a physical door,
    input_boolean for a manual room disable. Both report "on"/"off", so the
    parsing is domain-agnostic and no device_class inspection is needed.
    """
    state = await ha_adapter.get_entity_state(entity_id)
    if state is None:
        return GateReading(entity_id=entity_id, proceed=False, resolved=False, raw_state=None)
    raw = str(state.get("state", "")).lower()
    if raw == _GATE_PROCEED_STATE:
        return GateReading(entity_id=entity_id, proceed=True, resolved=True, raw_state=raw)
    if raw == _GATE_BLOCK_STATE:
        return GateReading(entity_id=entity_id, proceed=False, resolved=True, raw_state=raw)
    return GateReading(entity_id=entity_id, proceed=False, resolved=False, raw_state=raw)


async def _fetch_gate_readings(
    ha_adapter: HARestAdapter, zone_metadata: dict[int, ZoneMeta]
) -> dict[str, GateReading]:
    """Read every HomeOps-designated entry_gate_entity directly, keyed by entity id.

    Mirrors _fetch_occupancy_readings: deduped across zones that share a gate
    (Kids Table Area rides the Master Bedroom door), so this is ~5 HA calls, not
    one per zone. Zones with a null entry_gate_entity are skipped entirely — they
    have no gate and cost nothing.

    A fetch that raises still lands in the map as resolved=False, so the gate
    blocks loudly rather than vanishing. An HA outage therefore parks the gated
    jobs for the tick instead of waving them through; that is the intended
    direction of failure for a physical action taken unsupervised.
    """
    out: dict[str, GateReading] = {}
    for meta in zone_metadata.values():
        entity_id = meta.entry_gate_entity
        if not entity_id or entity_id in out:
            continue
        try:
            out[entity_id] = await _fetch_gate_reading(ha_adapter, entity_id)
        except Exception as exc:
            log.warning("entry_gate_fetch_failed", entity_id=entity_id, error=str(exc))
            out[entity_id] = GateReading(
                entity_id=entity_id, proceed=False, resolved=False, raw_state=None
            )
    return out


async def _fetch_person_activity(ha_adapter: HARestAdapter, name: str) -> PersonActivity:
    """Fetch PersonActivity for one person from HA REST."""
    entity_id = f"sensor.{name}_activity"
    state = await ha_adapter.get_entity_state(entity_id)
    if state is None:
        return PersonActivity(activity="unknown", confidence=0.0)

    attrs = state.get("attributes", {})
    activity = state.get("state", "unknown")
    confidence = _safe_float(attrs.get("confidence", attrs.get("probability", 0.0)))
    piano = attrs.get("piano")
    sleep_confidence = attrs.get("sleep_confidence")
    if sleep_confidence is not None:
        sleep_confidence = _safe_float(sleep_confidence)

    return PersonActivity(
        activity=activity,
        confidence=confidence,
        piano=bool(piano) if piano is not None else None,
        sleep_confidence=sleep_confidence,
    )


async def _fetch_room_activity(ha_adapter: HARestAdapter, room: str) -> RoomActivity | None:
    """Fetch RoomActivity for one room. Returns None if sensors unavailable.

    This function no longer reads any door/gate entity. The July 2026 bug this
    used to carry (door read ordered behind an early return, so rooms with no
    occupancy sensor skipped the door fetch entirely) is now structurally
    impossible: entry gating does not pass through RoomActivity, or through a
    room key, at all. See _fetch_gate_readings.
    """
    occupancy_id = f"binary_sensor.{room}_occupancy_status"
    activity_id = f"sensor.{room}_detected_activity"

    occ_state = await ha_adapter.get_entity_state(occupancy_id)
    act_state = await ha_adapter.get_entity_state(activity_id)

    if occ_state is None and act_state is None:
        return None

    raw_occupancy = False
    occupancy_last_changed: datetime | None = None
    if occ_state is not None:
        raw_occupancy = str(occ_state.get("state", "off")).lower() in _OCCUPIED_STATES
        occupancy_last_changed = _parse_last_changed(occ_state)

    detected = "unknown"
    confidence = 0.0
    if act_state is not None:
        detected = act_state.get("state", "unknown")
        confidence = _safe_float(
            act_state.get("attributes", {}).get(
                "probability", act_state.get("attributes", {}).get("confidence", 0.0)
            )
        )
    elif occ_state is not None:
        # Only occupancy available — infer
        detected = "active" if raw_occupancy else "idle"
        confidence = 0.5

    return RoomActivity(
        detected=detected,
        confidence=confidence,
        raw_occupancy=raw_occupancy,
        occupancy_last_changed=occupancy_last_changed,
        occupancy_available=occ_state is not None,
    )


async def _fetch_robot_state(ha_adapter: HARestAdapter, robot: str) -> RobotState:
    """Fetch RobotState for one robot from HA REST."""
    robot_cfg = _ROBOT_ENTITY_MAP.get(robot, {})
    entity_id = robot_cfg.get("vacuum", f"vacuum.{robot}")
    battery_entity_id = robot_cfg.get("battery", f"sensor.{robot}_battery")

    state = await ha_adapter.get_entity_state(entity_id)
    if state is None:
        # Robot unavailable — safe default (not docked, low battery)
        return RobotState(state="error", battery_pct=0)

    attrs = state.get("attributes", {})
    robot_state = state.get("state", "error")

    # Fetch dedicated battery sensor (more reliable than vacuum entity attribute).
    # The Roomba HA integration does NOT expose battery_level on the vacuum entity
    # top-level attributes — it is buried in raw_state.batPct.  The dedicated
    # sensor.{robot}_battery entity is the canonical surface.
    battery_state = await ha_adapter.get_entity_state(battery_entity_id)
    if battery_state is not None:
        battery_pct = _safe_int(battery_state.get("state", 0))
    else:
        # Fallback: try raw_state.batPct buried in vacuum entity attributes
        battery_pct = _safe_int(
            attrs.get("battery_level") or attrs.get("raw_state", {}).get("batPct", 0)
        )

    current_zone: str | None = attrs.get("status")

    return RobotState(
        state=robot_state,
        battery_pct=battery_pct,
        current_zone=current_zone or None,
        last_dock_at=None,  # Phase 1: not tracked from HA history
    )


async def _fetch_calendar_events(
    ha_adapter: HARestAdapter,
    now: datetime,
    window_hours: int = 2,
) -> tuple[list[CalendarEvent], bool]:
    """Fetch upcoming calendar events from all calendar.* HA entities.

    Returns (events, degraded). degraded=True if any calendar pull failed.
    Covers both Default and Perez Melgar Family calendars (and any others).

    Standing rule per reference_friday_checklist.md: BOTH calendars must be
    pulled. This implementation enumerates all calendar.* entities so it
    naturally covers all calendars including future additions.
    """
    calendar_entities = await ha_adapter.list_calendar_entities()
    if not calendar_entities:
        log.warning("no_calendar_entities_found")
        return [], True

    end = now + timedelta(hours=window_hours)
    events: list[CalendarEvent] = []
    degraded = False

    for entity_id in calendar_entities:
        raw_events = await ha_adapter.get_calendar_events(entity_id, now, end)
        for ev in raw_events:
            try:
                # HA calendar events have summary, start.dateTime / start.date
                start_str = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
                end_str = ev.get("end", {}).get("dateTime") or ev.get("end", {}).get("date")
                title = ev.get("summary", ev.get("title", ""))

                if not start_str or not title:
                    continue

                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=UTC)

                end_dt = (
                    datetime.fromisoformat(end_str.replace("Z", "+00:00")) if end_str else start_dt
                )
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=UTC)

                events.append(
                    CalendarEvent(
                        title=title,
                        start=start_dt,
                        end=end_dt,
                        calendar_id=entity_id,
                    )
                )
            except Exception as exc:
                log.warning("calendar_event_parse_failed", entity_id=entity_id, error=str(exc))
                degraded = True

    events.sort(key=lambda e: e.start)
    return events, degraded


async def build_snapshot(
    tick_id: str,
    ha_adapter: HARestAdapter,
    homeops_adapter: HomeOpsAdapter,
    settings: Settings,
) -> tuple[ContextSnapshot, dict[str, bool], VacuumOpsLiveSettings]:
    """Build a ContextSnapshot for one loop tick.

    Fetches all required data, applies graceful degradation per §8.5.
    Raises if HomeOps zone scores are unavailable (caller skips tick).

    Returns:
      (ctx, unit_dry_runs, live_settings)
        unit_dry_runs is dict[robot_name → dry_run bool]. robot_name is the
          lowercased unit nickname (e.g. "ethan", "sam"). Consumed by the loop
          to compute per-robot effective dry_run.
        live_settings is every live, DB-backed kill switch — `mop_enabled`
          (mop-cadence gate) and `opportunity_actuate` (predictive patience) —
          plus `read_ok`, read together in ONE HomeOps call. Every flag is
          already fail-closed to False by HomeOpsAdapter.get_vacuumops_settings()
          on any read problem, so there is nothing further to degrade here.
      Neither is stored on ContextSnapshot (avoids coupling schema to
      dispatch/module-config concerns) — both are consumed by the loop only.

    ⚠ THE THIRD ELEMENT IS A RECORD, NOT A BOOL, and was a bare
    `live_mop_enabled: bool` until `opportunity_actuate` joined it. Widening the
    tuple instead would have made this a 4- then 5-tuple of same-typed
    positional booleans destructured at the call site — the shape where adding
    the next switch silently swaps two flags at one of them. One flag per field,
    named, and the next one costs neither a tuple slot nor a second HTTP call.
    """
    now = datetime.now(tz=UTC)

    # ── Zone scores + display metadata (HomeOps) — must succeed or tick skipped ─
    # §8.5: "HomeOps get_zone_scores fails → skip this tick entirely."
    # get_zone_data() now also returns unit_dry_runs (dict[robot → bool]).
    # The synth does NOT attach unit_dry_runs to ContextSnapshot — it is consumed
    # by the loop directly after snapshot build (passed in via get_zone_data return).
    # We store it in the return value of build_snapshot so the loop can pass it
    # to dispatch_batch without coupling dry_run state into ContextSnapshot.
    zone_scores, zone_info, unit_dry_runs = await homeops_adapter.get_zone_data()
    if not zone_scores:
        raise RuntimeError("HomeOps zone scores empty or unavailable — skipping tick")

    # ── Zone metadata (HomeOps) — optional; degraded context if unavailable ──
    # Failure does NOT skip the tick — scores are the hard dependency.
    # get_zone_metadata() logs and returns {} on failure.
    zone_metadata = await homeops_adapter.get_zone_metadata()

    # ── Live kill switches (HomeOps, DB-backed) ───────────────────────────────
    # ONE call for BOTH `mop_enabled` (mop-cadence gate) and
    # `opportunity_actuate` (predictive patience). They live in the same
    # `cortex_vacuumops_settings` row and are both needed on the same tick, so a
    # request per flag would double the per-tick call count for no extra
    # freshness — and would let two flags that physically cannot disagree in the
    # DB arrive from two different instants.
    #
    # Live read every tick: mop_enabled replaced the old
    # CORTEX_VACUUMOPS_MOP_ENABLED env var and opportunity_actuate replaced a
    # static field on the job descriptors; both only ever took effect at process
    # start before. Failure does NOT skip the tick (same reasoning as
    # zone_metadata above); get_vacuumops_settings() fails closed to False on
    # every unreachable/malformed/missing-field case, so there is nothing
    # further to degrade here — the record is already safe. It also reports
    # `read_ok`, which is how the opportunity rule distinguishes "switched off"
    # from "could not ask".
    live_settings = await homeops_adapter.get_vacuumops_settings()

    # ── Home context ──────────────────────────────────────────────────────────
    home_state = await ha_adapter.get_entity_state("sensor.home_context")
    home: dict = {}
    degraded = False
    if home_state is None:
        degraded = True
        log.warning("home_context_unavailable")
    else:
        try:
            raw = home_state.get("attributes", {})
            home = {k: v for k, v in raw.items()}
        except Exception:
            degraded = True

    # ── Presence breakdown — parsed from sensor.home_context (spec §2) ────────
    # who_home is friendly-name Title Case (["Carlos","Elena"]) per the HA template.
    # home_count == -1 is the "unknown" sentinel (degraded/missing) — fail-closed in gate.
    # home_empty is only True on an explicit 0; unknown → False (belt-and-suspenders).
    if home:
        home_count = _safe_int(home.get("home_count"), default=-1)
        raw_who = home.get("who_home")
        who_home = list(raw_who) if isinstance(raw_who, list) else []
    else:
        home_count = -1
        who_home = []
    home_empty = home_count == 0

    # ── People ────────────────────────────────────────────────────────────────
    people: dict[str, PersonActivity] = {}
    for name in _PEOPLE:
        try:
            people[name] = await _fetch_person_activity(ha_adapter, name)
        except Exception as exc:
            log.warning("person_activity_fetch_failed", name=name, error=str(exc))
            people[name] = PersonActivity(activity="unknown", confidence=0.0)
            degraded = True

    # ── Rooms ─────────────────────────────────────────────────────────────────
    # Every room in _TRACKED_ROOMS must be present in ctx.rooms so Jinja2
    # templates (e.g. {{ ctx.rooms.loft.detected }}) never hit StrictUndefined.
    # Rooms without HA sensors (e.g. 3F: loft, office, gym) previously returned
    # None from _fetch_room_activity and were silently skipped — now they get a
    # safe default so the template layer always has a complete mapping.
    # occupancy_available=False on the default: a room with no HA sensor must read
    # as "no signal", not as "empty". zone_active_use_check falls through to the
    # floor rollup for these rather than passing them as clear.
    _room_default = RoomActivity(
        detected="unknown", confidence=0.0, raw_occupancy=False, occupancy_available=False
    )
    rooms: dict[str, RoomActivity] = {}
    for room in _TRACKED_ROOMS:
        try:
            room_activity = await _fetch_room_activity(ha_adapter, room)
            rooms[room] = room_activity if room_activity is not None else _room_default
        except Exception as exc:
            log.warning("room_activity_fetch_failed", room=room, error=str(exc))
            rooms[room] = _room_default
            degraded = True

    # ── Occupancy — floor rollups + per-zone designated sensors ───────────────
    # Read as dedicated entities rather than derived from ctx.rooms. Both feed the
    # R1 occupancy precedence chain (zone sensor → room sensor → floor rollup) and
    # both carry last_changed so the gate can require a confirmation window before
    # trusting a fresh flip to "off".
    try:
        floor_occupancy = await _fetch_floor_occupancy(ha_adapter)
    except Exception as exc:
        log.warning("floor_occupancy_fetch_failed", error=str(exc))
        floor_occupancy = {}
        degraded = True

    try:
        occupancy_readings = await _fetch_occupancy_readings(ha_adapter, zone_metadata)
    except Exception as exc:
        log.warning("zone_occupancy_fetch_failed", error=str(exc))
        occupancy_readings = {}
        degraded = True

    # ── Entry gates — per-zone designated gate entities ───────────────────────
    # Read by entity id off ZoneMeta.entry_gate_entity, deduped across zones that
    # share a gate. A blanket failure here leaves gate_readings empty, which
    # entry_gate_check reads as "unresolved" for every gated zone and blocks them
    # — the intended direction. It must never read as "no gate configured".
    try:
        gate_readings = await _fetch_gate_readings(ha_adapter, zone_metadata)
    except Exception as exc:
        log.warning("entry_gate_fetch_failed", error=str(exc))
        gate_readings = {}
        degraded = True

    # ── Robots ────────────────────────────────────────────────────────────────
    robot_states: dict[str, RobotState] = {}
    for robot in ("ethan", "sam", "saros"):
        try:
            robot_states[robot] = await _fetch_robot_state(ha_adapter, robot)
        except Exception as exc:
            log.warning("robot_state_fetch_failed", robot=robot, error=str(exc))
            robot_states[robot] = RobotState(state="error", battery_pct=0)
            degraded = True

    # ── Calendar events ───────────────────────────────────────────────────────
    calendar_degraded = False
    try:
        upcoming_events, calendar_degraded = await _fetch_calendar_events(ha_adapter, now)
    except Exception as exc:
        log.warning("calendar_fetch_failed", error=str(exc))
        upcoming_events = []
        calendar_degraded = True

    # ── Quiet-hours flags ─────────────────────────────────────────────────────
    # These are two DIFFERENT quantities and are sourced separately. They used
    # to be the same value (both `= _hc_quiet`), which meant 1F could not be
    # relaxed overnight without also relaxing 2F.
    #
    # quiet_hours_2f — sensor.home_context.attributes.quiet_hours, unchanged.
    #   home_context stays the canonical authority for the household quiet-hours
    #   convention (`hour >= 22 or hour < 7`); reading it rather than recomputing
    #   avoids CORTEX time-window drift from HA's own logic (confirmed
    #   divergence: home_context.quiet_hours=false while CORTEX computed
    #   quiet_hours_2f=true at 9:22 PM, blocking a 100-dirt dispatch).
    #   Degraded case (home={}): defaults to False — fail-open is acceptable;
    #   home_context unavailability is already marked ctx.degraded=True above.
    #
    # quiet_hours_1f — a 1F-local courtesy window, 22:00-23:00 PST, computed
    #   from the tick clock. Deliberately far shorter than the household window:
    #   from 23:00 the ground floor is measured empty, and noise_budget()'s
    #   floor-aware sleep tier (1F ×0.80) is already the right model for
    #   ground-floor noise during the household sleep window. See
    #   utils.is_quiet_hours_1f for the full rationale and the measurements.
    #   Being clock-derived, this flag does not degrade when home_context is
    #   unavailable — strictly more robust than the aliased value it replaces.
    quiet_hours_2f = bool(home.get("quiet_hours", False))
    quiet_hours_1f = is_quiet_hours_1f(now)

    # ── Assemble snapshot ─────────────────────────────────────────────────────
    ctx = ContextSnapshot(
        timestamp=now,
        tick_id=tick_id,
        home=home,
        people=people,
        rooms=rooms,
        zone_scores=zone_scores,
        zone_info=zone_info,
        zone_metadata=zone_metadata,
        occupancy_readings=occupancy_readings,
        floor_occupancy=floor_occupancy,
        gate_readings=gate_readings,
        upcoming_events=upcoming_events,
        robot_states=robot_states,
        quiet_hours_1f=quiet_hours_1f,
        quiet_hours_2f=quiet_hours_2f,
        degraded=degraded,
        calendar_degraded=calendar_degraded,
        # Presence breakdown (spec §2) — derived above from sensor.home_context attributes.
        home_count=home_count,
        who_home=who_home,
        home_empty=home_empty,
        # occupancy_gate_bypassed / bypass_reason are per-zone; set in evaluate_zone, not here.
    )

    # Compute noise_budget once for snapshot (§6.3 — so R0/R1/L1 don't recompute
    # independently). "2F" is the conservative floor default for this snapshot-level
    # cache; callers that know the operating floor pass job.floor directly.
    ctx.noise_budget = noise_budget(ctx, "2F")

    if degraded:
        log.warning("snapshot_degraded", tick_id=tick_id)
    else:
        log.debug("snapshot_built", tick_id=tick_id, zone_count=len(zone_scores))

    return ctx, unit_dry_runs, live_settings
