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
  - quiet_hours_1f / quiet_hours_2f — sourced from sensor.home_context.attributes.quiet_hours

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
    OccupancyReading,
    PersonActivity,
    RobotState,
    RoomActivity,
    ZoneMeta,
)

if TYPE_CHECKING:
    from cortex_python.adapters.ha_rest_adapter import HARestAdapter
    from cortex_python.adapters.homeops_adapter import HomeOpsAdapter

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

# Explicit room → HA entity overrides for door gate sensors.
# Add an entry whenever a room's door sensor doesn't follow the
# binary_sensor.{room}_door naming convention.
_DOOR_ENTITY_MAP: dict[str, str] = {
    "carlitos_room": "binary_sensor.sam_carlitos_room_door_gate",
    "daniel_room": "binary_sensor.sam_daniel_s_room_door_gate",
    "master_bathroom": "binary_sensor.sam_master_bathroom_door_gate",
    "master_bedroom": "binary_sensor.sam_master_bedroom_door_gate",
    # 1F Bathroom (Saros zone 20). The Z-Wave device exposes NINE binary_sensors
    # for this one physical reed switch; the entity below is the Z-Wave JS
    # "Door state (simple)" collapsed binary — device_class=door, on=open —
    # which is the polarity door_open_check expects. Do NOT swap this for a
    # "...window_door_is_closed" sibling: those are INVERTED (on=closed) and
    # carry no device_class, so the gate would defer exactly when the door is
    # open. Verified against live HA 2026-08-11 (204 transitions/7d, the open-
    # and closed-family entities perfectly anti-correlated).
    "bathroom": "binary_sensor.first_level_bathroom_door_sensor",
}


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
    """Fetch RoomActivity for one room. Returns None if sensors unavailable."""
    occupancy_id = f"binary_sensor.{room}_occupancy_status"
    activity_id = f"sensor.{room}_detected_activity"

    occ_state = await ha_adapter.get_entity_state(occupancy_id)
    act_state = await ha_adapter.get_entity_state(activity_id)

    # Door sensor fetched unconditionally — must not be gated on occupancy/activity
    # availability. Rooms without occupancy sensors (e.g. Carlitos Room) would
    # otherwise bypass the door-closed gate entirely (confirmed bug: 2026-07-17).
    door_open: bool | None = None
    door_entity = _DOOR_ENTITY_MAP.get(room, f"binary_sensor.{room}_door")
    door_state = await ha_adapter.get_entity_state(door_entity)
    if door_state is not None and door_state.get("state") not in ("unavailable", "unknown", None):
        door_open = door_state.get("state", "off").lower() in ("on", "true", "open")

    if occ_state is None and act_state is None:
        # No occupancy data — but surface door state if available so the door gate fires.
        # occupancy_available stays False: raw_occupancy=False here is a placeholder,
        # not evidence the room is empty, and the gate must fall through to the floor.
        if door_open is not None:
            return RoomActivity(
                detected="unknown",
                confidence=0.0,
                raw_occupancy=False,
                door_open=door_open,
                occupancy_available=False,
            )
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
        door_open=door_open,
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
) -> tuple[ContextSnapshot, dict[str, bool], bool]:
    """Build a ContextSnapshot for one loop tick.

    Fetches all required data, applies graceful degradation per §8.5.
    Raises if HomeOps zone scores are unavailable (caller skips tick).

    Returns:
      (ctx, unit_dry_runs, live_mop_enabled)
        unit_dry_runs is dict[robot_name → dry_run bool]. robot_name is the
          lowercased unit nickname (e.g. "ethan", "sam"). Consumed by the loop
          to compute per-robot effective dry_run.
        live_mop_enabled is the mop-cadence gate's live, DB-backed kill switch
          (HomeOps cortex_vacuumops_settings, replacing CORTEX_VACUUMOPS_MOP_ENABLED).
          Already fail-closed to False by HomeOpsAdapter.get_vacuumops_mop_enabled()
          on any read problem — nothing further to degrade here.
      Neither is stored on ContextSnapshot (avoids coupling schema to
      dispatch/module-config concerns) — both are consumed by the loop only.
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

    # ── Mop-cadence gate kill switch (HomeOps, DB-backed) ─────────────────────
    # Live read every tick — replaces the old CORTEX_VACUUMOPS_MOP_ENABLED env
    # var, which only took effect at process start. Failure does NOT skip the
    # tick (same reasoning as zone_metadata above); get_vacuumops_mop_enabled()
    # fails closed to False on any unreachable/malformed/missing-field case, so
    # there is nothing further to degrade here — the bool is already safe.
    live_mop_enabled = await homeops_adapter.get_vacuumops_mop_enabled()

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
    # Both flags sourced from sensor.home_context.attributes.quiet_hours.
    # home_context is the canonical authority — avoids CORTEX time-window drift
    # from HA's own quiet-hours logic (confirmed divergence: home_context.quiet_hours=false
    # while CORTEX computed quiet_hours_2f=true at 9:22 PM, blocking a 100-dirt dispatch).
    # Degraded case (home={}): defaults to False — fail-open is acceptable; home_context
    # unavailability is already marked ctx.degraded=True above.
    _hc_quiet = bool(home.get("quiet_hours", False))
    quiet_hours_1f = _hc_quiet
    quiet_hours_2f = _hc_quiet

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

    return ctx, unit_dry_runs, live_mop_enabled
