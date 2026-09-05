"""VacuumOps R1 — two-gate rule tier.

Two rule groups per the two-gate model (§6):
  1. Effectiveness rules — hard gates. Any FAIL short-circuits; L1 is NEVER
     invoked on an effectiveness failure (robot in the way is not an LLM call).
  2. Comfort rules — soft gates. AMBIGUOUS / PASS-marginal escalates to L1.
     Hard FAIL still defers without L1.

Plus D12 gate: per_robot_cooldown_check (Redis EXISTS), checked first.

Each rule returns (result: str, gate_failed: str, reason: str).
  result ∈ {"PASS", "FAIL", "AMBIGUOUS"}
  gate_failed: "effectiveness" | "comfort" | "robot_cooldown" | "none"
  reason: short human-readable string

Entry point: run_r1(job, zone, ctx, redis_client) → (result, gate_failed, reason)

Spec: C:/Jarvis/Team/TARS/cortex_vacuumops_module_spec.md §7.2
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis
import structlog

from cortex_python.modules.vacuumops.config import VacuumOpsConfig
from cortex_python.modules.vacuumops.jobs import VacuumJob
from cortex_python.modules.vacuumops.noise import FLOOR_ROOM_MAP, noise_budget, noise_impact
from cortex_python.modules.vacuumops.opportunity import (
    IMPATIENT,
    duration_estimate,
    format_opportunity,
    forward_slot_keys,
    lookahead_slot_count,
    opportunity,
    over_threshold_since_key,
    patience,
    patience_band,
    required_slot_count,
)
from cortex_python.modules.vacuumops.priors import (
    CONFIDENCE_GOOD,
    CONFIDENCE_UNAVAILABLE,
    HOUSEHOLD_TZ,
    PriorObservation,
    SlotPrior,
)
from cortex_python.modules.vacuumops.schemas import (
    ContextSnapshot,
    OccupancyReading,
    OpportunityRead,
    ZoneMeta,
)

log = structlog.get_logger()

# Redis key for per-robot cooldown (D12).
# cortex:vacuumops:robot_cooldown:<robot>
_ROBOT_COOLDOWN_KEY = "cortex:vacuumops:robot_cooldown:{robot}"

# Threshold for PASS-strong vs. PASS-marginal (→ AMBIGUOUS) in noise_budget_check.
# If impact ≤ budget * 0.7 → PASS-strong; if impact ≤ budget → PASS-marginal.
_NOISE_STRONG_PASS_FACTOR = 0.7

# Active room states that block zone_active_use_check
_ACTIVE_ROOM_STATES = {"active", "cooking", "eating", "transit"}

# Noise-sensitive room activities for noise_radius_check
_NOISE_SENSITIVE_ACTIVITIES = {"sleeping"}

# Elena friendly-name key for Override 2 carve-out (compared case-insensitively).
# person.elena friendly_name confirmed == "Elena" (spec §10 Q2); case-insensitive
# comparison is belt-and-suspenders per spec §2.1.
_ELENA = "elena"

# Explicit sensor-entity → room-key overrides for sensors that don't follow
# the {room}_sensor_group or {room}_occupancy_status naming convention.
# Keep small — only add entries as needed.
_SENSOR_ENTITY_TO_ROOM: dict[str, str] = {
    "binary_sensor.first_level_bathroom_tri_sensor_motion_detection": "bathroom",
}


# ── D12: Per-robot cooldown gate ──────────────────────────────────────────────


async def per_robot_cooldown_check(
    job: VacuumJob, zone_id: int, ctx: ContextSnapshot, redis_client: aioredis.Redis
) -> tuple[str, str, str]:
    """D12: Per-robot cooldown must not be active.

    Redis key: cortex:vacuumops:robot_cooldown:<robot>
    Set on every dispatch (single or batch); checked before any rule evaluation.
    zone_id is accepted for signature consistency with other R1 rules but is unused —
    this check is robot-level, not zone-level.
    """
    key = _ROBOT_COOLDOWN_KEY.format(robot=job.robot)
    exists = await redis_client.exists(key)
    if exists:
        ttl = await redis_client.ttl(key)
        return (
            "FAIL",
            "robot_cooldown",
            f"robot_cooldown_active:{job.robot}:ttl={ttl}s",
        )
    return "PASS", "none", "robot_cooldown_clear"


# ── Occupancy confirmation window (spec §6.7, added 2026-08-31) ──────────────


def _as_utc(dt: datetime) -> datetime:
    """Normalize a datetime to aware-UTC. Naive values are assumed UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def occupancy_state(
    occupied: bool,
    last_changed: datetime | None,
    now: datetime,
    grace_s: int,
) -> tuple[str, float | None]:
    """Classify an occupancy reading into occupied / clearing / clear.

    Returns (state, seconds_since_change):
      "occupied" → sensor reads on. BLOCKS IMMEDIATELY — the grace window is
                   deliberately one-directional and never delays this verdict.
      "clearing" → sensor reads off, but flipped off less than grace_s ago.
                   Treated as still-occupied for gating: an mmWave sensor that
                   just dropped is far more likely to be a person holding still
                   than a room that actually emptied.
      "clear"    → sensor reads off and has held off for at least grace_s.

    last_changed=None means the dwell time is unknown (sensor absent, or a
    RoomActivity synthesized without a timestamp). That degrades to "clear"
    rather than "clearing" so a missing timestamp is never *more* restrictive
    than the pre-grace behaviour — availability, not dwell, is the field that
    guards against a missing sensor.
    """
    if occupied:
        return "occupied", None
    if last_changed is None or grace_s <= 0:
        return "clear", None
    elapsed = (_as_utc(now) - _as_utc(last_changed)).total_seconds()
    if elapsed < grace_s:
        return "clearing", elapsed
    return "clear", elapsed


def _fmt_dwell(elapsed: float | None) -> str:
    """Render a dwell time for a reason string, truncated rather than rounded.

    `f"{119.6:.0f}"` renders "120", which makes the log read
    `clear_for=120s<120s` — a comparison that looks false while being true.
    Truncating toward zero keeps the printed value on the same side of the
    threshold as the comparison that produced it: 119.6s prints as "119s".

    Truncation (not floor) so a small negative elapsed from clock skew prints
    as "0" rather than "-1"; either way the printed value stays below grace.
    """
    if elapsed is None:
        return "?"
    return str(int(elapsed))


def _floor_reading(job: VacuumJob, ctx: ContextSnapshot) -> OccupancyReading | None:
    """The dedicated area_occupancy rollup for the job's operating floor, if usable."""
    reading = ctx.floor_occupancy.get(job.floor)
    if reading is None or not reading.available:
        return None
    return reading


# ── Shared occupancy resolution chain (spec §6.7) ─────────────────────────────


@dataclass(frozen=True)
class OccupancySignal:
    """The winning tier of the occupancy resolution chain, plus its raw read.

    `tier` is "zone" | "room" | "floor" — which precedence tier actually backed
    this answer. `label` is the entity_id, room key or floor name to name in the
    decision log.
    """

    tier: str
    label: str
    occupied: bool
    last_changed: datetime | None


def resolve_occupancy_signal(
    job: VacuumJob,
    ctx: ContextSnapshot,
    zone_meta: ZoneMeta | None,
    room_keys: tuple[str | None, ...],
) -> OccupancySignal | None:
    """Resolve occupancy through the strict precedence chain, most-specific first.

    Each tier is used ONLY when it is actually backed by a live entity; otherwise
    resolution falls through to the next tier. It never falls through to "clear":

      1. ctx.occupancy_readings[zone_meta.occupancy_sensor] — the entity HomeOps
         designated for this zone, read DIRECTLY by entity id. No naming
         convention is involved, because the convention round-trip is lossy:
         several zones point at sensors no suffix rule recovers (Dining Table →
         binary_sensor.emotion_kitchen_dining_table_presence, Master Bedroom →
         binary_sensor.master_bedroom_emotion_any_presence).
      2. ctx.rooms[room_key] for the first candidate key that resolves to a room
         with occupancy_available — the convention-named per-room sensor.
         Retained as a middle tier so Sam's per-room model
         (effectiveness_scope="room_only") keeps room-level precision for the
         rooms that genuinely have a working sensor.
      3. ctx.floor_occupancy[job.floor] — the area_occupancy floor rollup. The
         backstop for any zone with no usable zone- or room-level signal.

    Returns None only when all three tiers are unbacked — the caller decides what
    "no signal at all" means for its gate.

    `room_keys` is the ordered tuple of candidate tier-2 room keys, because the
    two callers derive the room differently: the main gate uses
    ctx.zone_info[zone_id].room_key, while Override 2 uses the zone's PARENT room
    (room_key_for_zone), which is what a sub-zone like Litter Box needs. Nones
    are skipped.
    """
    # Tier 1 — the zone's own designated entity.
    designated = zone_meta.occupancy_sensor if zone_meta else None
    reading = ctx.occupancy_readings.get(designated) if designated else None
    if reading is not None and reading.available:
        return OccupancySignal("zone", reading.entity_id, reading.occupied, reading.last_changed)

    # Tier 2 — the convention-named room sensor, but only if it really exists.
    for room_key in room_keys:
        if not room_key:
            continue
        room = ctx.rooms.get(room_key)
        if room is not None and room.occupancy_available:
            return OccupancySignal(
                "room", room_key, room.raw_occupancy, room.occupancy_last_changed
            )

    # Tier 3 — floor rollup backstop.
    floor = _floor_reading(job, ctx)
    if floor is not None:
        return OccupancySignal("floor", job.floor, floor.occupied, floor.last_changed)

    return None


