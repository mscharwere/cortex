"""Rolling occupancy prior learner (PR A1).

Spec: C:/Jarvis/Team/TARS/cortex_vacuum_patience_and_pause_resume_implementation_spec.md §4.2

WHAT THIS IS
------------
A forward-looking occupancy model that CORTEX learns itself, because Home
Assistant has no surface that answers "how likely is this floor to be clear
during the next 30 minutes?".  `area_occupancy` exposes only the CURRENT
(day_of_week, time_slot) prior at a fixed, non-configurable 60-minute
granularity, and its floor-aggregate sensors expose no attributes at all.

CORTEX therefore keeps its own table -- `cortex_occupancy_priors`, one row per
(entity, local day-of-week, 30-minute slot) -- and this module is everything
that reads and writes it.  PR A2's `opportunity()` consumes `read_slot()`; it is
the only consumer, and it is not built yet, which is why nothing in this module
changes a single dispatch decision.  A1 exists to START THE SAMPLE CLOCK: it is
the only calendar-bound item in the patience/pause-resume train.

SAMPLE-AND-AGGREGATE, NOT POLL-AND-SNAPSHOT
-------------------------------------------
An observation is an occupied FRACTION of a slot, not a binary reading taken at
some instant inside it.  This is not a refinement; it is a correctness
requirement.  `loop.next_interval()` returns 300 s whenever any robot is
`cleaning`, so a tick-sampled learner would be systematically under-sampled over
exactly the windows this feature exists to reason about.  Fractions are derived
from HA state-change timestamps over the closed slot, which is exact and
immune to tick jitter.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
------------------------------------------
- It does not reconstruct a floor from its member areas.  Carlos fixed the
  composite HA-side (Driveway moved out of the First Floor grouping, Garage
  threshold 5%->10%), and R1 gates on `binary_sensor.first_floor_occupancy_status`
  directly, so the learner learns THAT ENTITY -- the binary the gate actually
  reads.  No member-set reconstruction, no OR-vs-MEAN calibration gap.
- It does not judge the signal.  Per D1 the learner faithfully learns whatever
  the 1F rollup reports, including its residual overnight occupancy.  The
  practical consequence -- `opportunity()` will not recommend the overnight
  window while the rollup reads occupied there -- is an accepted cap on the
  feature's upside (§10 AR-1), not a defect to correct here.

EMPTY IS NOT ZERO
-----------------
The single most dangerous mistake available in this module is writing 0.0 for a
window HA returned no data for.  `mean_occupied = 0.0` reads downstream as
`p_clear = 1.0` -- "this slot is always free, dispatch now" -- so a purge-aged
or recorder-outage window would silently become an argument FOR running the
vacuum.  Every path that cannot establish a fraction returns None and writes
nothing.  See `occupied_fraction()`.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

# Household local time. Slots are LOCAL, not UTC: the entire premise is
# "Tuesday around 3pm is usually busy", which is a wall-clock statement. A
# UTC-keyed table would smear every weekday pattern by an hour twice a year.
HOUSEHOLD_TZ = ZoneInfo("America/Los_Angeles")

TABLE = "cortex_occupancy_priors"

ObservationSource = Literal["native", "backfill"]

# Redis key holding the ISO instant of the last slot END the learner has closed
# out. Redis (not the DB) because it is loop-scoped bookkeeping, not data --
# same treatment as the per-zone and per-robot cooldown keys.
WATERMARK_KEY = "cortex:vacuumops:prior_learner:watermark"

# Redis key marking that the one-time HA-history backfill has already run.
# The backfill is idempotent regardless (see priors_backfill), so this is a cost
# optimisation, not a correctness guard.
BACKFILL_DONE_KEY = "cortex:vacuumops:prior_learner:backfilled"

CONFIDENCE_GOOD = "good"
CONFIDENCE_THIN = "thin"
CONFIDENCE_UNAVAILABLE = "unavailable"


# ── Slot arithmetic ───────────────────────────────────────────────────────────


def slots_per_day(slot_minutes: int) -> int:
    """Number of slots in a day. Requires a slot length that divides 1440.

    Rejecting a non-dividing slot length up front matters: a 7-minute slot would
    silently produce a final short slot every day whose fraction is computed over
    a shorter window than every other sample of the same index.
    """
    if slot_minutes <= 0 or 1440 % slot_minutes != 0:
        raise ValueError(f"slot_minutes must be a positive divisor of 1440, got {slot_minutes}")
    return 1440 // slot_minutes


def _as_utc(ts: datetime) -> datetime:
    """Coerce to aware UTC. Naive input is assumed UTC, matching the loop."""
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def slot_key(ts: datetime, slot_minutes: int = 30, tz: ZoneInfo = HOUSEHOLD_TZ) -> tuple[int, int]:
    """Return (day_of_week, slot) for an instant, in household local time.

    day_of_week is 0=Monday..6=Sunday (datetime.weekday()), matching the column
    comment in the migration.
    """
    slots_per_day(slot_minutes)  # validate
    local = _as_utc(ts).astimezone(tz)
    return local.weekday(), (local.hour * 60 + local.minute) // slot_minutes


def slot_start(ts: datetime, slot_minutes: int = 30, tz: ZoneInfo = HOUSEHOLD_TZ) -> datetime:
    """UTC instant at which the local slot containing `ts` began.

    `.replace()` preserves the `fold` attribute that `astimezone()` set, so the
    repeated local hour on a fall-back DST day floors to the correct one of its
    two instants rather than jumping an hour.
    """
    slots_per_day(slot_minutes)  # validate
    local = _as_utc(ts).astimezone(tz)
    minute_of_day = (local.hour * 60 + local.minute) // slot_minutes * slot_minutes
    local_start = local.replace(
        hour=minute_of_day // 60, minute=minute_of_day % 60, second=0, microsecond=0
    )
    return local_start.astimezone(UTC)


def iter_completed_slots(
    since: datetime,
    until: datetime,
    slot_minutes: int = 30,
    tz: ZoneInfo = HOUSEHOLD_TZ,
    max_slots: int | None = None,
) -> list[tuple[datetime, datetime]]:
    """(start, end) UTC pairs for every slot that fully completed in (since, until].

    A slot is "completed" only once `until` has reached its end -- a half-observed
    slot would produce a fraction over a shorter window than its siblings and
    corrupt that slot's mean.

    Oldest first, capped at `max_slots`. Oldest-first + a cap means a process
    that has been down for days catches up over successive ticks rather than
    doing it all in one, while never letting a single tick issue an unbounded
    number of HA history reads.

    UTC arithmetic is used to step between boundaries. That is sound for any slot
    length dividing 60 because US DST shifts by a whole hour: local :00/:30
    boundaries stay exactly 30 UTC minutes apart across the transition.
    """
    since = _as_utc(since)
    until = _as_utc(until)
    if until <= since:
        return []

    step = timedelta(minutes=slot_minutes)
    # The slot containing `since` is only emitted if `since` sits exactly on its
    # boundary; otherwise it was already partially elapsed when we started
    # watching and cannot yield a full-window fraction.
    start = slot_start(since, slot_minutes, tz)
    if start < since:
        start += step

    out: list[tuple[datetime, datetime]] = []
    while start + step <= until:
        out.append((start, start + step))
        start += step
        if max_slots is not None and len(out) >= max_slots:
            break
    return out


# ── Fraction from a state timeline ────────────────────────────────────────────


def occupied_fraction(
    timeline: Iterable[tuple[datetime, bool]],
    window_start: datetime,
    window_end: datetime,
) -> float | None:
    """Fraction of [window_start, window_end) during which the entity read occupied.

    `timeline` is (changed_at, occupied) pairs in any order; entries outside the
    window are used only to establish the state in effect at its edges.

    Returns None -- never 0.0 -- when the window's occupancy cannot be
    established:
      * empty timeline (no recorder data: purged, or an outage);
      * every entry is at or after window_start, so the leading segment's state
        is unknown and attributing it either way would be a guess.

    None means "no observation"; the caller must write nothing. Writing 0.0 for
    an unknown window would read downstream as a certainty that the room is
    free -- the exact inversion of a fail-safe default.
    """
    window_start = _as_utc(window_start)
    window_end = _as_utc(window_end)
    total = (window_end - window_start).total_seconds()
    if total <= 0:
        return None

    entries = sorted(((_as_utc(t), bool(v)) for t, v in timeline), key=lambda e: e[0])
    if not entries:
        return None

    # State in effect when the window opens: the last transition at or before it.
    prior = [e for e in entries if e[0] <= window_start]
    if not prior:
        return None

    current = prior[-1][1]
    cursor = window_start
    occupied_seconds = 0.0

    for changed_at, state in entries:
        if changed_at <= window_start:
            continue
        if changed_at >= window_end:
            break
        if current:
            occupied_seconds += (changed_at - cursor).total_seconds()
        cursor = changed_at
        current = state

    if current:
        occupied_seconds += (window_end - cursor).total_seconds()

    return min(1.0, max(0.0, occupied_seconds / total))


# ── Observations ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PriorObservation:
    """One closed slot's occupied fraction.

    `at` is the UTC instant the observed slot BEGAN, which makes it a stable
    identity for that slot occurrence and therefore the dedupe key.
    """

    f: float
    at: datetime
    src: ObservationSource

    def to_json(self) -> dict[str, Any]:
        return {"f": round(self.f, 4), "at": _as_utc(self.at).isoformat(), "src": self.src}

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> PriorObservation | None:
        """Parse one stored observation. Returns None on anything malformed.

        Tolerant by design: one corrupt element must cost that observation, not
        the whole slot -- a row that fails to parse would otherwise take an
        entire (entity, dow, slot) out of service permanently.
        """
        try:
            f = float(raw["f"])
            at = datetime.fromisoformat(str(raw["at"]))
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(f) or not 0.0 <= f <= 1.0:
            return None
        src = raw.get("src", "native")
        if src not in ("native", "backfill"):
            src = "native"
        return cls(f=f, at=_as_utc(at), src=src)


def parse_observations(raw: Any) -> list[PriorObservation]:
    """Decode the stored `observations` JSON. Never raises."""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []
    out = [PriorObservation.from_json(item) for item in raw if isinstance(item, dict)]
    return [o for o in out if o is not None]


def merge_observations(
    existing: Sequence[PriorObservation],
    incoming: Sequence[PriorObservation],
    retention: int,
) -> list[PriorObservation]:
    """Merge, dedupe on `at`, sort oldest-first, keep the newest `retention`.

    Two rules, both load-bearing:

    1. DEDUPE ON `at`. Re-observing a slot instant replaces rather than appends.
       This is what makes the one-time backfill safe to re-run and what stops a
       process restart mid-catch-up from double-counting a slot.

    2. NATIVE BEATS BACKFILL at the same `at`, whichever arrives later. Without
       this, re-running the backfill over a window the learner has since observed
       live would DOWNGRADE those native observations to backfilled ones and
       silently decrement `native_count` -- walking a slot's confidence backwards
       from "good" to "thin" and, at A4, un-actuating a rule that had earned its
       soak. Among two observations of the same source, the later one wins.

    FIFO eviction at the retention cap also gives the backfill's aging-out for
    free: backfilled observations are the oldest in the array, so incoming native
    samples displace them first.
    """
    by_at: dict[datetime, PriorObservation] = {}
    for obs in list(existing) + list(incoming):
        at = _as_utc(obs.at)
        prev = by_at.get(at)
        if prev is not None and prev.src == "native" and obs.src == "backfill":
            continue  # rule 2: never let a backfill overwrite a native sample
        by_at[at] = PriorObservation(f=obs.f, at=at, src=obs.src)

    merged = sorted(by_at.values(), key=lambda o: o.at)
    if retention > 0 and len(merged) > retention:
        merged = merged[-retention:]
    return merged


def summarize(
    observations: Sequence[PriorObservation],
) -> tuple[float, float | None, int]:
    """(mean_occupied, sample_stddev_or_None, native_count).

    Sample (n-1) stddev, not population: these are a sample of an ongoing
    process, and n is small enough (<=8) that the difference is material. None
    below n=2, where the statistic is undefined rather than zero.
    """
    if not observations:
        return 0.0, None, 0
    values = [o.f for o in observations]
    mean = sum(values) / len(values)
    stddev = statistics.stdev(values) if len(values) >= 2 else None
    native = sum(1 for o in observations if o.src == "native")
    return mean, stddev, native


def confidence_for(native_count: int, sample_count: int, min_slot_samples: int) -> str:
    """Confidence label for a slot.

    Only NATIVE observations promote to "good". Backfilled samples make a slot
    usable-but-thin on day 1 -- which is the whole reason the backfill exists --
    but they must never be able to satisfy the actuation floor, because they are
    seeded from a single historical pass rather than accumulated across the weeks
    the soak is meant to observe.
    """
    if sample_count <= 0:
        return CONFIDENCE_UNAVAILABLE
    if native_count >= min_slot_samples:
        return CONFIDENCE_GOOD
    return CONFIDENCE_THIN


@dataclass(frozen=True)
class SlotPrior:
    """What PR A2's `opportunity()` reads. One (entity, dow, slot)."""

    entity_id: str
    day_of_week: int
    slot: int
    mean_occupied: float
    stddev_occupied: float | None
    native_count: int
    sample_count: int
    confidence: str
    last_sample_at: datetime | None = None

    @classmethod
    def unavailable(cls, entity_id: str, day_of_week: int, slot: int) -> SlotPrior:
        """A slot with no observations at all.

        mean_occupied is 0.0 only because the field needs a number; the
        `confidence == "unavailable"` label is the part callers must branch on.
        A2's fail-open matrix returns PASS on this and names the degradation in
        its reason string -- it must never be read as "0% occupied".
        """
        return cls(
            entity_id=entity_id,
            day_of_week=day_of_week,
            slot=slot,
            mean_occupied=0.0,
            stddev_occupied=None,
            native_count=0,
            sample_count=0,
            confidence=CONFIDENCE_UNAVAILABLE,
        )

    @property
    def p_clear(self) -> float:
        """1 - mean_occupied. Meaningless unless confidence != "unavailable"."""
        return 1.0 - self.mean_occupied


