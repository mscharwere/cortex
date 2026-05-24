"""VacuumOps noise model — noise_impact() + noise_budget().

Two orthogonal functions:
  - noise_impact(job, ctx): how disruptive this specific job is right now
    (property of the job + room layout)
  - noise_budget(ctx): how much noise the household can absorb right now
    (property of the context, not the job)

dispatch condition: noise_impact(job, ctx) ≤ noise_budget(ctx)

Spec: C:/Jarvis/Team/TARS/cortex_vacuumops_module_spec.md §6.2–§6.3
"""

from __future__ import annotations

from cortex_python.modules.vacuumops.jobs import VacuumJob
from cortex_python.modules.vacuumops.schemas import ContextSnapshot

# Floor-to-room map. Used by noise_impact (floor radius check) and
# floor_clearance_check in r1.py. Exported so r1.py and loop.py share the
# same canonical room inventory without re-importing.
#
# Spec: §4.2
FLOOR_ROOM_MAP: dict[str, list[str]] = {
    "1F": ["living_room", "kitchen", "hallway", "dining_room", "prep_area", "bathroom"],
    "2F": ["master_bedroom", "master_bath", "upper_hallway", "carlitos_room", "kids_table_area"],
    "3F": ["office", "family_room"],  # subscribe if sensors exist; degrade gracefully if not
}


def noise_impact(job: VacuumJob, ctx: ContextSnapshot) -> float:
    """Compute the noise impact of dispatching job in the current context.

    Base impact = job.noise_level (1–5).
    Radius modifier: jobs whose noise radius overlaps occupied rooms are multiplied.

    Returns a float in [1.0, ∞), though practically bounded by the room count
    on a given floor.

    Spec: §6.2
    """
    base = float(job.noise_level)  # 1.0 .. 5.0

    if job.noise_radius == "local":
        # Only the zone(s) being cleaned matter. For Litter Box, that's the
        # hallway-adjacent strip — no occupants typically.
        return base

    if job.noise_radius == "floor":
        # All rooms on the same floor as the robot contribute.
        # Use FLOOR_ROOM_MAP[job.floor] — same source as floor_clearance_check
        # so effectiveness and comfort gates reason over the same room set.
        floor_rooms = FLOOR_ROOM_MAP.get(job.floor, [])
        active = sum(1 for r in floor_rooms if ctx.rooms.get(r) and ctx.rooms[r].raw_occupancy)
        return base * (1.0 + 0.5 * active)  # +50% per occupied room on this floor

    if job.noise_radius == "house":
        # Sam-class job — 2F adjacent to bedrooms. Phase 2 territory.
        return base * 1.5

    # Unknown radius — return base conservatively
    return base


def noise_budget(ctx: ContextSnapshot) -> float:
    """Compute how much noise the household can absorb right now.

    Budget starts at 10.0 (fully open) and is reduced multiplicatively by each
    constraint that fires. Returns a float in [0.0, 10.0].

    Spec: §6.3
    """
    budget = 10.0  # start fully open

    # Piano practice — Elena's live piano sensor — drops budget near zero
    elena = ctx.people.get("elena")
    if elena and elena.piano:
        budget *= 0.05  # piano in progress: practically off-limits

    # Sleep state (2F bedrooms) — kills budget for any 2F job; reduces it for 1F
    sleep_active = ctx.quiet_hours_2f or any(
        p.sleep_confidence is not None and p.sleep_confidence > 0.7
        for p in ctx.people.values()
        if p.sleep_confidence is not None
    )
    if sleep_active:
        budget *= 0.20  # 2F asleep — 1F can still run softly; 2F cannot

    # Quiet hours 1F (10pm–7am) — daytime suppression for 1F jobs
    if ctx.quiet_hours_1f:
        budget *= 0.40

    # Active cooking — kitchen detected_activity == "cooking"
    kitchen = ctx.rooms.get("kitchen")
    if kitchen and kitchen.detected == "cooking" and kitchen.confidence > 0.6:
        budget *= 0.30  # don't vacuum under feet during dinner prep

    # Active eating
    if kitchen and kitchen.detected == "eating" and kitchen.confidence > 0.6:
        budget *= 0.50  # mid-meal: lower budget, not zero

    # Upcoming family-disruption events in next 30 min
    near_term_events = [
        e for e in ctx.upcoming_events if (e.start - ctx.timestamp).total_seconds() < 1800
    ]
    if near_term_events:
        budget *= 0.60  # don't start a mission about to be interrupted

    # Living room occupied + watching TV (proxy: detected == "active" + confidence)
    lr = ctx.rooms.get("living_room")
    if lr and lr.raw_occupancy and lr.confidence > 0.6:
        budget *= 0.70

    return max(0.0, min(10.0, budget))
