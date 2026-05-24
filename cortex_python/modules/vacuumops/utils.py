"""VacuumOps shared utilities.

Extracted here to break the r1.py ↔ loop.py circular import.
r1.py needs _parse_pattern_time; loop.py also uses it.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone


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
    return local.astimezone(timezone.utc)