# ── Store ─────────────────────────────────────────────────────────────────────


class PriorStoreProtocol(Protocol):
    """The surface the learner and the backfill depend on.

    Declared so both can be exercised against a fake in tests without a live
    MariaDB, and so a future caching layer can slot in without touching them.
    """

    async def read_slot(self, entity_id: str, day_of_week: int, slot: int) -> SlotPrior: ...

    async def read_observations(
        self, entity_id: str, day_of_week: int, slot: int
    ) -> list[PriorObservation]: ...

    async def record(self, entity_id: str, observations: Sequence[PriorObservation]) -> int: ...


class PriorStore:
    """SQLAlchemy-backed `cortex_occupancy_priors` reader/writer.

    Read-then-write rather than a dialect-specific upsert: the learner is a
    single writer inside one loop task, so there is no contention to lose a race
    to, and portable SQL keeps the table testable on SQLite.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        slot_minutes: int = 30,
        retention: int = 8,
        min_slot_samples: int = 3,
        tz: ZoneInfo = HOUSEHOLD_TZ,
    ) -> None:
        self._sessions = session_factory
        self._slot_minutes = slot_minutes
        self._retention = retention
        self._min_slot_samples = min_slot_samples
        self._tz = tz

    async def read_observations(
        self, entity_id: str, day_of_week: int, slot: int
    ) -> list[PriorObservation]:
        async with self._sessions() as session:
            row = await self._select(session, entity_id, day_of_week, slot)
        return parse_observations(row["observations"]) if row else []

    async def read_slot(self, entity_id: str, day_of_week: int, slot: int) -> SlotPrior:
        """Fetch one slot's prior. An absent row is "unavailable", not "0% occupied"."""
        observations = await self.read_observations(entity_id, day_of_week, slot)
        return self.build_prior(entity_id, day_of_week, slot, observations)

    def build_prior(
        self,
        entity_id: str,
        day_of_week: int,
        slot: int,
        observations: Sequence[PriorObservation],
    ) -> SlotPrior:
        if not observations:
            return SlotPrior.unavailable(entity_id, day_of_week, slot)
        mean, stddev, native = summarize(observations)
        return SlotPrior(
            entity_id=entity_id,
            day_of_week=day_of_week,
            slot=slot,
            mean_occupied=mean,
            stddev_occupied=stddev,
            native_count=native,
            sample_count=len(observations),
            confidence=confidence_for(native, len(observations), self._min_slot_samples),
            last_sample_at=observations[-1].at,
        )

    async def record(self, entity_id: str, observations: Sequence[PriorObservation]) -> int:
        """Merge observations into their slots. Returns the number of rows written.

        Observations are grouped by their own `at` instant, so a caller may pass a
        mixed batch spanning many slots (the backfill does exactly that) in one
        call.
        """
        if not observations:
            return 0

        grouped: dict[tuple[int, int], list[PriorObservation]] = {}
        for obs in observations:
            grouped.setdefault(slot_key(obs.at, self._slot_minutes, self._tz), []).append(obs)

        written = 0
        async with self._sessions() as session:
            for (dow, slot), batch in grouped.items():
                row = await self._select(session, entity_id, dow, slot)
                existing = parse_observations(row["observations"]) if row else []
                merged = merge_observations(existing, batch, self._retention)
                if merged == existing:
                    continue  # idempotent no-op: nothing changed, skip the write
                await self._write(session, entity_id, dow, slot, merged, exists=row is not None)
                written += 1
            await session.commit()
        return written

    # -- SQL ------------------------------------------------------------------

    async def _select(
        self, session: AsyncSession, entity_id: str, day_of_week: int, slot: int
    ) -> dict[str, Any] | None:
        result = await session.execute(
            sa.text(
                f"SELECT observations FROM {TABLE} "  # noqa: S608 - constant table name
                "WHERE entity_id = :entity_id AND day_of_week = :dow AND slot = :slot"
            ),
            {"entity_id": entity_id, "dow": day_of_week, "slot": slot},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def _write(
        self,
        session: AsyncSession,
        entity_id: str,
        day_of_week: int,
        slot: int,
        observations: Sequence[PriorObservation],
        *,
        exists: bool,
    ) -> None:
        mean, stddev, native = summarize(observations)
        # Timestamps are bound as parameters rather than emitted as NOW(): NOW()
        # is MariaDB/MySQL syntax, and the settings validator accepts a SQLite
        # DATABASE_URL, so a dialect-specific function here would work in
        # production and fail on any SQLite-backed test or local run. Naive UTC
        # matches how the rest of the module writes DATETIME columns.
        now = datetime.now(tz=UTC).replace(tzinfo=None)
        params = {
            "entity_id": entity_id,
            "dow": day_of_week,
            "slot": slot,
            "observations": json.dumps([o.to_json() for o in observations]),
            "native_count": native,
            "mean_occupied": round(mean, 4),
            "stddev_occupied": None if stddev is None else round(stddev, 4),
            "last_sample_at": _as_utc(observations[-1].at).replace(tzinfo=None),
            "now": now,
        }
        if exists:
            await session.execute(
                sa.text(
                    f"UPDATE {TABLE} SET "  # noqa: S608 - constant table name
                    "observations = :observations, native_count = :native_count, "
                    "mean_occupied = :mean_occupied, stddev_occupied = :stddev_occupied, "
                    "last_sample_at = :last_sample_at, updated_at = :now "
                    "WHERE entity_id = :entity_id AND day_of_week = :dow AND slot = :slot"
                ),
                params,
            )
        else:
            await session.execute(
                sa.text(
                    f"INSERT INTO {TABLE} "  # noqa: S608 - constant table name
                    "(entity_id, day_of_week, slot, observations, native_count, "
                    " mean_occupied, stddev_occupied, last_sample_at, created_at, updated_at) "
                    "VALUES (:entity_id, :dow, :slot, :observations, :native_count, "
                    " :mean_occupied, :stddev_occupied, :last_sample_at, :now, :now)"
                ),
                params,
            )


# ── Learner ───────────────────────────────────────────────────────────────────


@dataclass
class LearnerRun:
    """What one close-out pass did. Logged; also the return value tests assert on."""

    slots_closed: int = 0
    observations_written: int = 0
    slots_no_data: int = 0
    entities_failed: int = 0
    watermark_advanced_to: datetime | None = None


class PriorLearner:
    """Closes out completed slots and records one observation per entity per slot.

    Reads the fraction from HA history rather than from tick samples -- see the
    module docstring. That also makes multi-slot catch-up after a restart exact
    rather than approximate: one history call covers the whole gap and is split
    into slots locally.
    """

    def __init__(
        self,
        store: PriorStoreProtocol,
        ha_adapter: Any,
        redis_client: Any,
        *,
        entities: Sequence[str],
        slot_minutes: int = 30,
        max_catchup_slots: int = 48,
        max_lookback_days: int = 28,
        tz: ZoneInfo = HOUSEHOLD_TZ,
    ) -> None:
        self._store = store
        self._ha = ha_adapter
        self._redis = redis_client
        self._entities = list(entities)
        self._slot_minutes = slot_minutes
        self._max_catchup_slots = max_catchup_slots
        self._max_lookback_days = max_lookback_days
        self._tz = tz

    async def _read_watermark(self, now: datetime) -> datetime | None:
        try:
            raw = await self._redis.get(WATERMARK_KEY)
        except Exception as exc:
            log.warning("prior_learner_watermark_read_failed", error=str(exc))
            return None
        if not raw:
            return None
        try:
            return _as_utc(datetime.fromisoformat(raw))
        except (TypeError, ValueError):
            log.warning("prior_learner_watermark_unparseable", raw=str(raw))
            return None

    async def _write_watermark(self, value: datetime) -> None:
        try:
            await self._redis.set(WATERMARK_KEY, _as_utc(value).isoformat())
        except Exception as exc:
            # Non-fatal: the worst case is re-observing slots we already recorded,
            # and merge_observations dedupes those on `at`.
            log.warning("prior_learner_watermark_write_failed", error=str(exc))

    async def close_out_due_slots(self, now: datetime) -> LearnerRun:
        """Close out every slot completed since the watermark. Never raises.

        On a cold start (no watermark) this seeds the watermark at the current
        slot boundary and returns without recording anything: historical slots
        are the BACKFILL's job, and replaying them here would duplicate its work
        against a much smaller lookback.
        """
        run = LearnerRun()
        now = _as_utc(now)

        watermark = await self._read_watermark(now)
        if watermark is None:
            seed = slot_start(now, self._slot_minutes, self._tz)
            await self._write_watermark(seed)
            log.info("prior_learner_watermark_seeded", at=seed.isoformat())
            run.watermark_advanced_to = seed
            return run

        # Never chase history the recorder has already purged. Without this an
        # instance that has been down for months would spend every tick issuing
        # history reads over windows that can only ever come back empty.
        floor = now - timedelta(days=self._max_lookback_days)
        if watermark < floor:
            log.warning(
                "prior_learner_watermark_clamped",
                was=watermark.isoformat(),
                now_at=floor.isoformat(),
                reason="older than recorder lookback",
            )
            watermark = slot_start(floor, self._slot_minutes, self._tz)

        slots = iter_completed_slots(
            watermark, now, self._slot_minutes, self._tz, self._max_catchup_slots
        )
        if not slots:
            return run

        window_start, window_end = slots[0][0], slots[-1][1]
        any_success = False

        for entity_id in self._entities:
            timeline = await self._fetch_timeline(entity_id, window_start, window_end)
            if timeline is None:
                run.entities_failed += 1
                continue
            any_success = True

            observations: list[PriorObservation] = []
            for start, end in slots:
                fraction = occupied_fraction(timeline, start, end)
                if fraction is None:
                    run.slots_no_data += 1
                    continue
                observations.append(PriorObservation(f=fraction, at=start, src="native"))

            if observations:
                try:
                    run.observations_written += await self._store.record(entity_id, observations)
                except Exception as exc:
                    log.error(
                        "prior_learner_store_write_failed", entity_id=entity_id, error=str(exc)
                    )

        run.slots_closed = len(slots)

        # Advance only if at least one entity's history read SUCCEEDED. A total
        # HA-history outage must leave the watermark where it is so the slots are
        # retried; advancing through an outage would silently punch a permanent
        # hole in the table that nothing ever notices.
        if any_success:
            await self._write_watermark(window_end)
            run.watermark_advanced_to = window_end
        else:
            log.warning(
                "prior_learner_all_entities_failed",
                slots=len(slots),
                window_start=window_start.isoformat(),
            )

        log.info(
            "prior_learner_slots_closed",
            slots=run.slots_closed,
            written=run.observations_written,
            no_data=run.slots_no_data,
            entities_failed=run.entities_failed,
        )
        return run

    async def _fetch_timeline(
        self, entity_id: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, bool]] | None:
        """History read for one entity. None = the call failed; [] = no data."""
        try:
            return await self._ha.get_state_history(entity_id, start, end)
        except Exception as exc:
            log.warning("prior_learner_history_failed", entity_id=entity_id, error=str(exc))
            return None
