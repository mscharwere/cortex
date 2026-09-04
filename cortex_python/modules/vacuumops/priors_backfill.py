"""One-time HA-history seed for the occupancy prior learner (PR A1).

Spec: C:/Jarvis/Team/TARS/cortex_vacuum_patience_and_pause_resume_implementation_spec.md §4.2

WHY THIS EXISTS
---------------
Without it the learner is COLD for eight weeks: `opportunity()` needs
`opportunity_min_slot_samples` observations of a given (day-of-week, 30-min
slot) before it says anything, and each slot recurs once per week. Seeding from
the recorder makes the table usable-but-thin on day 1, while native samples
still ramp confidence to "good" over the following weeks -- which is why A1 is
the first PR merged in the train (§3, merge-order note).

It is an OPTIMISATION, never a hard dependency. Every failure path here logs
loudly and lets the learner start cold; nothing in this module may raise into
the loop.

⚠ THE 28-DAY FIGURE IN THE SPEC IS NOT ACHIEVABLE — VERIFIED 2026-09-04
------------------------------------------------------------------------
§4.2 specifies "reads 28 days of ha_get_history ... and seeds 4 observations per
slot". Live probing of `binary_sensor.first_floor_occupancy_status` on
2026-09-04 found data dense back to 2026-08-15 and NOTHING on 2026-08-14,
2026-08-13 or 2026-08-11. Cause confirmed in the HA config:

    home-assistant-config/configuration.yaml:13 -> recorder: purge_keep_days: 20

So the real ceiling is ~20 days ~= 2.86 weeks, which seeds 2-3 observations per
slot, not 4. The default lookback below stays at the spec's 28 deliberately --
asking for more than the recorder holds costs a handful of empty windows and
nothing else, whereas hardcoding CORTEX's default to HA's CURRENT recorder
setting would silently go stale the day Carlos changes it (Dream Pass v5 #1,
"logged once != tracked"). Instead the report returned by this module records
what coverage was ACTUALLY achieved -- `earliest_data_at`, `slots_no_data` --
so the shortfall is measured on every run rather than assumed away.

Practical consequence for the A4 gate: the backfill contributes less confidence
than §4.2 assumed, so `opportunity_min_slot_samples=3` native observations
remains the binding constraint on actuation. That is the correct outcome; it
just means the backfill buys a warmer start rather than a shorter soak.

⚠ EMPTY IS NOT ZERO
-------------------
A window the recorder has purged comes back empty. `occupied_fraction()` returns
None for it and this module writes NOTHING. Seeding 0.0 instead would encode
"this slot is always clear" -- turning missing data into an argument for
dispatching the vacuum. This is the single failure mode most worth guarding, and
it is the one that the 20-vs-28-day gap above would otherwise walk straight
into: eight days of every slot seeded as permanently free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from cortex_python.modules.vacuumops.priors import (
    HOUSEHOLD_TZ,
    PriorObservation,
    PriorStoreProtocol,
    _as_utc,
    iter_completed_slots,
    occupied_fraction,
    slot_start,
)

log = structlog.get_logger()


@dataclass
class BackfillReport:
    """What the backfill actually managed, per run.

    Deliberately reports coverage rather than just success: the spec's assumed
    28-day window exceeds the recorder's real 20-day retention, so "it ran
    without error" and "it seeded what we expected" are different questions and
    only the second one matters to A2's confidence accounting.
    """

    entities: int = 0
    slots_seeded: int = 0
    slots_no_data: int = 0
    rows_written: int = 0
    chunks_failed: int = 0
    entities_failed: int = 0
    earliest_data_at: datetime | None = None
    per_entity: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True if at least one entity yielded at least one observation."""
        return self.slots_seeded > 0


