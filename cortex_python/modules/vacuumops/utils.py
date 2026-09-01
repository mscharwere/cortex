"""VacuumOps shared utilities.

Extracted here to break the r1.py ↔ loop.py circular import.
r1.py needs _parse_pattern_time; loop.py also uses it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time


def parse_pattern_time(current_date: date, time_str: str) -> datetime:
    """Parse HH:MM string into a UTC datetime on current_date.

    Patterns are defined in PST (America/Los_Angeles). Loop timestamps are UTC.
    Uses pytz for correct DST handling. Carlos uses "PST" year-round.
    """
    import pytz

    pst = pytz.timezone("America/Los_Angeles")
    h, m = [int(x) for x in time_str.split(":")]
    naive = datetime.combine(current_date, time(h, m))
    local = pst.localize(naive)
    return local.astimezone(UTC)


# ── 1F quiet hours ────────────────────────────────────────────────────────────
#
# quiet_hours_1f and quiet_hours_2f are DIFFERENT quantities and must not share
# a source. Until this module gained is_quiet_hours_1f() the synth aliased both
# to sensor.home_context.attributes.quiet_hours, which is the household
# quiet-hours convention: `now().hour >= 22 or now().hour < 7` (a plain clock
# window, packages/room_context/home_context.yaml). That is the right signal for
# 2F. It is the wrong signal for 1F, and the aliasing made the two flags
# impossible to move independently.
#
# Why 1F needs its own, much shorter window:
#
#   * The household sleep window's effect on the ground floor is ALREADY
#     modelled, floor-aware, by noise.noise_budget()'s sleep tier — 2F ×0.05
#     (block), 3F ×0.20, 1F ×0.80 ("sound from the ground floor does not
#     meaningfully reach 2F bedrooms"). Feeding the same 22:00-07:00 signal
#     into the separate quiet_hours_1f reducer double-counted it, compounding
#     to ×0.32 and silently contradicting that stated intent.
#
#   * 7 days of live binary_sensor.first_floor_occupancy_status history put
#     essentially all of 1F's long clear stretches between 23:00 and 07:00.
#     Hourly occupancy falls off a cliff at 23:00 — 22:00 reads 72-79%, 23:00
#     reads 31-42%, and 00:00-06:00 stays low. A window that ran to 07:00 was
#     therefore suppressing precisely the supply of usable windows.
#
# So the window keeps the household's 22:00 start (no change before 23:00, no
# surprise for anyone reading home_context) and ends at the measured 23:00
# cliff. From 23:00 the ground floor is empty and the ×0.80 sleep tier is the
# correct and sufficient model on its own.
#
# Hours are PST (America/Los_Angeles), matching parse_pattern_time above and
# Carlos's year-round "PST" convention. Both bounds are parameters rather than
# literals so the window can be retuned after observing real overnight runs.
QUIET_HOURS_1F_START_HOUR = 22
QUIET_HOURS_1F_END_HOUR = 23


def is_quiet_hours_1f(
    now: datetime,
    start_hour: int = QUIET_HOURS_1F_START_HOUR,
    end_hour: int = QUIET_HOURS_1F_END_HOUR,
) -> bool:
    """Is ``now`` inside the 1F-local quiet-hours window?

    ``now`` may carry any timezone (the loop works in UTC); it is converted to
    PST before the hour is read. The window is half-open on the hour:
    ``[start_hour:00, end_hour:00)``.

    Windows that wrap past midnight are supported (``start_hour > end_hour``,
    e.g. 22 → 7) so retuning the bounds cannot silently produce an
    always-false window. ``start_hour == end_hour`` means "no quiet hours" —
    an empty window, never a 24-hour one.
    """
    import pytz

    pst = pytz.timezone("America/Los_Angeles")
    hour = now.astimezone(pst).hour

    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour
