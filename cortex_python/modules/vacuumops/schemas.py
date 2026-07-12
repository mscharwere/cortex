"""VacuumOps data schemas — ContextSnapshot, PersonActivity, RoomActivity,
RobotState, CalendarEvent, ZoneDecisionDetail, DecisionEntry, and internal
ZoneOutcome / BatchEntry types.

Spec: C:/Jarvis/Team/TARS/cortex_vacuumops_module_spec.md §3 + §7.4
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

# ── Public context types (§3) ────────────────────────────────────────────────


@dataclass
class ZoneMeta:
    """Per-zone structural metadata fetched from HomeOps each tick."""

    zone_id: int
    unit_id: int
    floor_type: str | None = None
    debris_profile: list[str] = field(default_factory=list)
    contained_by: int | None = None
    dispatchable: bool = True
    low_disruption: bool = False
    # NEW (spec §3): CORTEX Override 2 eligibility — zone is non-disruptive to a single
    # sole (non-Elena) occupant when the zone's own room is unoccupied.
    occupancy_sensor: str | None = None
    # NEW (spec §1.1): HA room-level occupancy sensor for the zone's parent room.
    # Already exists in HomeOps DB/API (migration 014); was previously unused by CORTEX.
    # Used in Override 2 to resolve floor-level → room-level occupancy check.
    child_zones: list[int] = field(default_factory=list)
    # child_zones: reverse index of contained_by — computed by homeops_adapter each tick


@dataclass
class PersonActivity:
    """Per-person activity rollup. One instance per family member tracked.

    Source: sensor.<name>_activity (HA, Bayesian person-state composites).
    """

    activity: str
    # "exercising" | "home_idle" | "away" | "sleeping" | "school" | "unknown"
    confidence: float  # 0.0–1.0 from the Bayesian sensor's "probability" attribute
    piano: bool | None = None
    # Elena ONLY — sensor.elena_activity 'piano' attribute (live piano sensor)
    sleep_confidence: float | None = None
    # Kids ONLY — sensor.<kid>_activity 'sleep_confidence' attribute


@dataclass
class RoomActivity:
    """Per-room activity rollup.

    Source: sensor.<room>_detected_activity (HA, Bayesian room-state) +
    binary_sensor.<room>_occupancy_status.
    """

    detected: str
    # "cooking" | "eating" | "sleeping" | "idle" | "active" | "unknown"
    confidence: float  # 0.0–1.0 from the detected_activity sensor
    raw_occupancy: bool
    # binary_sensor.<room>_occupancy_status — instantaneous mmWave/Bayes occupancy.
    # Apply 90s grace period before treating False as "truly clear".
    door_open: bool | None = None
    # binary_sensor.{room}_door state. None = sensor unavailable → treat as open.


@dataclass
class RobotState:
    """Per-robot status snapshot.

    Source: vacuum.<robot> state + battery + status attributes.
    """

    state: str
    # "docked" | "cleaning" | "returning" | "error" | "paused" | "idle"
    battery_pct: int  # 0–100 from attributes.battery_level
    current_zone: str | None = None
    # attributes.status parsed (or None when docked)
    last_dock_at: datetime | None = None
    # last docked timestamp from HA history (used for cooldown)


@dataclass
class CalendarEvent:
    """Upcoming event within the next ~2h window.

    Source: calendar.* entities (M365 calendars — Default + Perez Melgar
    Family at minimum; Carlos's standing rule applies — both calendars must
    be pulled, never just Default).
    """

    title: str
    start: datetime
    end: datetime
    calendar_id: str  # which calendar entity it came from (debug + provenance)
    owner: str | None = None
    # parsed where determinable: "carlos" | "elena" | "family" | etc.


@dataclass
class ZoneInfo:
    """Per-zone display and routing metadata. Built from /api/vacuum/units each tick."""

    label: str  # raw HomeOps zone label, e.g. "Litter Box"
    display: str  # "{floor} {label}" for UI, e.g. "3F Litter Box"
    unit_id: int
    floor: str  # operating floor: "1F" | "2F" | "3F"
    room_key: str | None  # snake_case ctx.rooms key; None for sub-zones (Litter Box, etc.)


@dataclass
class ContextSnapshot:
    """The single object that R0/R1/L1 all reason over for one loop tick.

    Assembled by synth/vacuumops_synth.py once per loop tick.
    Cached in Redis for the duration of one tick only
    (key: cortex:vacuumops:ctx:<tick_id>, TTL 60s).
    """

    timestamp: datetime  # tick start time, UTC; render as PST in human-facing log
    tick_id: str  # uuid — for joining R0/R1/L1/dispatch rows in decision_log

    # Home-level
    home: dict  # parsed JSON from sensor.home_context (presence rollup, mode, etc.)

    # Per-person
    people: dict[str, PersonActivity]
    # keys: "carlos" | "elena" | "carlitos" | "daniel" | "iestaf"

    # Per-room
    rooms: dict[str, RoomActivity]
    # keys: "kitchen" | "living_room" | "master_bedroom" | "carlitos_room"
    # (Phase 1: these four. Phase 2 expands as Sam 2F module needs them.)

    # Zone scores — read from HomeOps, NOT cached longer than one tick (D1)
    zone_scores: dict[int, float]
    # keys: zone_id (int), e.g. 14 → 78.3

    # Upcoming events (calendar)
    upcoming_events: list[CalendarEvent]
    # next 2h window, sorted by start ascending

    # Robot state
    robot_states: dict[str, RobotState]
    # keys: "ethan" | "sam"

    # Zone display/routing metadata — built from /api/vacuum/units each tick
    zone_info: dict[int, ZoneInfo] = field(default_factory=dict)
    # keys: zone_id (int). label, display, floor, room_key. Built by homeops_adapter.

    # Zone metadata — structural per-zone data fetched from HomeOps each tick
    zone_metadata: dict[int, ZoneMeta] = field(default_factory=dict)
    # keys: zone_id (int). Built by homeops_adapter each tick.

    # Derived (computed once when snapshot is built)
    noise_budget: float | None = None
    # 0–10 scale; computed by noise model (§6); None until noise step runs
    quiet_hours_2f: bool | None = None
    # True if any 2F sleep zone occupied + in 9pm–8am
    quiet_hours_1f: bool | None = None
    # True in 10pm–7am window

    # Presence fields — parsed from sensor.home_context attributes (spec §2)
    # Populated by synth; consumed by R1 occupancy-gate overrides.
    home_count: int = -1
    # -1 = unknown / degraded (fail-closed in the gate). 0 = empty house. ≥1 = occupied.
    who_home: list[str] = field(default_factory=list)
    # Friendly-name Title Case (["Carlos","Elena"]) — as returned by sensor.home_context.
    # Elena carve-out in Override 2 compares case-insensitively against "elena".
    home_empty: bool = False
    # True iff home_count == 0 (known empty). Unknown → False (fail-closed).

    # Degraded-context flags (§8.5)
    degraded: bool = False
    # True if HA WS was down and snapshot used cached data
    calendar_degraded: bool = False
    # True if calendar pull failed; upcoming_events will be []


# ── Decision log types (§7.4) ─────────────────────────────────────────────────


@dataclass
class ZoneDecisionDetail:
    """Per-zone detail within a DecisionEntry. Spec §7.4."""

    label: str  # e.g. "Litter Box"
    score: float  # 0–100, zone score at evaluation time
    bundled: bool
    # True if included via bundle threshold (D11), NOT independently above
    # dispatch_threshold
    l1_confidence: float | None = None
    # per-zone L1 confidence if L1 was reached; None for R0/R1 deferrals and
    # for bundled zones (D11)
    display: str = ""  # floor-prefixed label, e.g. "3F Litter Box"
    result: str | None = None  # "dispatch" | "bundled" | "defer" | None (unset)
    gate_failed: str | None = None  # non-null when result=="defer"
    gate_reason: str | None = None  # zo.reason when result=="defer"
    # L1 details — only populated when this zone reached L1
    l1_decision: str | None = None  # "dispatch" | "defer"
    l1_reason: str | None = None
    l1_defer_until_hint: str | None = None
    l1_passes: str | None = None  # "auto" | "one" | "two"
    l1_intensity: str | None = None  # "auto" | "eco" | "perf"
    l1_params_reason: str | None = None


@dataclass
class DecisionEntry:
    """Batch-scoped decision log entry. One per (tick, robot). Spec §7.4."""

    tick_id: str
    timestamp: str  # ISO8601 PST
    robot: str  # "ethan" | "sam" — which robot this decision applies to
    zones: list[ZoneDecisionDetail]
    # ALL zones evaluated for this robot in this tick, regardless of outcome.
    # Each zone carries a `result` field: "dispatch" | "bundled" | "defer".
    tier_reached: str  # "R0" | "R1" | "L1" — highest tier any zone reached
    gate_failed: str | None
    # non-null on SKIP/DEFER: "r0" | "effectiveness" | "comfort"
    # | "robot_cooldown" | "l1"; null on DISPATCH
    decision: str  # "dispatch" | "skip"
    reason: str  # short string (e.g. "all_rules_pass+1_bundled", "piano_active")
    l1_confidence: float | None
    # only when tier_reached == "L1"; minimum confidence across zones (worst-case)
    dry_run: bool
    dispatched_at: str | None = None  # ISO8601 PST; null on SKIP


# ── Internal loop types ───────────────────────────────────────────────────────


@dataclass
class ZoneOutcome:
    """Internal per-zone evaluation result. Not persisted directly.

    loop.py collects these and assembles them into DecisionEntry + BatchEntry.
    """

    zone: int  # zone_id
    action: Literal["dispatch", "defer"]
    tier: str  # "R0" | "R1" | "L1"
    gate_failed: str  # "r0" | "effectiveness" | "comfort" | "robot_cooldown" | "l1" | "none"
    reason: str
    score: float = 0.0
    l1_confidence: float | None = None


@dataclass
class DropRecord:
    """A zone dropped by containment dedup. Logged to decision_log."""

    zone_id: int
    job_id: str
    reason: str = "contained_by"
    parent_zone_id: int | None = None


@dataclass
class BatchEntry:
    """One zone in a per-robot dispatch batch. Assembled by assemble_batch().

    Passed to dispatch_batch() → HomeOps /api/vacuum/trigger.
    """

    zone: int  # zone_id
    bundled: bool  # True if included via D11 bundle threshold
    score: float  # zone score at evaluation time
    l1_confidence: float | None = None
    # None for bundled zones and R1-decided zones; populated for L1-decided zones
    passes: str = "auto"
    intensity: str = "auto"
    params_source: str = "default"
    # "l1" | "mixed" | "default" — how passes/intensity were resolved
    params_reason: str | None = None
    # 1-sentence rationale from L1; None when source == "default"
