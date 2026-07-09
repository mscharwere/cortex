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
  - quiet_hours_1f / quiet_hours_2f — derived flags

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
    PersonActivity,
    RobotState,
    RoomActivity,
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

# Quiet-hours windows (PST — compared after converting ctx.timestamp to PST)
_QUIET_HOURS_1F_START = 22  # 10 PM
_QUIET_HOURS_1F_END = 7  # 7 AM
_QUIET_HOURS_2F_START = 21  # 9 PM
_QUIET_HOURS_2F_END = 8  # 8 AM


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


def _is_quiet_1f(now_pst_hour: int) -> bool:
    """True if current PST hour is within quiet 1F window (10 PM – 7 AM)."""
    return now_pst_hour >= _QUIET_HOURS_1F_START or now_pst_hour < _QUIET_HOURS_1F_END


def _is_quiet_2f(now_pst_hour: int) -> bool:
    """True if current PST hour is within quiet 2F window (9 PM – 8 AM)."""
    return now_pst_hour >= _QUIET_HOURS_2F_START or now_pst_hour < _QUIET_HOURS_2F_END


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

    if occ_state is None and act_state is None:
        return None  # Both unavailable — skip this room

    raw_occupancy = False
    if occ_state is not None:
        raw_occupancy = occ_state.get("state", "off").lower() in ("on", "true", "1")

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

    # Door sensor — binary_sensor.{room}_door (None if unavailable)
    door_open: bool | None = None
    door_state = await ha_adapter.get_entity_state(f"binary_sensor.{room}_door")
    if door_state is not None and door_state.get("state") not in ("unavailable", "unknown", None):
        door_open = door_state.get("state", "off").lower() in ("on", "true", "open")

    return RoomActivity(
        detected=detected,
        confidence=confidence,
        raw_occupancy=raw_occupancy,
        door_open=door_open,
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
) -> tuple[ContextSnapshot, dict[str, bool]]:
    """Build a ContextSnapshot for one loop tick.

    Fetches all required data, applies graceful degradation per §8.5.
    Raises if HomeOps zone scores are unavailable (caller skips tick).

    Returns:
      (ctx, unit_dry_runs) where unit_dry_runs is dict[robot_name → dry_run bool].
      robot_name is the lowercased unit nickname (e.g. "ethan", "sam").
      unit_dry_runs is consumed by the loop to compute per-robot effective dry_run;
      it is NOT stored on ContextSnapshot (avoids coupling schema to dispatch concerns).
    """
    now = datetime.now(tz=UTC)

    # PST hour for quiet-hours flags
    import pytz

    pst = pytz.timezone("America/Los_Angeles")
    now_pst_hour = now.astimezone(pst).hour

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
    rooms: dict[str, RoomActivity] = {}
    for room in _TRACKED_ROOMS:
        try:
            room_activity = await _fetch_room_activity(ha_adapter, room)
            if room_activity is not None:
                rooms[room] = room_activity
        except Exception as exc:
            log.warning("room_activity_fetch_failed", room=room, error=str(exc))
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
    quiet_hours_1f = _is_quiet_1f(now_pst_hour)
    quiet_hours_2f = _is_quiet_2f(now_pst_hour)

    # Check if any 2F bedroom is currently sleeping
    sleep_rooms = ("master_bedroom", "carlos_room", "upper_hall")
    for room_key in sleep_rooms:
        room_act = rooms.get(room_key)
        if room_act and room_act.detected == "sleeping":
            quiet_hours_2f = True
            break

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

    return ctx, unit_dry_runs