async def _fetch_chunked_timeline(
    ha_adapter: Any,
    entity_id: str,
    start: datetime,
    end: datetime,
    chunk_days: int,
    report: BackfillReport,
) -> list[tuple[datetime, bool]] | None:
    """Read [start, end) in chunks and concatenate.

    Chunked because a single 28-day range over a sensor that flips dozens of
    times an hour is a large response and a long-held HTTP read; the loop must
    not be blocked on one enormous call. A failed chunk is counted and skipped,
    not fatal -- a partial timeline still yields real observations for the slots
    it does cover, and every slot it does not cover produces None (no data)
    rather than a fabricated fraction.

    Returns None only if EVERY chunk failed, which the caller treats as "this
    entity is unreadable" rather than "this entity was never occupied".
    """
    timeline: list[tuple[datetime, bool]] = []
    cursor = start
    step = timedelta(days=max(1, chunk_days))
    attempted = 0
    failed = 0

    while cursor < end:
        chunk_end = min(cursor + step, end)
        attempted += 1
        try:
            chunk = await ha_adapter.get_state_history(entity_id, cursor, chunk_end)
        except Exception as exc:
            log.warning(
                "prior_backfill_chunk_error",
                entity_id=entity_id,
                start=cursor.isoformat(),
                error=str(exc),
            )
            chunk = None
        if chunk is None:
            failed += 1
            report.chunks_failed += 1
        else:
            timeline.extend(chunk)
        cursor = chunk_end

    if attempted > 0 and failed == attempted:
        return None
    return timeline


async def backfill_priors(
    store: PriorStoreProtocol,
    ha_adapter: Any,
    *,
    entities: list[str],
    now: datetime,
    lookback_days: int = 28,
    chunk_days: int = 7,
    slot_minutes: int = 30,
    tz: ZoneInfo = HOUSEHOLD_TZ,
) -> BackfillReport:
    """Seed `cortex_occupancy_priors` from HA recorder history. Idempotent.

    Idempotency comes from two properties working together, neither sufficient
    alone:
      * every observation is keyed by its slot-start instant, and
        `merge_observations` dedupes on that key, so a second run replaces rather
        than appends;
      * that same merge refuses to let a backfilled observation overwrite a
        native one at the same instant, so re-running after the learner has been
        live does not walk `native_count` backwards.

    Running it twice therefore leaves the table byte-identical, and running it
    after weeks of live learning leaves the native samples untouched.
    """
    report = BackfillReport()
    now = _as_utc(now)
    # End at the current slot boundary: the in-progress slot is not complete and
    # would yield a fraction over a short window.
    end = slot_start(now, slot_minutes, tz)
    start = slot_start(end - timedelta(days=lookback_days), slot_minutes, tz)

    slots = iter_completed_slots(start, end, slot_minutes, tz, max_slots=None)
    if not slots:
        log.warning("prior_backfill_no_slots", lookback_days=lookback_days)
        return report

    log.info(
        "prior_backfill_started",
        entities=len(entities),
        lookback_days=lookback_days,
        slots=len(slots),
        window_start=start.isoformat(),
    )

    for entity_id in entities:
        report.entities += 1
        timeline = await _fetch_chunked_timeline(
            ha_adapter, entity_id, start, end, chunk_days, report
        )
        if timeline is None:
            report.entities_failed += 1
            log.warning("prior_backfill_entity_unreadable", entity_id=entity_id)
            continue

        if timeline:
            earliest = min(t for t, _ in timeline)
            if report.earliest_data_at is None or earliest < report.earliest_data_at:
                report.earliest_data_at = earliest

        observations: list[PriorObservation] = []
        for slot_begin, slot_end in slots:
            fraction = occupied_fraction(timeline, slot_begin, slot_end)
            if fraction is None:
                # Purged window or recorder outage. Write nothing -- see the
                # module docstring's "EMPTY IS NOT ZERO".
                report.slots_no_data += 1
                continue
            observations.append(PriorObservation(f=fraction, at=slot_begin, src="backfill"))

        report.slots_seeded += len(observations)
        report.per_entity[entity_id] = len(observations)

        if observations:
            try:
                report.rows_written += await store.record(entity_id, observations)
            except Exception as exc:
                log.error("prior_backfill_write_failed", entity_id=entity_id, error=str(exc))

    log.info(
        "prior_backfill_finished",
        entities=report.entities,
        entities_failed=report.entities_failed,
        slots_seeded=report.slots_seeded,
        slots_no_data=report.slots_no_data,
        rows_written=report.rows_written,
        chunks_failed=report.chunks_failed,
        earliest_data_at=(report.earliest_data_at.isoformat() if report.earliest_data_at else None),
        # Coverage, stated explicitly rather than left to be inferred: this is the
        # number that tells Carlos whether the recorder actually held what §4.2
        # assumed. See the 20-vs-28-day note in the module docstring.
        effective_lookback_days=(
            round((now - report.earliest_data_at).total_seconds() / 86400, 1)
            if report.earliest_data_at
            else 0.0
        ),
        requested_lookback_days=lookback_days,
    )
    return report


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)