# ── Effectiveness rules ───────────────────────────────────────────────────────


def zone_active_use_check(
    job: VacuumJob,
    zone_id: int,
    ctx: ContextSnapshot,
    zone_meta: ZoneMeta | None = None,
) -> tuple[str, str, str]:
    """R1-E1: Zone must not be actively occupied or in active use.

    Occupancy is resolved by resolve_occupancy_signal() — the single precedence
    chain shared with Override 2's room-scoped bypass in run_r1(), so both paths
    are guaranteed to read the same tiers in the same order:

      1. ctx.occupancy_readings[zone_meta.occupancy_sensor] — the designated entity
      2. ctx.rooms[zone_info.room_key] — the room sensor, only when available
      3. ctx.floor_occupancy[job.floor] — the floor rollup backstop
      4. Only if all three are unavailable is the zone treated as clear, and the
         reason string says so explicitly.

    Tier 3 is the fix for the permanent no-op class of bug: the Dining Table
    zone (room_key="dining_room") has no binary_sensor.dining_room_occupancy_status
    in HA at all, so tiers 1–2 produced a RoomActivity default of
    raw_occupancy=False and the gate could never fire. It now falls to the 1F
    rollup, which correctly read "on" during all three 2026-08-31 dispatches.

    The occupancy verdict additionally requires the sensor to have been clear
    for job.occupancy_clear_grace_s (see occupancy_state) — a fresh flip to
    "off" is not trusted. detected_activity is unaffected by the grace window.
    """
    grace = job.occupancy_clear_grace_s
    zone_info = ctx.zone_info.get(zone_id)
    signal = resolve_occupancy_signal(
        job, ctx, zone_meta, (zone_info.room_key if zone_info else None,)
    )

    # Tier 4 — genuinely no occupancy signal anywhere. Graceful degradation §8.5.
    if signal is None:
        return "PASS", "none", f"zone_sensor_unavailable_treat_clear:{zone_id}"

    state, elapsed = occupancy_state(signal.occupied, signal.last_changed, ctx.timestamp, grace)

    if signal.tier == "floor":
        occupied_reason = f"zone_floor_fallback_occupied:{zone_id}:{signal.label}"
        unconfirmed_reason = (
            f"zone_floor_fallback_unconfirmed:{zone_id}:{signal.label}"
            f":clear_for={_fmt_dwell(elapsed)}s<{grace}s"
        )
        source = f"floor:{signal.label}"
    elif signal.tier == "zone":
        occupied_reason = f"zone_occupied:{zone_id}:{signal.label}"
        unconfirmed_reason = (
            f"zone_occupancy_unconfirmed:{zone_id}:{signal.label}"
            f":clear_for={_fmt_dwell(elapsed)}s<{grace}s"
        )
        source = signal.label
    else:  # "room" — historic reason string omits the label on the occupied case
        occupied_reason = f"zone_occupied:{zone_id}"
        unconfirmed_reason = (
            f"zone_occupancy_unconfirmed:{zone_id}:{signal.label}"
            f":clear_for={_fmt_dwell(elapsed)}s<{grace}s"
        )
        source = signal.label

    if state == "occupied":
        return "FAIL", "effectiveness", occupied_reason
    if state == "clearing":
        return "FAIL", "effectiveness", unconfirmed_reason
    return _detected_activity_verdict(zone_id, ctx, source=source)


def _detected_activity_verdict(
    zone_id: int, ctx: ContextSnapshot, source: str
) -> tuple[str, str, str]:
    """Second half of R1-E1: block on the room's detected_activity, if we have one.

    Split out so all three occupancy tiers share it. detected_activity is a
    Bayesian rollup rather than a raw sensor edge, so no grace window applies —
    it is not subject to the momentary-dropout failure mode.
    """
    zone_info = ctx.zone_info.get(zone_id)
    room_key = zone_info.room_key if zone_info else None
    room = ctx.rooms.get(room_key) if room_key else None
    if room is not None and room.detected in _ACTIVE_ROOM_STATES:
        return "FAIL", "effectiveness", f"zone_active_use:{zone_id}:activity={room.detected}"
    return "PASS", "none", f"zone_clear:{source}"


def floor_clearance_check(
    job: VacuumJob, zone_id: int, ctx: ContextSnapshot
) -> tuple[str, str, str]:
    """R1-E2: Operating floor must be clear.

    Primary signal is the floor's own dedicated rollup entity from the
    area_occupancy HACS integration —
    binary_sensor.{first,second,third}_floor_occupancy_status — read via
    ctx.floor_occupancy[job.floor].

    That entity replaces the previous approach of OR-ing every room in
    FLOOR_ROOM_MAP[job.floor]. Re-deriving the floor state that way duplicates
    logic HA already owns and, worse, silently omits every room whose
    convention-named entity does not exist: confirmed live 2026-08-31, HA has no
    occupancy entity for dining_room, prep_area, loft, carlitos_room,
    upper_hallway or kids_table_area, so those rooms contributed nothing and the
    1F "floor clear" verdict was computed from four of six rooms.

    The per-room sweep is RETAINED as a secondary net rather than replaced
    outright, and runs after the rollup rather than only when the rollup is
    missing. The bug being fixed is that the sweep was the *only* signal and
    silently under-reported; a room that positively reports "occupied" is real
    evidence and is never a silent default, so keeping it can only ever add a
    block, never suppress one. It also means a dead area_occupancy integration
    degrades to the old behaviour instead of to "clear".

    Both paths honour job.occupancy_clear_grace_s.
    """
    grace = job.occupancy_clear_grace_s

    # Primary — the floor's own rollup entity.
    floor = _floor_reading(job, ctx)
    if floor is not None:
        state, elapsed = occupancy_state(floor.occupied, floor.last_changed, ctx.timestamp, grace)
        if state == "occupied":
            return "FAIL", "effectiveness", f"floor_not_clear:{job.floor}:{floor.entity_id}"
        if state == "clearing":
            return (
                "FAIL",
                "effectiveness",
                f"floor_occupancy_unconfirmed:{job.floor}:{floor.entity_id}"
                f":clear_for={_fmt_dwell(elapsed)}s<{grace}s",
            )

    # Secondary — per-room sweep. Positive occupancy from any room still blocks.
    for room_key in FLOOR_ROOM_MAP.get(job.floor, []):
        room = ctx.rooms.get(room_key)
        if room is None:
            # Sensor unavailable — skip this room (don't let missing sensor block).
            # Coverage for these rooms now comes from the floor rollup above; that
            # is precisely the gap this check could never close on its own.
            continue
        state, elapsed = occupancy_state(
            room.raw_occupancy, room.occupancy_last_changed, ctx.timestamp, grace
        )
        if state == "occupied":
            return "FAIL", "effectiveness", f"floor_not_clear:{room_key}"
        if state == "clearing":
            return (
                "FAIL",
                "effectiveness",
                f"floor_occupancy_unconfirmed:{room_key}:clear_for={_fmt_dwell(elapsed)}s<{grace}s",
            )

    return "PASS", "none", "floor_clear"


