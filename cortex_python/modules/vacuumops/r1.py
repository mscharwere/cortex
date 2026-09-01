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

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis

from cortex_python.modules.vacuumops.jobs import VacuumJob
from cortex_python.modules.vacuumops.noise import FLOOR_ROOM_MAP, noise_budget, noise_impact
from cortex_python.modules.vacuumops.schemas import ContextSnapshot, OccupancyReading, ZoneMeta

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

    # Any hard comfort FAIL → defer
    if nb_result == "FAIL":
        return "FAIL", nb_gate, nb_reason
    if nr_result == "FAIL":
        return "FAIL", nr_gate, nr_reason

    # Any AMBIGUOUS comfort result → escalate to L1
    if nb_result == "AMBIGUOUS" or nr_result == "AMBIGUOUS":
        ambiguous_reason = nb_reason if nb_result == "AMBIGUOUS" else nr_reason
        return "AMBIGUOUS", "comfort", ambiguous_reason

    # All pass — append bypass tag to reason for observability (spec §6.8)
    pass_reason = "all_rules_pass"
    if bypass_mode != "none" and bypass_reason_str:
        pass_reason = f"all_rules_pass|occ_bypass:{bypass_reason_str}"
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
