"""VacuumOps job descriptors.

A *job* is a CORTEX-VacuumOps concept that bundles "what to clean, with which
robot, with which cleaning parameters, at which noise level, with which
decision-tier configuration." A loop tick evaluates each active job
independently.

Spec: C:/Jarvis/Team/TARS/cortex_vacuumops_module_spec.md §5
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class VacuumJob:
    """Base job descriptor. One concrete subclass per active job.

    Fields added for multi-robot fleet expansion:
      effectiveness_scope — controls which occupancy gates run in R1 (spec §7.2)
      door_check          — whether R1 runs door_open_check (R1-E4)
    """

    # Identity
    job_id: str  # stable string, e.g. "ethan_3f_litter_box"
    robot: str  # "ethan" | "sam" | "saros"
    zones: list[int]  # HomeOps zone_id values (must exist in vac_zone_cleanliness)
    floor: str = "1F"  # operating floor — used by floor_clearance_check

    # Noise model inputs (§6)
    noise_level: int = 1  # 1–5; intrinsic disruption from this robot running in these zones
    noise_radius: str = "floor"  # "local" | "floor" | "house"

    # Dispatch gating
    dispatch_threshold: float = 50.0  # zone_score > threshold → eligible
    cooldown_minutes: int = 240
    # Per-zone cooldown (D12). After this zone is included in a dispatch, skip
    # re-considering it for this long. Covers mission run + dock + post-clean
    # settle + resoil window. Independent of per-robot cooldown.
    bundle_threshold_pct: float = 0.70
    # D11 — bundle inclusion floor. A zone below dispatch_threshold may still
    # be included in a batch if its score ≥ bundle_threshold_pct × dispatch_threshold
    # AND a batch is already being assembled for this robot in this tick AND it
    # passes zone_effective + noise_acceptable. Bundle inclusion is deterministic
    # — no L1 call. Logged as bundled=True in the decision entry.

    # Cleaning parameters (passed to HomeOps /api/vacuum/trigger)
    cleaning_params: dict[str, str] = field(default_factory=dict)
    # e.g. {"passes": "auto", "intensity": "auto"} — matches HomeOps schema

    # Decision config
    prompt_file: str = ""  # relative path under cortex_python/modules/vacuumops/prompts/
    r0_checks: list[str] = field(default_factory=list)
    # names of R0 predicate functions to run
    r1_rules: list[str] = field(default_factory=list)
    # names of R1 scored rules to run
    l1_required: bool = False
    # If True, L1 always runs after R0/R1 PASS (no shortcut).
    # If False, L1 only runs when R1 produces an ambiguous result.

    effectiveness_scope: str = "floor"
    # Controls which occupancy gates run in R1 (spec §7.2):
    #   "floor"     → zone_active_use_check + floor_clearance_check (default)
    #   "room_only" → zone_active_use_check only (no floor-wide check; per-room model)
    #   "none"      → skip both (zone dispatches regardless of floor/room occupancy)

    door_check: bool = False
    # If True, R1 runs door_open_check: reads room.door_open from ContextSnapshot.
    # Graceful degradation: door_open=None (sensor missing) → treat as open → PASS.

    occupancy_clear_grace_s: int = 120
    # Confirmation window (seconds) an occupancy sensor must have been reporting
    # "off" before the gate will trust it as genuinely clear. ONE-DIRECTIONAL:
    # it delays *clearing* only. A flip TO occupied blocks dispatch on the very
    # next tick with zero added latency.
    #
    # Why: on 2026-08-31 the Saros dispatched into occupied 1F rooms three times
    # (10:07:19, 14:06:47, 18:34:34 PST). Every one landed 2–90s after an
    # occupancy sensor flipped off, and every one saw it flip back on 26–90s
    # later — e.g. living_room went off at 18:33:38, dispatch fired at 18:34:34,
    # sensor back on at 18:35:01 (87s of "off" in total). The gate was reading an
    # instantaneous state with no confirmation window, so a person pausing
    # between mmWave detections read as an empty room. 120s clears all three.
    # 0 disables the window (instantaneous trust — pre-2026-08-31 behaviour).

    # ── Mop-cadence gate (mop.py) ────────────────────────────────────────────
    # Locked design, 2026-07-03 (D14–D18): "Mop intelligence (Saros only):
    # signal → schedule (7-day) → score threshold → off. Intensity: light/deep."
    #
    # The mop is a MODIFIER on a vacuum dispatch, not a separate job: the robot
    # physically cannot mop without driving the same segments it vacuums, so a
    # separate mop job would double-dispatch and fight the per-robot cooldown.
    # These fields therefore tune an existing job's wet/dry behaviour.
    mop_enabled: bool = False
    # Master switch. False → this job never mops (Braava and the iRobot units are
    # excluded from the CORTEX mop model entirely; the HomeOps route ignores mop
    # fields for non-Roborock kinds anyway).
    mop_cadence_days: float = 7.0
    # Schedule arm: mop when this many days have elapsed since last_mopped_at.
    mop_score_threshold: float = 80.0
    # Score arm: a zone this dirty earns a wet pass regardless of the schedule.
    # Deliberately well above dispatch_threshold (50) — a routine vacuum-eligible
    # zone should not trigger a mop on score alone.
    mop_deep_after_days: float = 14.0
    # Intensity arm: a zone overdue by this much gets a deep mop rather than light.


@dataclass
class Ethan3FLitterBoxJob(VacuumJob):
    """Ethan (j9+, 3F) Litter Box — Petivity-signal-driven.

    effectiveness_scope="none": dispatches regardless of 3F occupancy.
    The litter box is a contained zid zone; Carlos explicitly wants it
    to run even when someone is on 3F.
    """

    job_id: str = "ethan_3f_litter_box"
    robot: str = "ethan"
    zones: list[int] = field(default_factory=lambda: [14])
    floor: str = "3F"
    effectiveness_scope: str = "none"
    noise_level: int = 1
    noise_radius: str = "floor"
    dispatch_threshold: float = 50.0
    cooldown_minutes: int = 240
    cleaning_params: dict[str, str] = field(
        default_factory=lambda: {"passes": "auto", "intensity": "auto"}
    )
    prompt_file: str = "prompts/litter_box.md"
    r0_checks: list[str] = field(
        default_factory=lambda: [
            "robot_docked",
            "battery_above_30",
            "score_above_threshold",
            "not_in_active_mission",
            "not_in_cooldown",
        ]
    )
    r1_rules: list[str] = field(
        default_factory=lambda: [
            "transit_pattern_lookahead",
            "noise_budget_check",
            "noise_radius_check",
        ]
    )
    l1_required: bool = True


@dataclass
class Ethan3FRoomsJob(VacuumJob):
    """Ethan (j9+, 3F) room zones — Loft, Office, Gym — decay-driven.

    Full floor clearance: defers when anyone is on 3F.
    """

    job_id: str = "ethan_3f_rooms"
    occupancy_clear_grace_s: int = 120
    robot: str = "ethan"
    zones: list[int] = field(default_factory=lambda: [15, 16, 17])
    floor: str = "3F"
    effectiveness_scope: str = "floor"
    noise_level: int = 2
    noise_radius: str = "floor"
    dispatch_threshold: float = 50.0
    cooldown_minutes: int = 120
    cleaning_params: dict[str, str] = field(
        default_factory=lambda: {"passes": "auto", "intensity": "auto"}
    )
    prompt_file: str = "prompts/ethan_3f_rooms.md"
    r0_checks: list[str] = field(
        default_factory=lambda: [
            "robot_docked",
            "battery_above_30",
            "score_above_threshold",
            "not_in_active_mission",
            "not_in_cooldown",
        ]
    )
    r1_rules: list[str] = field(
        default_factory=lambda: [
            "zone_active_use_check",
            "floor_clearance_check",
            "transit_pattern_lookahead",
            "noise_budget_check",
            "noise_radius_check",
        ]
    )
    l1_required: bool = False


@dataclass
class Saros1FLitterBoxJob(VacuumJob):
    """Saros 10R (1F) Litter Box — Petivity-signal-driven.

    Full floor clearance: defers when anyone is on 1F.

    Vacuum-only by design: mop_enabled stays False. Dragging a wet pad through
    litter scatter makes a paste and fouls the pad for the rest of the mission,
    which would then be smeared across the room zones. The original design
    scoped the mop model to room zones for this reason.
    """

    job_id: str = "saros_1f_litter_box"
    occupancy_clear_grace_s: int = 120
    robot: str = "saros"
    zones: list[int] = field(default_factory=lambda: [23])
    floor: str = "1F"
    effectiveness_scope: str = "floor"
    noise_level: int = 1
    noise_radius: str = "floor"
    dispatch_threshold: float = 50.0
    cooldown_minutes: int = 240
    cleaning_params: dict[str, str] = field(default_factory=dict)
    prompt_file: str = "prompts/saros_1f_litter_box.md"
    r0_checks: list[str] = field(
        default_factory=lambda: [
            "robot_docked",
            "battery_above_30",
            "score_above_threshold",
            "not_in_active_mission",
            "not_in_cooldown",
        ]
    )
    r1_rules: list[str] = field(
        default_factory=lambda: [
            "zone_active_use_check",
            "floor_clearance_check",
            "transit_pattern_lookahead",
            "noise_budget_check",
            "noise_radius_check",
        ]
    )
    l1_required: bool = True


@dataclass
class Saros1FRoomsJob(VacuumJob):
    """Saros 10R (1F) room zones — decay-driven.

    Full floor clearance: defers when anyone is on 1F.

    This is the ONLY job with the mop gate enabled. Per the locked 2026-07-03
    design the mop model is Saros-only, and within the Saros it is room-zones
    only — the litter box stays vacuum-only (see Saros1FLitterBoxJob).

    door_check=True gates the Bathroom (zone 20), which has a real door that is
    routinely shut — dispatching into it is mechanically futile. The flag is
    job-wide but structurally affects the Bathroom only: door_open_check
    no-ops to "treat as open" for any zone whose room_key is None (Prep Area)
    or whose room has no mapped/resolvable door entity (Kitchen, Living Room,
    Hallway, Dining Table — none of these has a binary_sensor.{room}_door in HA).
    """

    job_id: str = "saros_1f_rooms"
    occupancy_clear_grace_s: int = 120
    robot: str = "saros"
    door_check: bool = True
    mop_enabled: bool = True
    mop_cadence_days: float = 7.0
    mop_score_threshold: float = 80.0
    mop_deep_after_days: float = 14.0
    zones: list[int] = field(
        default_factory=lambda: [19, 20, 21, 22, 24, 25]
        # Kitchen=19, Bathroom=20, Living Room=21, Hallway=22, Prep Area=24, Dining Table=25
    )
    floor: str = "1F"
    effectiveness_scope: str = "floor"
    noise_level: int = 3
    noise_radius: str = "floor"
    dispatch_threshold: float = 50.0
    cooldown_minutes: int = 120
    cleaning_params: dict[str, str] = field(default_factory=dict)
    prompt_file: str = "prompts/saros_1f_rooms.md"
    r0_checks: list[str] = field(
        default_factory=lambda: [
            "robot_docked",
            "battery_above_30",
            "score_above_threshold",
            "not_in_active_mission",
            "not_in_cooldown",
        ]
    )
    r1_rules: list[str] = field(
        default_factory=lambda: [
            "zone_active_use_check",
            "floor_clearance_check",
            "door_open_check",
            "transit_pattern_lookahead",
            "noise_budget_check",
            "noise_radius_check",
        ]
    )
    l1_required: bool = False


@dataclass
class Sam2FJob(VacuumJob):
    """Sam (j7+, 2F) room zones — per-room model with door sensor check.

    effectiveness_scope="room_only": each zone evaluated independently on its
    own room occupancy only — no floor-wide block. Door sensors gate entry.
    Quiet hours (9pm–8am) already collapse noise_budget near zero.
    """

    job_id: str = "sam_2f_rooms"
    occupancy_clear_grace_s: int = 120
    robot: str = "sam"
    zones: list[int] = field(
        default_factory=lambda: [1, 2, 3, 4, 5, 6]
        # Master Bathroom=1, Master Bedroom=2, Upper Hallway=3,
        # Carlitos Room=4, Kids Table Area=5, Daniel's Room=6
    )
    floor: str = "2F"
    effectiveness_scope: str = "room_only"
    door_check: bool = True
    noise_level: int = 4
    noise_radius: str = "house"
    dispatch_threshold: float = 50.0
    cooldown_minutes: int = 120
    cleaning_params: dict[str, str] = field(default_factory=dict)
    prompt_file: str = "prompts/sam_2f_rooms.md"
    r0_checks: list[str] = field(
        default_factory=lambda: [
            "robot_docked",
            "battery_above_30",
            "score_above_threshold",
            "not_in_active_mission",
            "not_in_cooldown",
        ]
    )
    r1_rules: list[str] = field(
        default_factory=lambda: [
            "zone_active_use_check",
            "door_open_check",
            "transit_pattern_lookahead",
            "noise_budget_check",
            "noise_radius_check",
        ]
    )
    l1_required: bool = False


# Backward-compat alias — tests and prompts may still reference the old name.
LitterBoxJob = Ethan3FLitterBoxJob