def door_open_check(job: VacuumJob, zone_id: int, ctx: ContextSnapshot) -> tuple[str, str, str]:
    """R1-E4: Room door must be open if a door sensor is available.

    Reads room.door_open from ContextSnapshot. The synth fetches
    binary_sensor.{room_key}_door and sets door_open on RoomActivity.
    If door_open is None (sensor unavailable), treat as open — graceful
    degradation. Only runs when job.door_check is True.
    """
    zone_info = ctx.zone_info.get(zone_id)
    room_key = zone_info.room_key if zone_info else None
    if room_key is None:
        return "PASS", "none", f"door_sensor_unavailable_treat_open:{zone_id}"
    room = ctx.rooms.get(room_key)
    if room is None or room.door_open is None:
        return "PASS", "none", f"door_sensor_unavailable_treat_open:{zone_id}"
    if not room.door_open:
        return "FAIL", "effectiveness", f"door_closed:{zone_id}"
    return "PASS", "none", f"door_open:{zone_id}"


def transit_pattern_lookahead(
    job: VacuumJob,
    zone_id: int,
    ctx: ContextSnapshot,
    patterns: list[dict],
) -> tuple[str, str, str]:
    """R1-E3: No transit pattern should be active within the asymmetric look-ahead.

    Asymmetric look-ahead window: [pattern.start - 15min, pattern.end + 5min]
    Forward-biased to avoid starting a run that puts the robot in the path of
    an imminent departure.

    Reads from the temporally-filtered pattern set (passed in from loop.py).
    The render_patterns_for() in loop.py already filters by job and day — this
    rule only checks the temporal window for transit-relevance patterns.

    Spec: §6.1a sub-check (4) + §7.2
    """
    from cortex_python.modules.vacuumops.utils import parse_pattern_time

    current_time = ctx.timestamp
    current_date = current_time.date()
    weekday = current_time.isoweekday()

    for p in patterns:
        # Only transit-relevant patterns block zone_effective
        if "transit" not in p.get("relevance", []):
            continue
        # Day-of-week gate
        if weekday not in p.get("days", []):
            continue
        # Job gate
        jobs_field = p.get("jobs", [])
        if not ("*" in jobs_field or job.job_id in jobs_field):
            continue

        try:
            start = parse_pattern_time(current_date, p["start"])
            end = parse_pattern_time(current_date, p["end"])
        except Exception:
            continue  # malformed pattern — skip

        lookback = timedelta(minutes=15)
        lookahead = timedelta(minutes=5)
        window_start = start - lookback
        window_end = end + lookahead

        if window_start <= current_time <= window_end:
            return (
                "FAIL",
                "effectiveness",
                f"imminent_transit:{p['name']}",
            )

    return "PASS", "none", "no_transit_pattern"


# ── Comfort rules ─────────────────────────────────────────────────────────────


def noise_budget_check(job: VacuumJob, zone_id: int, ctx: ContextSnapshot) -> tuple[str, str, str]:
    """R1-C1: noise_impact must not exceed noise_budget.

    Result:
      impact ≤ budget * 0.7  → PASS (strong — no L1 needed)
      impact ≤ budget         → PASS-marginal (records as AMBIGUOUS → L1)
      impact > budget         → FAIL with reason naming dominant budget reducer
    """
    impact = noise_impact(job, ctx)
    budget = noise_budget(ctx, job.floor)

    if budget <= 0.0:
        # Identify dominant reducer
        reason = _dominant_budget_reducer(ctx, job.floor)
        return "FAIL", "comfort", reason

    if impact <= budget * _NOISE_STRONG_PASS_FACTOR:
        return "PASS", "none", f"noise_ok:impact={impact:.2f}:budget={budget:.2f}"

    if impact <= budget:
        # Marginal — escalate to L1
        return "AMBIGUOUS", "comfort", f"noise_marginal:impact={impact:.2f}:budget={budget:.2f}"

    # impact > budget
    reason = _dominant_budget_reducer(ctx, job.floor)
    return "FAIL", "comfort", reason


def noise_radius_check(job: VacuumJob, zone_id: int, ctx: ContextSnapshot) -> tuple[str, str, str]:
    """R1-C2: noise_radius must not overlap noise-sensitive zones.

    Noise-sensitive = a sleeping person's room or an active piano room.
    Piano is covered by noise_budget (budget near-zero), so this rule focuses
    on sleeping rooms.

    Returns FAIL if job.noise_radius is "house" and any bedroom has sleeping
    detected, or "floor" and any in-scope room has sleeping detected.
    """
    if job.noise_radius == "local":
        # Local radius — no overlap concern
        return "PASS", "none", "local_radius_no_overlap"

    if job.noise_radius == "floor":
        floor_rooms = FLOOR_ROOM_MAP.get(job.floor, [])
        for room_key in floor_rooms:
            room = ctx.rooms.get(room_key)
            if room and room.detected in _NOISE_SENSITIVE_ACTIVITIES:
                return (
                    "FAIL",
                    "comfort",
                    f"noise_radius_overlap:{room_key}:activity={room.detected}",
                )

    elif job.noise_radius == "house":
        # Full house — check all known rooms
        for room_key, room in ctx.rooms.items():
            if room.detected in _NOISE_SENSITIVE_ACTIVITIES:
                return (
                    "FAIL",
                    "comfort",
                    f"noise_radius_overlap:{room_key}:activity={room.detected}",
                )

    return "PASS", "none", "no_noise_radius_overlap"


# ── opportunity_check — predictive patience, LOG-ONLY (PR A3) ────────────────
#
# Spec: cortex_vacuum_patience_and_pause_resume_implementation_spec.md §4.4
# Design memo: cortex_predictive_patience_design.md §3.1-§3.3, §5.1-§5.5
#
# WHAT THIS IS
# ------------
# The third comfort rule. It asks a question none of the other rules ask: not
# "is now acceptable?" but "is a materially BETTER window arriving soon enough
# to be worth waiting for?". The maths lives in `opportunity.py` (PR A2) and is
# pure; everything here is the I/O and adapter layer that turns `run_r1`'s live
# objects into that module's keyword arguments and its answer into a decision-log
# reason string.
#
# WHAT THIS IS NOT
# ----------------
# ⚠ THIS RULE CANNOT CHANGE A DISPATCH OUTCOME UNLESS ACTUATION IS LIVE. The
# shadow branch below returns PASS before any verdict is allowed to become a
# FAIL or an AMBIGUOUS. The rule computes a real verdict, writes a real
# deferral-streak counter and logs a real reason string — and then passes
# anyway. What lifts that is `cfg.opportunity_actuate`, and as of this PR it is
# NOT a source-tree constant: it is a live, DB-backed HomeOps setting
# (`cortex_vacuumops_settings`), re-read every tick and threaded in through
# `dataclasses.replace()` by `loop.vacuumops_loop()`. It ships FALSE and
# fail-closed — an unreachable HomeOps is shadow mode, never a withheld
# dispatch.
#
# Moving the switch out of the code did NOT move the decision. Flipping the row
# is still gated on a >=14-day soak (§4.5's four-signal table) plus Carlos's
# explicit go-ahead. What changed is that turning it back OFF is now a DB write
# rather than a redeploy, which is the property a kill switch is for. Do not
# hardcode this flag anywhere in this file.
#
# It is also NOT an occupancy gate and must never be mistaken for one. It reasons
# about PREDICTED occupancy from a learned prior. Actual, measured occupancy is
# the business of `zone_active_use_check` / `floor_clearance_check`, which are
# EFFECTIVENESS rules that `run_r1` short-circuits on with a bare `return`
# before any comfort rule is reached. See invariant 2 below.
#
# ────────────────────────────────────────────────────────────────────────────
# THE THREE STRUCTURAL INVARIANTS THIS RULE EXISTS INSIDE (all test-pinned)
# ────────────────────────────────────────────────────────────────────────────
# 1. IT CAN NEVER FORCE A DISPATCH. The return type is the same
#    `(result, gate_failed, reason)` triple as every other R1 rule, and the only
#    values this function can produce are PASS / FAIL / AMBIGUOUS on the
#    "comfort" tier. There is no code path by which it can turn another rule's
#    FAIL into a PASS, and no path by which it can cause a dispatch that would
#    not otherwise have happened. Its maximum authority is to withhold.
#
# 2. IT CAN NEVER RUN BEFORE, OR WEAKEN, THE HARDENED OCCUPANCY GATE. The
#    effectiveness rules return out of `run_r1` entirely on FAIL. A degraded
#    prior store, an unreachable Redis, a cold learner — none of them can reach
#    backwards past that `return`. The 2026-08-31 incident (three dispatches into
#    occupied 1F rooms) is structurally out of this rule's reach, and
#    `test_opportunity_never_precedes_effectiveness_gate` pins the ordering so a
#    future refactor cannot silently reorder it into the effectiveness block.
#
# 3. EVERY DEGRADED PATH RETURNS PASS WITH A REASON THAT NAMES THE DEGRADATION.
#    Never a silent no-op. The 2026-08-31 root cause was a gate that no-op'd
#    invisibly — a rule that fails open without saying so in the decision log is
#    the same bug wearing a different hat. Every `return` below carries a reason
#    string, and the fail-open matrix is enumerated in `_OPPORTUNITY_FAIL_OPEN`
#    so a test can assert the set is complete rather than trusting inspection.
#
# WHY THE PSEUDOCODE'S CALL ORDER IS NOT THE CODE'S CALL ORDER
# ------------------------------------------------------------
# Spec §4.4 sketches `opportunity(...)` first and `patience(...)` second. This
# implementation computes patience FIRST, deliberately. `patience` scales the
# forward horizon — `lookahead_slot_count(patience_value=IMPATIENT)` is exactly
# zero slots — so an impatient zone cannot defer to anything even if the read
# were computed. Evaluating patience first is therefore outcome-identical and
# skips a per-slot database read per zone per tick on the path where the answer
# cannot matter. The pseudocode was a sketch of the decision, not of the I/O.
#
# ON BUNDLING (§5.5) — THE GUARD IS STRUCTURAL, NOT A RUNTIME `if`
# -----------------------------------------------------------------
# §4.4's pseudocode opens with `if _is_bundled(zone_id, ctx)`. That predicate
# cannot be written, because at `run_r1` time the answer does not exist yet:
# bundling is decided in `loop.assemble_batch`, strictly AFTER every zone
# outcome has been produced. A bundle passenger is by definition a zone that
# FAILED R0 with `score_below_threshold` (`loop.py`, the D11 bundle sweep), so
# `evaluate_zone` returned at the R0 gate and this rule was never reached; and
# the sweep re-checks eligibility through `_zone_effective_simple` /
# `_noise_acceptable_simple`, which do not call `run_r1` at all.
#
# So the §5.5 requirement — "never shrink a batch" — is guaranteed by the
# control flow rather than by a branch, which is a strictly stronger guarantee
# than the pseudocode asked for. It is pinned by
# `test_bundle_sweep_never_consults_opportunity_check`. The threshold guard
# below is retained anyway as belt-and-braces: it makes the intent legible at
# the point of use and costs one comparison.

