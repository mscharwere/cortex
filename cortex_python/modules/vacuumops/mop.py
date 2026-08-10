"""Mop-cadence gate for CORTEX VacuumOps (Saros only).

Implements the design locked by Carlos on 2026-07-03 (session decisions D14–D18):

    "Mop intelligence (Saros only): signal -> schedule (7-day) -> score
     threshold -> off. Intensity: light/deep."

WHERE THIS LIVES AND WHY
------------------------
The mop is a **modifier on a vacuum dispatch**, not a separate dispatch. The
Saros cannot mop without driving the same segments it vacuums: mopping is
`select.saros_10r_mop_intensity` being set to something other than "off" before
the ordinary `app_segment_clean` command. There is no mop-only mission.

That single physical fact drives three architectural consequences:

1. **Not a new job.** A `Saros1FMopJob` would compete with `Saros1FRoomsJob`
   for the same robot and the same zones, fight the per-robot cooldown (D12),
   and double-dispatch. Instead `VacuumJob` gains mop config fields and this
   module answers "should the dispatch we already decided on run wet?".

2. **Not an R0/R1 rule.** R0/R1 answer *whether the robot should go*. The mop
   question is *how it should run once it goes* — orthogonal. Folding it into
   the gates would mean a zone that is clean enough to skip could block a mop,
   or a mop need could force a dispatch through the noise/effectiveness gates
   that were designed to hold it back. The two-gate model stays untouched.

3. **Resolved at the robot boundary, not per zone.** Mop intensity is a
   unit-level HA setting applied once per mission, so every zone in a batch is
   mopped at the same intensity or none is. Per-zone need is computed
   independently, then joined deterministically into one batch decision —
   exactly the division of labour D13 sets for batching (CORTEX joins; the LLM
   never reasons about batches). The mop gate makes no L1 calls at all: all
   three arms are deterministic.

THE THREE ARMS
--------------
A zone needs a mop when ANY of these fire (checked in this precedence order,
which only affects which arm is *reported*, never whether the mop happens):

  signal   — `mop_requested_at` is set on the zone. An explicit request from a
             human, an HA automation, or a future spill detector. Checked first
             because an explicit request outranks any heuristic.
  schedule — `mop_cadence_days` (default 7) have elapsed since `last_mopped_at`.
             A zone that has never been mopped is treated as maximally overdue.
  score    — the zone's dirtiness score is at or above `mop_score_threshold`
             (default 80, well above the dispatch threshold of 50). A merely
             vacuum-eligible zone does not earn a wet pass on score alone.

DEGRADED CONTEXT
----------------
`ctx.zone_metadata` is best-effort: `get_zone_metadata()` returns `{}` on
failure and the tick proceeds. When a zone's metadata is missing we cannot know
when it was last mopped, so the gate declines to mop. A skipped mop costs one
cadence cycle; an unwanted wet run on unknown state is the worse failure — the
pad may be dirty, the floor may have just been mopped, and neither is
recoverable within the tick.

Spec: C:/Jarvis/Team/TARS/cortex_vacuumops_module_spec.md
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from cortex_python.modules.vacuumops.config import VacuumOpsConfig
from cortex_python.modules.vacuumops.jobs import VacuumJob
from cortex_python.modules.vacuumops.schemas import (
    BatchEntry,
    ContextSnapshot,
    MopDecision,
    MopZoneNeed,
    ZoneMeta,
)

log = structlog.get_logger()

_SECONDS_PER_DAY = 86400.0


def _as_utc(ts: datetime) -> datetime:
    """Normalize a timestamp to tz-aware UTC.

    HomeOps returns MySQL TIMESTAMP values that may deserialize without a
    timezone. Comparing a naive datetime against the tz-aware tick timestamp
    raises TypeError, which would take down the tick — so naive values are
    interpreted as UTC, matching how HomeOps stores them.
    """
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def days_since(ts: datetime | None, now: datetime) -> float | None:
    """Whole-and-fractional days between `ts` and `now`. None if `ts` is None.

    Negative results (a timestamp in the future, e.g. clock skew between the NAS
    and the DB) are clamped to 0.0 rather than being allowed to read as "hugely
    overdue" through a sign error.
    """
    if ts is None:
        return None
    delta = (_as_utc(now) - _as_utc(ts)).total_seconds()
    return max(0.0, delta / _SECONDS_PER_DAY)


def evaluate_mop_need(
    job: VacuumJob,
    zone_id: int,
    ctx: ContextSnapshot,
    now: datetime | None = None,
) -> MopZoneNeed:
    """Decide whether one zone needs a wet pass. Deterministic; no L1.

    Returns a MopZoneNeed whose `reason` is written verbatim into the decision
    log, so it must stay short and human-readable.
    """
    now = now or ctx.timestamp

    if not job.mop_enabled:
        return MopZoneNeed(zone=zone_id, needed=False, reason="job_mop_disabled")

    meta: ZoneMeta | None = ctx.zone_metadata.get(zone_id)
    if meta is None:
        # Degraded context — decline rather than guess. See module docstring.
        log.warning("mop_zone_metadata_missing", zone_id=zone_id, job_id=job.job_id)
        return MopZoneNeed(zone=zone_id, needed=False, reason="metadata_unavailable")

    elapsed = days_since(meta.last_mopped_at, now)
    score = ctx.zone_scores.get(zone_id, 0.0)

    # A zone that has never been mopped is maximally overdue, so it also earns a
    # deep pass. Otherwise deep is a function of how far past cadence it is.
    deep = elapsed is None or elapsed >= job.mop_deep_after_days

    # Arm 1 — signal. An explicit request outranks the heuristics.
    if meta.mop_requested_at is not None:
        return MopZoneNeed(
            zone=zone_id,
            needed=True,
            arm="signal",
            reason="signal:requested",
            deep=deep,
            days_since_mopped=elapsed,
        )

    # Arm 2 — 7-day schedule.
    if elapsed is None:
        return MopZoneNeed(
            zone=zone_id,
            needed=True,
            arm="schedule",
            reason="schedule:never_mopped",
            deep=True,
            days_since_mopped=None,
        )
    if elapsed >= job.mop_cadence_days:
        return MopZoneNeed(
            zone=zone_id,
            needed=True,
            arm="schedule",
            reason=f"schedule:{elapsed:.1f}d",
            deep=deep,
            days_since_mopped=elapsed,
        )

    # Arm 3 — score threshold.
    if score >= job.mop_score_threshold:
        return MopZoneNeed(
            zone=zone_id,
            needed=True,
            arm="score",
            reason=f"score:{score:.0f}",
            deep=deep,
            days_since_mopped=elapsed,
        )

    return MopZoneNeed(
        zone=zone_id,
        needed=False,
        reason=f"not_due:{elapsed:.1f}d",
        days_since_mopped=elapsed,
    )


def resolve_batch_mop(
    batch: list[BatchEntry],
    ctx: ContextSnapshot,
    job_for_zone: dict[int, VacuumJob],
    cfg: VacuumOpsConfig,
    now: datetime | None = None,
) -> MopDecision:
    """Join per-zone mop needs into one batch-level decision for a robot.

    `job_for_zone` maps zone_id -> the job that owns it, so a batch spanning
    jobs (which the bundle path can produce) resolves each zone against its own
    mop configuration.

    The join is a deterministic OR: if any zone in the batch is due, the mission
    runs wet. Zones that are not themselves due ride along, which is unavoidable
    — the pad is either down or it is not.
    """
    now = now or ctx.timestamp

    if not batch:
        return MopDecision(mop=False, reason="off:empty_batch")

    if not cfg.mop_enabled:
        return MopDecision(mop=False, reason="off:module_disabled")

    needs: list[MopZoneNeed] = []
    for entry in batch:
        job = job_for_zone.get(entry.zone)
        if job is None:
            log.warning("mop_no_job_for_zone", zone_id=entry.zone)
            continue
        needs.append(evaluate_mop_need(job, entry.zone, ctx, now))

    due = [n for n in needs if n.needed]
    if not due:
        return MopDecision(mop=False, reason="off:no_zone_due")

    # Report the highest-precedence arm that actually fired.
    arm_order = {"signal": 0, "schedule": 1, "score": 2}
    lead = min(due, key=lambda n: arm_order.get(n.arm or "", 99))

    deep = any(n.deep for n in due)
    reason = lead.reason

    # Floor-type safety cap. Intensity is unit-level, so the batch must run at a
    # setting safe for the most water-sensitive surface it will touch — checked
    # across the WHOLE batch, not just the zones that triggered the mop.
    if deep:
        sensitive = [
            entry.zone
            for entry in batch
            if (meta := ctx.zone_metadata.get(entry.zone)) is not None
            and meta.floor_type in cfg.mop_deep_floor_type_blocklist
        ]
        if sensitive:
            deep = False
            reason = f"{reason}+capped_light"
            log.info(
                "mop_intensity_capped",
                zones=sensitive,
                floor_types=list(cfg.mop_deep_floor_type_blocklist),
            )

    intensity = cfg.mop_intensity_deep if deep else cfg.mop_intensity_light

    decision = MopDecision(
        mop=True,
        intensity=intensity,
        reason=reason,
        triggering_zones=[n.zone for n in due],
    )
    log.info(
        "mop_decision",
        mop=True,
        intensity=intensity,
        reason=reason,
        triggering_zones=decision.triggering_zones,
        deep=deep,
    )
    return decision


__all__ = ["days_since", "evaluate_mop_need", "resolve_batch_mop"]
