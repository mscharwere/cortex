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

from datetime import timedelta

import redis.asyncio as aioredis

from cortex_python.modules.vacuumops.jobs import VacuumJob
from cortex_python.modules.vacuumops.noise import FLOOR_ROOM_MAP, noise_budget, noise_impact
from cortex_python.modules.vacuumops.schemas import ContextSnapshot, ZoneMeta

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


# ── Effectiveness rules ───────────────────────────────────────────────────────


def zone_active_use_check(job: VacuumJob, zone_id: int, ctx: ContextSnapshot) -> tuple[str, str, str]:
    """R1-E1: Zone must not be actively occupied or in active use.

    Checks:
    - raw_occupancy == False (with 90s grace — we treat False as clear; the
      ContextSnapshot is assembled with the grace period baked in by the synth)
    - detected_activity NOT IN {active, cooking, eating, transit}

    Returns FAIL with reason if zone is occupied or in active use.
    Sub-zones (room_key=None, e.g. Litter Box) have no room sensor — treat as clear.
    """
    zone_info = ctx.zone_info.get(zone_id)
    room_key = zone_info.room_key if zone_info else None
    if room_key is None:
        # No parent room sensor (sub-zone or sensor unavailable) — graceful degradation §8.5
        return "PASS", "none", "zone_sensor_unavailable_treat_clear"

    room = ctx.rooms.get(room_key)
    if room is None:
        return "PASS", "none", "zone_sensor_unavailable_treat_clear"

    if room.raw_occupancy:
        return "FAIL", "effectiveness", f"zone_occupied:{zone_id}"

    if room.detected in _ACTIVE_ROOM_STATES:
        return "FAIL", "effectiveness", f"zone_active_use:{zone_id}:activity={room.detected}"

    return "PASS", "none", "zone_clear"


def floor_clearance_check(job: VacuumJob, zone_id: int, ctx: ContextSnapshot) -> tuple[str, str, str]:
    """R1-E2: Operating floor must be clear (no raw_occupancy across floor rooms).

    Checks every room in FLOOR_ROOM_MAP[job.floor]. The target zone's own
    occupancy is also covered here for double-safety. Skips rooms whose sensors
    are unavailable (graceful degradation §4.2).

    Returns FAIL naming the first occupied room.
    """
    floor_rooms = FLOOR_ROOM_MAP.get(job.floor, [])
    for room_key in floor_rooms:
        room = ctx.rooms.get(room_key)
        if room is None:
            # Sensor unavailable — skip this room (don't let missing sensor block)
            continue
        if room.raw_occupancy:
            return "FAIL", "effectiveness", f"floor_not_clear:{room_key}"

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
    budget = noise_budget(ctx)

    if budget <= 0.0:
        # Identify dominant reducer
        reason = _dominant_budget_reducer(ctx)
        return "FAIL", "comfort", reason

    if impact <= budget * _NOISE_STRONG_PASS_FACTOR:
        return "PASS", "none", f"noise_ok:impact={impact:.2f}:budget={budget:.2f}"

    if impact <= budget:
        # Marginal — escalate to L1
        return "AMBIGUOUS", "comfort", f"noise_marginal:impact={impact:.2f}:budget={budget:.2f}"

    # impact > budget
    reason = _dominant_budget_reducer(ctx)
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
           "room_scoped" → skip floor_clearance_check; check ONLY the zone's parent room
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
        # Floor-wide floor_clearance_check is replaced by a room-level check on the
        # zone's own parent room (via occupancy_sensor → room key resolver, spec §6.4).
        # zone_active_use_check checks by zone NAME key; for sub-zones like Litter Box
        # the correct room is the parent room (hallway), not the zone label — so we skip
        # zone_active_use_check too and do a single room-level check ourselves.
        target_room = room_key_for_zone(zone_meta)
        room = ctx.rooms.get(target_room) if target_room else None
        if room is not None:
            if room.raw_occupancy:
                return (
                    "FAIL",
                    "effectiveness",
                    f"target_room_occupied:{target_room}|occ_relax:{bypass_reason_str}",
                )
            if room.detected in _ACTIVE_ROOM_STATES:
                return (
                    "FAIL",
                    "effectiveness",
                    f"target_room_active:{target_room}:activity={room.detected}|occ_relax:{bypass_reason_str}",
                )
        # Room sensor unavailable → treat as clear (graceful degradation §8.5 consistent with
        # floor_clearance_check which skips unavailable rooms rather than blocking).

    else:
        # mode == "none" — occupancy gates per job.effectiveness_scope
        if job.effectiveness_scope == "floor":
            for eff_fn in (zone_active_use_check, floor_clearance_check):
                result, gate_failed, reason = eff_fn(job, zone_id, ctx)
                if result == "FAIL":
                    return result, gate_failed, reason
        elif job.effectiveness_scope == "room_only":
            # Per-room model (Sam 2F): only check the zone's own room occupancy,
            # not the whole floor. floor_clearance_check is skipped.
            result, gate_failed, reason = zone_active_use_check(job, zone_id, ctx)
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


def _dominant_budget_reducer(ctx: ContextSnapshot) -> str:
    """Identify the most dominant reason the noise budget is near-zero.

    Used in noise_budget_check FAIL reason string for the decision log.
    """
    elena = ctx.people.get("elena")
    if elena and elena.piano:
        return "piano_active"

    if ctx.quiet_hours_2f:
        return "quiet_hours_2f_active"

    if ctx.quiet_hours_1f:
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

    return "noise_budget_exceeded"