# Redis key: consecutive `better_window` verdicts for a zone. The instrument the
# A4 go/no-go reads (§4.5: max streak <=3 green, >=6 red). "Always one hour away"
# is the classic pathology of this mechanism class and is easy to ship blind.
_OPPORTUNITY_DEFER_STREAK_KEY = "cortex:vacuumops:opportunity_defer_streak:{zone_id}"

# A streak is a statement about a run of CONSECUTIVE ticks, so it must not
# outlive the run. 24h is far longer than any plausible streak (the 6h patience
# hard cap bounds a real one) and exists only so a zone that stops being
# evaluated — job disabled, zone removed — cannot leave a stale number behind to
# be misread as soak evidence months later.
_OPPORTUNITY_DEFER_STREAK_TTL_S = 24 * 3600

# Verdicts. `better_window` is the only one that would defer under A4.
VERDICT_BETTER_WINDOW = "better_window"
VERDICT_FIT_MARGINAL = "fit_marginal"
VERDICT_FIT_OK = "fit_ok"

# The complete fail-open set: every reason PREFIX on which this rule returns
# PASS without having formed an opinion. Enumerated as data, not left implicit
# in the branches, so `test_fail_open_matrix_is_complete` can assert that the
# matrix the spec requires and the matrix the code implements are the same set.
# Invariant 3 is only meaningful if it is checkable.
_OPPORTUNITY_FAIL_OPEN: tuple[str, ...] = (
    "opportunity_disabled",  # job.opportunity_enabled is False
    "opportunity_inert",  # no prior source wired in (feature absent)
    "opportunity_skipped",  # bundle-candidate guard (§5.5)
    "opportunity_impatient",  # patience() == 0: hard cap, score band, or no clock
    "opportunity_unavailable",  # opportunity() declined to form a read
    "opportunity_thin",  # a read too thinly evidenced to act on (or escalate on)
    "opportunity_error",  # unexpected exception; rule is inert, tick survives
    "opportunity_shadow",  # LOG-ONLY: a real verdict, deliberately not acted on
    "opportunity_shadow_degraded",  # LOG-ONLY because the settings read FAILED,
    # not because anyone chose it. Distinct from `opportunity_shadow` on purpose:
    # the two are indistinguishable from the flag alone (the read fails closed),
    # and a reviewer counting shadow rows during the §4.5 soak must be able to
    # tell "Carlos has not flipped it yet" from "HomeOps was down for six hours".
)


class OpportunityPriorSource(Protocol):
    """The slice of `priors.PriorStore` this rule needs.

    Narrower than `PriorStoreProtocol` on purpose, and it asks for
    `read_observations` + `build_prior` rather than the more obvious
    `read_slot`. `read_slot` is exactly `build_prior(read_observations(...))`, so
    this costs the same single database read per slot — but it also hands back
    the raw observations, which is the only place the learner's NATIVE AGE can
    be established (see `_read_forward_priors`). Going through `build_prior`
    rather than re-deriving the summary keeps the confidence labelling in A1's
    hands, where it belongs, so the two cannot drift.
    """

    async def read_observations(
        self, entity_id: str, day_of_week: int, slot: int
    ) -> list[PriorObservation]: ...

    def build_prior(
        self,
        entity_id: str,
        day_of_week: int,
        slot: int,
        observations: Sequence[PriorObservation],
    ) -> SlotPrior: ...


@dataclass(frozen=True)
class OpportunityContext:
    """The live I/O dependencies `opportunity_check` needs, bundled into one arg.

    WHY A NEW `run_r1` PARAMETER AND NOT A FIELD ON `ContextSnapshot`:
    `ContextSnapshot` is a per-tick DATA snapshot. It is serialised into the
    decision log (`loop.py`'s `zone_scores` dump) and is deliberately free of
    live handles. Hanging a database-backed store off it would make a
    log-serialisable value hold an open session factory, and would mean every
    existing `ContextSnapshot` construction site in the tests had to grow a
    dependency it does not use. A single optional keyword argument on `run_r1`
    leaves all ~40 existing call sites and fixtures untouched.

    WHY ONE OBJECT AND NOT FIVE PARAMETERS: `run_r1` already takes eight
    arguments. The prior source, the config, the tracked entity and the two
    stats payloads are one cohesive dependency — they are all "the things the
    opportunity rule needs from outside the tick snapshot" — and they arrive and
    depart together.

    `None` for the whole object means the feature is not wired in. That is a
    supported state, not an error: it returns PASS with `opportunity_inert`,
    which keeps every pre-A3 caller (and every existing test) working — the
    RESULT and the GATE are unchanged, which is everything that can reach a
    robot.

    ⚠ "UNCHANGED" DOES NOT MEAN THE REASON STRING IS BYTE-FOR-BYTE IDENTICAL.
    On an opportunity-enabled job the inert path APPENDS
    `|opportunity_inert:no_prior_source` to the decision-log reason. That is
    invariant 3 deliberately at work — every degraded or inert path must NAME
    itself, because a silent no-op is the exact shape of the 2026-08-31 root
    cause. Pinned by
    `test_run_r1_result_and_gate_are_unchanged_when_no_context_is_supplied`.
    """

    prior_source: OpportunityPriorSource
    cfg: VacuumOpsConfig

    prior_entity_id: str
    # The entity whose learned prior is consulted. A1 learns THE BINARY THE GATE
    # READS (`binary_sensor.first_floor_occupancy_status`) rather than
    # reconstructing a floor from its member areas, which is what removed the
    # design memo's OR-vs-MEAN calibration gap (§4.2). The other four tracked
    # entities are diagnostic and are deliberately NOT consulted here.

    zone_stats: Mapping[int, Mapping[str, Any]] = field(default_factory=dict)
    robot_stats: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # `get_vacuum_mission_stats` payloads, fetched ONCE PER TICK by the loop and
    # keyed by zone_id / robot. Not fetched here: this function runs once per
    # (job, zone) and an HTTP call per zone per tick would put a network
    # dependency inside the rule tier. An absent key is not an error — it
    # degrades to `duration_unavailable`, which is a named fail-open path.

    slot_minutes: int = 30
    tz: ZoneInfo = HOUSEHOLD_TZ

    reads: dict[int, OpportunityRead] = field(default_factory=dict)
    # OUT-PARAMETER: `opportunity_check` deposits the OpportunityRead it computed
    # here, keyed by zone_id, so `evaluate_zone` can hand it to the L1 prompt
    # (spec §4.4: "the OpportunityRead goes into the L1 prompt verbatim").
    #
    # An out-parameter rather than a widened return type because `run_r1` returns
    # the flat `(result, gate_failed, reason)` triple that all ~40 of its call
    # sites and every R1 rule share; widening it to carry one optional rule's
    # detail would touch every one of them to serve a single consumer. This
    # mirrors the `l1_results` out-param `evaluate_zone` already uses for exactly
    # the same reason. The dataclass is frozen — the BINDING cannot be swapped —
    # while the dict it points at is deliberately mutable, and its lifetime is
    # one tick because the loop builds a fresh context each time.

    def record(self, zone_id: int, read: OpportunityRead) -> None:
        """Deposit a computed read for the L1 prompt. See `reads`."""
        self.reads[zone_id] = read


