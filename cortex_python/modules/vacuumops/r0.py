"""VacuumOps R0 — hard gate predicates.

Five predicates, all must pass or R0 fails immediately with the first-fail
reason logged. Each check returns (passed: bool, reason: str).

Entry point: run_r0(job, zone, ctx, redis_client) → (bool, str)
  Returns (True, "r0_pass") if all checks pass.
  Returns (False, <reason>) at the first failure, short-circuiting the rest.

Spec: C:/Jarvis/Team/TARS/cortex_vacuumops_module_spec.md §7.1
"""

from __future__ import annotations

import redis.asyncio as aioredis

from cortex_python.modules.vacuumops.jobs import VacuumJob
from cortex_python.modules.vacuumops.schemas import ContextSnapshot

# Redis key template for per-zone cooldown. Full key:
# cortex:vacuumops:cooldown:litter_box_clean:<zone_label>
# Spec §8.4, §7.1
_ZONE_COOLDOWN_KEY = "cortex:vacuumops:cooldown:{job_id}:{zone_label}"

# Robot states that mean the robot is NOT available for dispatch
_ACTIVE_STATES = {"cleaning", "returning", "error", "paused"}


def robot_docked(job: VacuumJob, zone: str, ctx: ContextSnapshot) -> tuple[bool, str]:
    """R0-1: Robot must be docked (idle at home base).

    Source: ctx.robot_states[job.robot].state == "docked"
    """
    robot = ctx.robot_states.get(job.robot)
    if robot is None:
        return False, f"robot_state_missing:{job.robot}"
    if robot.state != "docked":
        return False, f"robot_not_docked:{job.robot}:state={robot.state}"
    return True, "r0_pass"


def battery_above_30(job: VacuumJob, zone: str, ctx: ContextSnapshot) -> tuple[bool, str]:
    """R0-2: Robot battery must be above 30%.

    Source: ctx.robot_states[job.robot].battery_pct > 30
    """
    robot = ctx.robot_states.get(job.robot)
    if robot is None:
        return False, f"robot_state_missing:{job.robot}"
    if robot.battery_pct <= 30:
        return False, f"battery_low:{job.robot}:pct={robot.battery_pct}"
    return True, "r0_pass"


def score_above_threshold(job: VacuumJob, zone: str, ctx: ContextSnapshot) -> tuple[bool, str]:
    """R0-3: Zone dirtiness score must exceed dispatch_threshold.

    Source: ctx.zone_scores[zone] > job.dispatch_threshold
    Per spec §7.1 note: threshold for R0 check is job.dispatch_threshold.
    Bundle threshold (D11) is only evaluated in loop.py batch assembly.
    """
    score = ctx.zone_scores.get(zone)
    if score is None:
        return False, f"zone_score_missing:{zone}"
    if score <= job.dispatch_threshold:
        thresh = job.dispatch_threshold
        reason = f"score_below_threshold:{zone}:score={score:.1f}:threshold={thresh}"
        return False, reason
    return True, "r0_pass"


def not_in_active_mission(job: VacuumJob, zone: str, ctx: ContextSnapshot) -> tuple[bool, str]:
    """R0-4: Robot must not be in an active mission state.

    Active states: cleaning | returning | error | paused
    """
    robot = ctx.robot_states.get(job.robot)
    if robot is None:
        return False, f"robot_state_missing:{job.robot}"
    if robot.state in _ACTIVE_STATES:
        return False, f"robot_in_active_mission:{job.robot}:state={robot.state}"
    return True, "r0_pass"


async def not_in_zone_cooldown(
    job: VacuumJob, zone: str, ctx: ContextSnapshot, redis_client: aioredis.Redis
) -> tuple[bool, str]:
    """R0-5: Per-zone cooldown must not be active.

    Redis key: cortex:vacuumops:cooldown:<job_id>:<zone_label>
    Key is set on dispatch (EX = cooldown_minutes * 60). R0 EXISTS-tests it.

    On dock event (§8.4), the key may be cleared early — so cooldown is a
    min-wait, not a max-wait.
    """
    key = _ZONE_COOLDOWN_KEY.format(job_id=job.job_id, zone_label=zone)
    exists = await redis_client.exists(key)
    if exists:
        ttl = await redis_client.ttl(key)
        return False, f"zone_cooldown_active:{zone}:ttl={ttl}s"
    return True, "r0_pass"


async def run_r0(
    job: VacuumJob,
    zone: str,
    ctx: ContextSnapshot,
    redis_client: aioredis.Redis,
) -> tuple[bool, str]:
    """Run all five R0 checks in order. Return (False, reason) at first failure.

    The async signature is required because not_in_zone_cooldown hits Redis.
    The sync checks are called directly (no await needed).

    Returns (True, "r0_pass") only when all five pass.
    """
    # Sync checks — run in sequence; stop at first failure
    for check_fn in (robot_docked, battery_above_30, score_above_threshold, not_in_active_mission):
        passed, reason = check_fn(job, zone, ctx)
        if not passed:
            return False, reason

    # Async check — Redis cooldown
    passed, reason = await not_in_zone_cooldown(job, zone, ctx, redis_client)
    if not passed:
        return False, reason

    return True, "r0_pass"
