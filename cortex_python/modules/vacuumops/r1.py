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
from cortex_python.modules.vacuumops.schemas import ContextSnapshot

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


# ── D12: Per-robot cooldown gate ──────────────────────────────────────────────


async def per_robot_cooldown_check(
    job: VacuumJob, zone: str, ctx: ContextSnapshot, redis_client: aioredis.Redis
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


def zone_active_use_check(job: VacuumJob, zone: str, ctx: ContextSnapshot) -> tuple[str, str, str]:
    """R1-E1: Zone must not be actively occupied or in active use.

    Checks:
    - raw_occupancy == False (with 90s grace — we treat False as clear; the
      ContextSnapshot is assembled with the grace period baked in by the synth)
    - detected_activity NOT IN {active, cooking, eating, transit}

    Returns FAIL with reason if zone is occupied or in active use.
    """
    room = ctx.rooms.get(zone.lower().replace(" ", "_"))
    if room is None:
        # Sensor unavailable — treat zone as clear (graceful degradation §8.5)
        return "PASS", "none", "zone_sensor_unavailable_treat_clear"

    if room.raw_occupancy:
        return "FAIL", "effectiveness", f"zone_occupied:{zone}"

    if room.detected in _ACTIVE_ROOM_STATES:
        return "FAIL", "effectiveness", f"zone_active_use:{zone}:activity={room.detected}"

    return "PASS", "none", "zone_clear"


def floor_clearance_check(job: VacuumJob, zone: str, ctx: ContextSnapshot) -> tuple[str, str, str]:
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


def transit_pattern_lookahead(
    job: VacuumJob,
    zone: str,
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


def noise_budget_check(job: VacuumJob, zone: str, ctx: ContextSnapshot) -> tuple[str, str, str]:
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


def noise_radius_check(job: VacuumJob, zone: str, ctx: ContextSnapshot) -> tuple[str, str, str]:
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


# ── Entry point ───────────────────────────────────────────────────────────────


async def run_r1(
    job: VacuumJob,
    zone: str,
    ctx: ContextSnapshot,
    redis_client: aioredis.Redis,
    patterns: list[dict] | None = None,
) -> tuple[str, str, str]:
    """Run the full R1 tier for one (job, zone) pair.

    Sequence per spec §7.2:
      1. Per-robot cooldown gate (D12) — short-circuits all zones for this robot
      2. Effectiveness rules (hard FAIL → no L1)
      3. Comfort rules (AMBIGUOUS / PASS-marginal → L1 if job.l1_required or AMBIGUOUS)

    Returns (result, gate_failed, reason):
      result ∈ {"PASS", "FAIL", "AMBIGUOUS"}
      gate_failed: which gate caused the failure (or "none" on PASS)
      reason: short string for decision log
    """
    if patterns is None:
        patterns = []

    # D12: per-robot cooldown gate first
    result, gate_failed, reason = await per_robot_cooldown_check(job, zone, ctx, redis_client)
    if result == "FAIL":
        return result, gate_failed, reason

    # Effectiveness rules — hard gates
    for eff_fn in (zone_active_use_check, floor_clearance_check):
        result, gate_failed, reason = eff_fn(job, zone, ctx)
        if result == "FAIL":
            return result, gate_failed, reason

    # Transit lookahead (needs patterns list)
    result, gate_failed, reason = transit_pattern_lookahead(job, zone, ctx, patterns)
    if result == "FAIL":
        return result, gate_failed, reason

    # Comfort rules — collect results
    nb_result, nb_gate, nb_reason = noise_budget_check(job, zone, ctx)
    nr_result, nr_gate, nr_reason = noise_radius_check(job, zone, ctx)

    # Any hard comfort FAIL → defer
    if nb_result == "FAIL":
        return "FAIL", nb_gate, nb_reason
    if nr_result == "FAIL":
        return "FAIL", nr_gate, nr_reason

    # Any AMBIGUOUS comfort result → escalate to L1
    if nb_result == "AMBIGUOUS" or nr_result == "AMBIGUOUS":
        ambiguous_reason = nb_reason if nb_result == "AMBIGUOUS" else nr_reason
        return "AMBIGUOUS", "comfort", ambiguous_reason

    # All pass
    return "PASS", "none", "all_rules_pass"


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