async def _read_over_threshold_since(
    redis_client: aioredis.Redis,
    zone_id: int,
    zone_score: float,
    dispatch_threshold: float,
    now: datetime,
) -> datetime | None:
    """When this zone FIRST crossed its dispatch threshold, or None.

    ⚠ NONE MUST MEAN IMPATIENT, AND EVERY FAILURE PATH HERE MUST PRODUCE NONE.
    `patience()` returns IMPATIENT for `over_threshold_since=None`, which makes
    the whole rule inert — the safe direction, because without the clock the
    starvation hard cap cannot fire and a rule that could defer indefinitely must
    not be allowed to run at all. The danger is the opposite mistake: a fallback
    that substitutes "now" for an unreadable key would restart the starvation
    clock on every tick and make a zone INFINITELY PATIENT, silently disabling
    the primary starvation guard. So every exception below returns None, and
    none of them invent a timestamp.

    SETNX-then-GET in the same call, per `opportunity.patience()`'s contract:
    the key is written on the first tick a zone is seen above threshold and read
    back immediately, so a None return means Redis is unreachable or the value is
    corrupt — never "the zone only just crossed".
    """
    # `now` is the TICK timestamp, not wall clock. Every other time input in
    # this rule comes from `ctx.timestamp`, and mixing in a second clock would
    # make the starvation cap depend on how long the tick itself took — and
    # would make the whole rule untestable at a fixed instant.
    key = over_threshold_since_key(zone_id)
    try:
        if zone_score >= dispatch_threshold:
            # No TTL, deliberately. An expiring key would delete the starvation
            # clock of a zone that has been waiting a long time, and the next
            # tick would re-seed it at "now" — turning a zone that had exceeded
            # the 6h hard cap back into a patient one. That is precisely the
            # failure the cap exists to prevent, so an orphaned key (cleared on
            # dispatch, see `loop._clear_opportunity_zone_state`) is the better
            # of the two risks.
            await redis_client.set(key, now.isoformat(), nx=True)
        raw = await redis_client.get(key)
    except Exception as exc:  # noqa: BLE001 — any Redis failure means "no clock"
        log.warning("opportunity_over_threshold_read_failed", zone_id=zone_id, error=str(exc))
        return None

    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        log.warning("opportunity_over_threshold_unparseable", zone_id=zone_id, raw=str(raw))
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def _read_forward_priors(
    source: OpportunityPriorSource,
    entity_id: str,
    keys: Sequence[tuple[int, int]],
    now: datetime,
) -> tuple[list[SlotPrior], float | None]:
    """`(slot priors in `keys` order, learner native age in days)`.

    HOW LEARNER AGE IS ESTABLISHED, AND WHY IT IS DERIVED RATHER THAN STORED:
    the age gate exists to stop `opportunity()` reporting "good" before the
    learner has accumulated real (non-backfilled) history. The obvious
    implementation — a Redis "learning started at" key — restarts the 14-day
    soak clock on any Redis flush and can be trivially wrong after a restore. So
    the age is measured from the DATA instead: the oldest NATIVE observation
    anywhere in the consulted set. That number cannot be reset by losing a cache,
    cannot be seeded by the backfill (which writes `src="backfill"`), and is
    exactly the quantity `opportunity_min_learn_days` is specified against.

    Returns 0.0 — NOT None — when no native observation exists anywhere.
    `opportunity()` SKIPS the age check on None and enforces it on a float, so
    None here would silently disable the very gate this function computes. 0.0
    is "the learner is brand new", which is both true and fail-open.
    """
    priors: list[SlotPrior] = []
    oldest_native: datetime | None = None

    for day_of_week, slot in keys:
        observations = await source.read_observations(entity_id, day_of_week, slot)
        priors.append(source.build_prior(entity_id, day_of_week, slot, observations))
        for obs in observations:
            if obs.src != "native":
                continue
            if oldest_native is None or obs.at < oldest_native:
                oldest_native = obs.at

    if oldest_native is None:
        return priors, 0.0
    # Measured against the TICK clock, not `datetime.now()`. A second clock here
    # would make the 14-day learner gate drift from every other time comparison
    # in the rule.
    age_days = (_as_utc(now) - _as_utc(oldest_native)).total_seconds() / 86400.0
    return priors, max(0.0, age_days)


async def _bump_defer_streak(redis_client: aioredis.Redis, zone_id: int) -> int:
    """Increment the consecutive-`better_window` counter. Returns it, 0 on failure.

    A failure to record the instrument must never fail the tick — the counter is
    evidence for a future decision, not a gate on the current one.
    """
    key = _OPPORTUNITY_DEFER_STREAK_KEY.format(zone_id=zone_id)
    try:
        streak = int(await redis_client.incr(key))
        await redis_client.expire(key, _OPPORTUNITY_DEFER_STREAK_TTL_S)
    except Exception as exc:  # noqa: BLE001
        log.warning("opportunity_defer_streak_bump_failed", zone_id=zone_id, error=str(exc))
        return 0
    return streak


async def _reset_defer_streak(redis_client: aioredis.Redis, zone_id: int) -> None:
    """Clear the streak because THIS tick's verdict was not `better_window`.

    ⚠ THIS IS WHY THE COUNTER IS USABLE DURING THE SHADOW SOAK, AND IT IS A
    DELIBERATE READING OF §4.4's "cleared on dispatch". While actuating, the two
    clear conditions coincide: a non-`better_window` verdict is exactly the tick
    on which the zone stops being deferred and dispatches. IN SHADOW THEY DO NOT
    coincide — every tick passes and dispatches, so clearing on dispatch alone
    would pin the streak at 1 forever and the A4 soak table's "max streak >= 6"
    red light could never illuminate. Clearing on the VERDICT instead measures
    the counterfactual the soak is actually asking about: how many consecutive
    ticks WOULD this zone have been deferred? `loop` still clears on dispatch as
    well, but only on ticks where actuation is live — see
    `loop._clear_opportunity_zone_state` for the other half of this reasoning.
    Because the switch is now a live DB read, which of the two regimes applies
    can change between one tick and the next without a deploy.
    """
    key = _OPPORTUNITY_DEFER_STREAK_KEY.format(zone_id=zone_id)
    try:
        await redis_client.delete(key)
    except Exception as exc:  # noqa: BLE001
        log.warning("opportunity_defer_streak_reset_failed", zone_id=zone_id, error=str(exc))


async def opportunity_check(
    job: VacuumJob,
    zone_id: int,
    ctx: ContextSnapshot,
    redis_client: aioredis.Redis,
    opp_ctx: OpportunityContext | None = None,
) -> tuple[str, str, str]:
    """Comfort rule: is a materially better cleaning window arriving soon?

    See the block comment above for the three structural invariants, the
    fail-open matrix and why bundling needs no runtime guard.

    Returns the standard `(result, gate_failed, reason)` triple. While
    `opp_ctx.cfg.opportunity_actuate` is False the result is ALWAYS "PASS" —
    asserted by `test_shadow_mode_can_never_change_a_verdict` across the full
    verdict cross-product — and the reason string is where all the information
    goes. That flag is live and DB-backed (HomeOps), so shadow-vs-actuating is
    a property of THIS TICK, not of the deployed code.
    """
    if not job.opportunity_enabled:
        return "PASS", "none", "opportunity_disabled"

    if opp_ctx is None:
        # Feature not wired in (a caller that predates A3, or a deployment with
        # no prior store). Named, per invariant 3 — never a silent no-op.
        return "PASS", "none", "opportunity_inert:no_prior_source"

    zone_score = ctx.zone_scores.get(zone_id, 0.0)

    # §5.5 belt-and-braces. Structurally unreachable — a below-threshold zone is
    # deferred by R0 long before R1 — but if it ever became reachable, this zone
    # would be a bundle passenger and shrinking a batch is a behaviour change
    # nobody asked for.
    if zone_score < job.dispatch_threshold:
        return "PASS", "none", "opportunity_skipped:below_dispatch_threshold"

    try:
        return await _opportunity_verdict(job, zone_id, ctx, redis_client, opp_ctx, zone_score)
    except Exception as exc:  # noqa: BLE001
        # A comfort rule must not be able to take down a tick. `run_r1` is
        # wrapped by `evaluate_zone`, but that wrapper converts an exception into
        # a DEFER — so letting one escape would turn a bug in a log-only rule
        # into a suppressed dispatch, which is exactly the authority this PR
        # promises the rule does not have.
        log.error(
            "opportunity_check_error",
            job_id=job.job_id,
            zone_id=zone_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return "PASS", "none", f"opportunity_error:{type(exc).__name__}:{exc!s}"


async def _opportunity_verdict(
    job: VacuumJob,
    zone_id: int,
    ctx: ContextSnapshot,
    redis_client: aioredis.Redis,
    opp_ctx: OpportunityContext,
    zone_score: float,
) -> tuple[str, str, str]:
    """The rule body. Split out so `opportunity_check` owns the blanket guard."""
    cfg = opp_ctx.cfg
    now = ctx.timestamp

    # ── 1. Patience. Computed first — see "call order" in the block comment. ──
    over_since = await _read_over_threshold_since(
        redis_client, zone_id, zone_score, job.dispatch_threshold, now
    )
    patience_value = patience(
        zone_score=zone_score, over_threshold_since=over_since, now=now, cfg=cfg
    )
    if patience_value == IMPATIENT:
        band = patience_band(
            zone_score=zone_score, over_threshold_since=over_since, now=now, cfg=cfg
        )
        # An impatient zone is not deferring, so its streak is over.
        await _reset_defer_streak(redis_client, zone_id)
        return "PASS", "none", f"opportunity_impatient:{band}"

    # ── 2. Duration. Unavailable short-circuits the per-slot reads entirely. ──
    duration = duration_estimate(
        cfg=cfg,
        zone_stats=opp_ctx.zone_stats.get(zone_id),
        robot_stats=opp_ctx.robot_stats.get(job.robot),
        zone_count=1,
    )

    lookahead = lookahead_slot_count(
        patience_value=patience_value, cfg=cfg, slot_minutes=opp_ctx.slot_minutes
    )

    # ── 3. Forward priors. Skipped when there is nothing to fit against. ──
    slots: list[SlotPrior] = []
    learner_days: float | None = None
    if duration.minutes is not None:
        needed = required_slot_count(
            duration_min=duration.minutes,
            lookahead_slots=lookahead,
            slot_minutes=opp_ctx.slot_minutes,
        )
        keys = forward_slot_keys(
            now=now, count=needed, slot_minutes=opp_ctx.slot_minutes, tz=opp_ctx.tz
        )
        slots, learner_days = await _read_forward_priors(
            opp_ctx.prior_source, opp_ctx.prior_entity_id, keys, now
        )

    # ── 4. The pure read. Every remaining fail-open case is decided in here. ──
    opp = opportunity(
        now=now,
        slots=slots,
        duration=duration,
        cfg=cfg,
        patience_value=patience_value,
        learner_native_days=learner_days,
        context_degraded=ctx.degraded,
        slot_minutes=opp_ctx.slot_minutes,
        tz=opp_ctx.tz,
    )

    # Recorded BEFORE the confidence branch: an unavailable read is exactly the
    # thing L1 most needs to be told about, and dropping it here would recreate
    # the invisible-degradation bug one layer up in the prompt.
    opp_ctx.record(zone_id, opp)

    if opp.confidence == CONFIDENCE_UNAVAILABLE:
        await _reset_defer_streak(redis_client, zone_id)
        # `degraded_reason` is never None when confidence != "good" — A2 treats
        # an unnamed degradation as a bug — but the fallback keeps invariant 3
        # true by construction rather than by trusting another module.
        return (
            "PASS",
            "none",
            f"opportunity_unavailable:{opp.degraded_reason or 'unspecified'}",
        )

    # ── 5. Verdict (spec §4.4). ──
    verdict = VERDICT_FIT_OK
    if (
        opp.best_slot_gain >= cfg.opportunity_strong_gain
        and opp.expected_fit_now <= cfg.opportunity_weak_fit
        and opp.confidence == CONFIDENCE_GOOD
    ):
        # "good" is load-bearing here and nowhere else: a `thin` read may inform
        # L1 but may never, on its own, withhold a dispatch.
        verdict = VERDICT_BETTER_WINDOW
    elif opp.expected_fit_now <= cfg.opportunity_marginal_fit:
        verdict = VERDICT_FIT_MARGINAL

    # ── 6. The instrument. Updated on EVERY tick, including in shadow mode —
    # that is the entire point of shipping the counter with A3 rather than A4. ──
    if verdict == VERDICT_BETTER_WINDOW:
        streak = await _bump_defer_streak(redis_client, zone_id)
    else:
        await _reset_defer_streak(redis_client, zone_id)
        streak = 0

    detail = f"{format_opportunity(opp)} streak={streak}"

    # ── 7. ACTUATION GATE. Nothing below this line runs while it is off. ──
    #
    # Read off the VacuumOpsConfig this rule was handed, with no knowledge of
    # where the value came from — deliberately the same ignorance `mop.py` has
    # about `cfg.mop_enabled`. Today it arrives from HomeOps
    # `cortex_vacuumops_settings`, re-read every tick and threaded in via
    # `dataclasses.replace()` in `loop.vacuumops_loop()`; if that source ever
    # changes again, nothing in this file should need to.
    #
    # TWO DISTINCT OFF STATES, AND THEY MUST NOT SHARE A REASON STRING. The
    # read fails closed, so `opportunity_actuate=False` means EITHER "Carlos
    # has not turned it on" or "we could not reach HomeOps". Invariant 3 says
    # every degraded path names its degradation; collapsing an outage into the
    # ordinary shadow reason would hide it in exactly the place a reviewer
    # looks — the decision log — which is the 2026-08-31 invisible-no-op bug
    # with a new coat of paint. `cfg.opportunity_actuate_degraded` separates
    # them. Both still PASS: an unreachable settings endpoint degrades to
    # pre-A4 behaviour, never to a withheld dispatch.
    if not cfg.opportunity_actuate:
        if cfg.opportunity_actuate_degraded:
            return (
                "PASS",
                "none",
                f"opportunity_shadow_degraded:settings_read_failed:{verdict}:{detail}",
            )
        return "PASS", "none", f"opportunity_shadow:{verdict}:{detail}"

    if opp.confidence != CONFIDENCE_GOOD:
        # A THIN read may inform, but may not act — not even by escalating.
        #
        # `better_window` already requires "good", so only `fit_marginal` could
        # reach the actuating path on a thin read, and it would escalate to L1.
        # Spec §4.4's fail-open matrix lists "prior store cold (native_count <
        # opportunity_min_slot_samples on any consulted slot)" as a PASS case,
        # full stop — and an AMBIGUOUS is not a PASS. It routes to an LLM that
        # will be shown a forecast built on one or two observations and asked to
        # weigh it, which is how a thin prior acquires more authority than the
        # evidence behind it. Declining outright is what the spec asked for and
        # is strictly the safer of the two readings.
        return "PASS", "none", f"opportunity_thin:{verdict}:{detail}"

    if verdict == VERDICT_BETTER_WINDOW:
        offset_min = (opp.best_slot_offset or 0) * opp.slot_minutes
        return "FAIL", "comfort", f"better_window_in_{offset_min}m:{detail}"
    if verdict == VERDICT_FIT_MARGINAL:
        return "AMBIGUOUS", "comfort", f"fit_marginal:{detail}"
    return "PASS", "none", f"fit_ok:{detail}"


# ── Occupancy-gate override helpers (spec §6.4, §6.5) ────────────────────────


def room_key_for_zone(zone_meta: ZoneMeta | None) -> str | None:
    """Resolve a zone's parent-room ctx.rooms key from its occupancy_sensor entity ID.

    The litter box's occupancy_sensor is 'binary_sensor.hallway_sensor_group' (Hallway).
    Returns None if the zone has no occupancy_sensor or the entity can't be resolved.

    Resolution strategy (spec §6.4):
      1. Explicit _SENSOR_ENTITY_TO_ROOM map (handles non-standard naming).
      2. Strip 'binary_sensor.' prefix and a known suffix to recover the room key.
    """
    if zone_meta is None or not zone_meta.occupancy_sensor:
        return None
    ent = zone_meta.occupancy_sensor
    if ent in _SENSOR_ENTITY_TO_ROOM:
        return _SENSOR_ENTITY_TO_ROOM[ent]
    name = ent.removeprefix("binary_sensor.")
    for suffix in ("_sensor_group", "_occupancy_status", "_sensor_motion_detection"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return None


def occupancy_gate_bypass(
    zone_id: int,
    ctx: ContextSnapshot,
    zone_meta: ZoneMeta | None,
) -> tuple[str, str | None]:
    """Deterministic occupancy-gate bypass / relaxation (spec §6.5).

    Returns (mode, reason):
      "none"        → run the normal floor-level occupancy gates.
      "full"        → Override 1: skip occupancy gates entirely (house empty).
      "room_scoped" → Override 2: skip the floor-wide check; check ONLY the
                      zone's parent room occupancy sensor.
    reason is a short tag for logging / L1, or None when mode == "none".

    Fail-closed: home_count == -1 (degraded/unknown) → no bypass, gate stays active.
    ALL non-occupancy gates (battery, cooldown, noise, transit, sleep radius) still
    run regardless of mode (spec §0).
    """
    hc = ctx.home_count
    if hc < 0:
        # Unknown presence (degraded sensor.home_context) → no bypass
        return "none", None

    # Override 1 — Home Empty Bypass (all zones, full skip)
    if hc == 0:
        return "full", "home_empty"

    # Override 2 — Single-Person Room-Scoped Low-Disruption (this zone only)
    if hc == 1 and zone_meta is not None and zone_meta.low_disruption:
        # Carve-out: never relax if the sole occupant is Elena (she roams room-to-room
        # doing chores; even alone she could be in any room). Any other single occupant
        # (Carlos, guest, Iestaf…) qualifies. Compare case-insensitively (spec §2.1).
        who = [w.lower() for w in ctx.who_home]
        if _ELENA not in who:
            return "room_scoped", "single_person_low_disruption"

    return "none", None


# ── Entry point ───────────────────────────────────────────────────────────────


async def run_r1(
    job: VacuumJob,
    zone_id: int,
    ctx: ContextSnapshot,
    redis_client: aioredis.Redis,
    patterns: list[dict] | None = None,
    zone_meta: ZoneMeta | None = None,
    bypass_mode: str = "none",
    bypass_reason_str: str | None = None,
    opp_ctx: OpportunityContext | None = None,
) -> tuple[str, str, str]:
    """Run the full R1 tier for one (job, zone) pair.

    Sequence per spec §7.2 + occupancy-gate override layer (spec §6.5, §6.6):
      1. Per-robot cooldown gate (D12) — short-circuits all zones for this robot
      2. Occupancy-gate override decision (deterministic, computed by caller via
         occupancy_gate_bypass(); passed in as bypass_mode to avoid double-compute)
      3. Effectiveness occupancy rules — bypassed/narrowed per override mode:
           "none"        → run zone_active_use_check + floor_clearance_check normally
           "full"        → skip both (house empty; no one to disturb)
           "room_scoped" → skip floor_clearance_check; resolve the zone's own
                           occupancy through resolve_occupancy_signal() with the
                           PARENT room as the tier-2 candidate (spec §6.4)
      3b. Door check (R1-E4) — runs when job.door_check=True, after occupancy gates.
      4. Transit lookahead — ALWAYS runs (not an occupancy gate)
      5. Comfort rules (AMBIGUOUS / PASS-marginal → L1 if job.l1_required or AMBIGUOUS)
         — ALWAYS runs (not an occupancy gate)
           5a. noise_budget_check
           5b. noise_radius_check
           5c. opportunity_check (PR A3) — predictive patience, LOG-ONLY

    ⚠ THE ORDER OF STEPS 3 AND 5 IS A SAFETY PROPERTY, NOT A STYLE CHOICE.
    Every effectiveness rule in step 3 exits this function with a bare `return`
    on FAIL. That is what makes it structurally impossible for ANY comfort rule
    — including `opportunity_check`, which reasons about PREDICTED occupancy —
    to run when MEASURED occupancy has already said no, and therefore impossible
    for a degraded prior store to weaken the gate hardened after the 2026-08-31
    incident. `test_opportunity_never_precedes_effectiveness_gate` pins this
    ordering. Do not convert those short-circuits into collected results, and do
    not move `opportunity_check` above them.

    `opp_ctx` carries the live dependencies `opportunity_check` needs (prior
    store, config, mission-duration stats). It is optional: `None` means the
    predictive-patience feature is not wired in, and the rule returns PASS with
    `opportunity_inert` rather than silently not running. Every pre-A3 caller
    therefore keeps working: its `result` and `gate_failed` are unchanged.
    The `reason` string is NOT — on an enabled job it gains an
    `|opportunity_inert:no_prior_source` suffix, by design (invariant 3: name
    the inert path, never no-op silently). See `OpportunityContext`.

    Returns (result, gate_failed, reason):
      result ∈ {"PASS", "FAIL", "AMBIGUOUS"}
      gate_failed: which gate caused the failure (or "none" on PASS)
      reason: short string for decision log (includes bypass tag when a bypass fired)
    """
    if patterns is None:
        patterns = []

    # D12: per-robot cooldown gate first
    result, gate_failed, reason = await per_robot_cooldown_check(job, zone_id, ctx, redis_client)
    if result == "FAIL":
        return result, gate_failed, reason

    # ── Occupancy-gate override / relaxation (spec §6.6) ─────────────────────
    if bypass_mode == "full":
        # Override 1 — house empty: skip zone_active_use_check + floor_clearance_check.
        # The inflated kitchen occupancy sensor (dry-run bug) is the canonical case this fixes.
        pass  # fall through to transit + comfort

    elif bypass_mode == "room_scoped":
        # Override 2 — single non-Elena occupant + low_disruption zone.
        # Floor-wide floor_clearance_check is replaced by a zone/room-level check.
        # zone_active_use_check keys tier 2 off ctx.zone_info[zone_id].room_key; for
        # sub-zones like Litter Box that is None and the correct room is the PARENT
        # room (hallway, via room_key_for_zone, spec §6.4) — so we run the shared
        # chain ourselves with the parent room prepended to the tier-2 candidates.
        #
        # This branch previously resolved occupancy ONLY through
        # room_key_for_zone(zone_meta) → ctx.rooms[...], which is the same lossy
        # round-trip Fix 1 removed from the main gate: the entity id is mapped back
        # to a room key by suffix-stripping, then a DIFFERENT, convention-named
        # entity is read. For a zone whose designated sensor no suffix rule recovers
        # (Dining Table → binary_sensor.emotion_kitchen_dining_table_presence) the
        # round-trip yields None, ctx.rooms.get(None) is None, and the bypass fell
        # straight through to dispatch having consulted ZERO occupancy signal —
        # reproducing the 2026-08-31 incident class on the Override-2 path.
        # It now reads the designated entity directly (tier 1), then the parent /
        # own room (tier 2), then the floor rollup (tier 3). Tier 3 only ever runs
        # when there is no zone- or room-level signal at all, which is precisely the
        # case where relaxing a floor-wide gate cannot be justified — so Override 2's
        # purpose (a person on the floor but not in THIS room) is fully preserved.
        zone_info = ctx.zone_info.get(zone_id)
        parent_room = room_key_for_zone(zone_meta)
        signal = resolve_occupancy_signal(
            job,
            ctx,
            zone_meta,
            (parent_room, zone_info.room_key if zone_info else None),
        )
        if signal is not None:
            # Same confirmation window as the main gate — a relaxed gate is still
            # a gate, and trusting a 2-second-old "off" here would reopen exactly
            # the hole this PR closes, just on the single-occupant path.
            state, elapsed = occupancy_state(
                signal.occupied,
                signal.last_changed,
                ctx.timestamp,
                job.occupancy_clear_grace_s,
            )
            if state == "occupied":
                return (
                    "FAIL",
                    "effectiveness",
                    f"target_room_occupied:{signal.label}|occ_relax:{bypass_reason_str}",
                )
            if state == "clearing":
                return (
                    "FAIL",
                    "effectiveness",
                    f"target_room_occupancy_unconfirmed:{signal.label}"
                    f":clear_for={_fmt_dwell(elapsed)}s<{job.occupancy_clear_grace_s}s"
                    f"|occ_relax:{bypass_reason_str}",
                )
        # detected_activity is a Bayesian rollup rather than a raw sensor edge, so
        # it is checked separately and without a grace window — same split as
        # _detected_activity_verdict on the main gate.
        target_room = parent_room or (zone_info.room_key if zone_info else None)
        room = ctx.rooms.get(target_room) if target_room else None
        if room is not None and room.detected in _ACTIVE_ROOM_STATES:
            return (
                "FAIL",
                "effectiveness",
                f"target_room_active:{target_room}:activity={room.detected}"
                f"|occ_relax:{bypass_reason_str}",
            )
        # No zone, room OR floor signal anywhere → treat as clear (graceful
        # degradation §8.5, consistent with zone_active_use_check's tier 4).

    else:
        # mode == "none" — occupancy gates per job.effectiveness_scope
        if job.effectiveness_scope == "floor":
            result, gate_failed, reason = zone_active_use_check(job, zone_id, ctx, zone_meta)
            if result == "FAIL":
                return result, gate_failed, reason
            result, gate_failed, reason = floor_clearance_check(job, zone_id, ctx)
            if result == "FAIL":
                return result, gate_failed, reason
        elif job.effectiveness_scope == "room_only":
            # Per-room model (Sam 2F): only check the zone's own room occupancy,
            # not the whole floor. floor_clearance_check is skipped.
            result, gate_failed, reason = zone_active_use_check(job, zone_id, ctx, zone_meta)
            if result == "FAIL":
                return result, gate_failed, reason
        # effectiveness_scope == "none": skip both occupancy checks entirely.
        # Used for Ethan 3F Litter Box — dispatches regardless of 3F occupancy.

    # Door check — R1-E4 (Sam 2F only; job.door_check=False for all others)
    if job.door_check:
        result, gate_failed, reason = door_open_check(job, zone_id, ctx)
        if result == "FAIL":
            return result, gate_failed, reason

    # Transit lookahead — ALWAYS runs regardless of occupancy bypass (not an occupancy gate)
    result, gate_failed, reason = transit_pattern_lookahead(job, zone_id, ctx, patterns)
    if result == "FAIL":
        return result, gate_failed, reason

    # Comfort rules — ALWAYS run (noise/sleep radius not affected by occupancy bypass)
    nb_result, nb_gate, nb_reason = noise_budget_check(job, zone_id, ctx)
    nr_result, nr_gate, nr_reason = noise_radius_check(job, zone_id, ctx)

    # opportunity_check (PR A3) runs AFTER the two noise rules, per spec §4.4.
    # Its placement last in the comfort block is deliberate: it is the only rule
    # here that performs I/O, so a zone already hard-failing on noise should not
    # pay for a database read to be told something that cannot change the answer.
    op_result, op_gate, op_reason = await opportunity_check(
        job, zone_id, ctx, redis_client, opp_ctx
    )

    # Any hard comfort FAIL → defer
    if nb_result == "FAIL":
        return "FAIL", nb_gate, nb_reason
    if nr_result == "FAIL":
        return "FAIL", nr_gate, nr_reason
    if op_result == "FAIL":
        # Unreachable while `opportunity_actuate` is False on every job (PR A3);
        # live from PR A4. Present now so the actuation flip is genuinely the
        # one-line config change it is advertised as, and so the A4 path is
        # exercised by tests before it is exercised by the house.
        return "FAIL", op_gate, op_reason

    # Any AMBIGUOUS comfort result → escalate to L1
    if nb_result == "AMBIGUOUS" or nr_result == "AMBIGUOUS":
        ambiguous_reason = nb_reason if nb_result == "AMBIGUOUS" else nr_reason
        return "AMBIGUOUS", "comfort", ambiguous_reason
    if op_result == "AMBIGUOUS":
        return "AMBIGUOUS", "comfort", op_reason

    # All pass — append bypass tag to reason for observability (spec §6.8).
    # The opportunity reason rides along on the PASS: in LOG-ONLY mode it is the
    # ONLY place the shadow verdict reaches the decision log, and the A4 soak
    # reads it out of exactly this field via get_vacuum_decisions().
    pass_reason = "all_rules_pass"
    if bypass_mode != "none" and bypass_reason_str:
        pass_reason = f"all_rules_pass|occ_bypass:{bypass_reason_str}"
    if op_reason != "opportunity_disabled":
        pass_reason = f"{pass_reason}|{op_reason}"
    return "PASS", "none", pass_reason


# ── Helpers ───────────────────────────────────────────────────────────────────


def _dominant_budget_reducer(ctx: ContextSnapshot, floor: str = "1F") -> str:
    """Identify the most dominant reason the noise budget is near-zero.

    Used in noise_budget_check FAIL reason string for the decision log.

    ``floor`` matters because the quiet-hours reducer in noise_budget() is
    floor-scoped: 1F reduces on quiet_hours_1f, 2F/3F on quiet_hours_2f. Naming
    the flag that did not actually apply to this job's floor would put a wrong
    cause in the decision log — the 2026-08-31 incident was a gate whose real
    behaviour was invisible in that log, so the reason string is load-bearing.
    """
    elena = ctx.people.get("elena")
    if elena and elena.piano:
        return "piano_active"

    # Carlos-in-meeting — 3F only (see noise_budget()'s ×0.05 reducer, D5/C2).
    # Checked ahead of the sleep tier because ×0.05 is stronger than 3F's own
    # sleep-tier multiplier (×0.20); naming the wrong cause here would repeat
    # the 2026-08-31 "invisible gate" class of bug for the meeting path.
    if floor == "3F" and ctx.home.get("carlos_in_meeting"):
        return "carlos_in_meeting_active"

    # Sleep tier — driven by quiet_hours_2f on every floor (see noise_budget).
    # Reported ahead of the floor's own quiet-hours flag because it is the
    # larger multiplier on 2F (×0.05) and 3F (×0.20).
    if ctx.quiet_hours_2f and floor != "1F":
        return "quiet_hours_2f_active"

    if floor == "1F" and ctx.quiet_hours_1f:
        return "quiet_hours_1f_active"

    kitchen = ctx.rooms.get("kitchen")
    if kitchen:
        if kitchen.detected == "cooking" and kitchen.confidence > 0.6:
            return "cooking_in_progress"
        if kitchen.detected == "eating" and kitchen.confidence > 0.6:
            return "eating_in_progress"

    near_term_events = [
        e for e in ctx.upcoming_events if (e.start - ctx.timestamp).total_seconds() < 1800
    ]
    if near_term_events:
        return f"dinner_soon:{near_term_events[0].title}"

    lr = ctx.rooms.get("living_room")
    if lr and lr.raw_occupancy and lr.confidence > 0.6:
        return "living_room_occupied"

    # 1F during household quiet hours but outside the short 1F courtesy window.
    # Reported LAST because ×0.80 is the mildest multiplier in noise_budget() —
    # anything above would mask a stronger, more actionable cause. Named at all
    # so a 1F overnight deferral is not logged as a bare "budget exceeded", and
    # never as "quiet_hours_1f_active", which is not what is being applied.
    if ctx.quiet_hours_2f:
        return "sleep_tier_1f_mild"

    return "noise_budget_exceeded"
