"""Tests for the rolling occupancy prior learner (PR A1).

Spec: cortex_vacuum_patience_and_pause_resume_implementation_spec.md §4.2, §9.1

§9.1's row for this PR asks for: slot close-out from a synthetic state timeline;
multi-slot gap after a simulated restart; retention eviction at 8 observations;
backfill idempotency (run twice -> identical table); native-vs-backfill
confidence accounting.

Two things beyond that list are covered here because they are the failure modes
that would do real damage rather than merely be wrong:

  * EMPTY IS NOT ZERO. Every path that cannot establish a slot's occupancy must
    yield None and write nothing. A 0.0 written for a purged window reads
    downstream as p_clear = 1.0 -- "this slot is always free, dispatch now" --
    so missing data would become an argument FOR running the vacuum. This bites
    for real: HA's recorder keeps 20 days (configuration.yaml:13), not the 28
    the spec assumed, so the backfill WILL meet empty windows on every run.

  * NATIVE BEATS BACKFILL at the same instant. Without it, re-running the
    backfill over a window the learner had since observed live would decrement
    native_count and walk a slot's confidence backwards from "good" to "thin" --
    silently un-actuating, at A4, a rule that had earned its soak.

Numbers here are hand-computed and asserted as numbers, per §9.1's "assert the
numbers, not just 'no exception'".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio

from cortex_python.adapters.ha_rest_adapter import HARestAdapter, _parse_history_payload
from cortex_python.modules.vacuumops.priors import (
    WATERMARK_KEY,
    PriorLearner,
    PriorObservation,
    PriorStore,
    confidence_for,
    iter_completed_slots,
    merge_observations,
    occupied_fraction,
    parse_observations,
    slot_key,
    slot_start,
    slots_per_day,
    summarize,
)
from cortex_python.modules.vacuumops.priors_backfill import backfill_priors

# 2026-09-01 is a Tuesday (weekday() == 1). 10:00 local PDT == 17:00 UTC.
TUE_1000_LOCAL = datetime(2026, 9, 1, 17, 0, tzinfo=UTC)


def _utc(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


# ── Test doubles ──────────────────────────────────────────────────────────────


class FakePriorStore:
    """In-memory stand-in for PriorStore.

    Uses the real merge_observations so learner/backfill orchestration is tested
    against the genuine merge semantics rather than a simplification that could
    hide a dedupe or precedence bug. merge_observations itself is tested directly
    below.
    """

    def __init__(self, slot_minutes: int = 30, retention: int = 8) -> None:
        self.rows: dict[tuple[str, int, int], list[PriorObservation]] = {}
        self.slot_minutes = slot_minutes
        self.retention = retention
        self.record_calls = 0

    async def record(self, entity_id: str, observations: Any) -> int:
        self.record_calls += 1
        written = 0
        grouped: dict[tuple[int, int], list[PriorObservation]] = {}
        for obs in observations:
            grouped.setdefault(slot_key(obs.at, self.slot_minutes), []).append(obs)
        for (dow, slot), batch in grouped.items():
            key = (entity_id, dow, slot)
            existing = self.rows.get(key, [])
            merged = merge_observations(existing, batch, self.retention)
            if merged != existing:
                self.rows[key] = merged
                written += 1
        return written

    async def read_observations(
        self, entity_id: str, day_of_week: int, slot: int
    ) -> list[PriorObservation]:
        return list(self.rows.get((entity_id, day_of_week, slot), []))

    async def read_slot(
        self, entity_id: str, day_of_week: int, slot: int
    ) -> Any:  # pragma: no cover
        raise NotImplementedError

    def snapshot(self) -> dict[tuple[str, int, int], list[tuple[float, str, str]]]:
        """Comparable, order-sensitive view — for the byte-identical idempotency assert."""
        return {
            k: [(round(o.f, 6), o.at.isoformat(), o.src) for o in v]
            for k, v in sorted(self.rows.items())
        }


class FakeHAAdapter:
    """History source. `timeline` maps entity_id -> list[(datetime, bool)].

    An entity listed in `fail` returns None (call failed) rather than [] (no
    data) — the two are different signals and the learner branches on them.
    """

    def __init__(
        self,
        timelines: dict[str, list[tuple[datetime, bool]]] | None = None,
        fail: set[str] | None = None,
    ) -> None:
        self.timelines = timelines or {}
        self.fail = fail or set()
        self.calls: list[tuple[str, datetime, datetime]] = []

    async def get_state_history(
        self, entity_id: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, bool]] | None:
        """Models HA's actual contract, including the boundary record.

        HA prepends the state in EFFECT at start_time when the recorder holds
        anything earlier. That prepended record is what makes the leading segment
        of a window attributable, and therefore what makes chunked reads work at
        all — a fake that only returned records strictly inside the range would
        make every chunk after the first look like no-data.

        A window with nothing at or before it yields [] — genuinely no data,
        which is the purged case.
        """
        self.calls.append((entity_id, start, end))
        if entity_id in self.fail:
            return None
        series = sorted(self.timelines.get(entity_id, []), key=lambda e: e[0])
        before = [e for e in series if e[0] <= start]
        inside = [e for e in series if start < e[0] < end]
        if not before:
            return list(inside) if inside else []
        return [(start, before[-1][1]), *inside]


class FakeRedis:
    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.data: dict[str, str] = dict(initial or {})

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, **_: Any) -> bool:
        self.data[key] = value
        return True


# ── Slot arithmetic ───────────────────────────────────────────────────────────


class TestSlotArithmetic:
    def test_slots_per_day(self):
        assert slots_per_day(30) == 48
        assert slots_per_day(60) == 24
        assert slots_per_day(15) == 96

    @pytest.mark.parametrize("bad", [0, -30, 7, 50, 1441])
    def test_slot_length_must_divide_the_day(self, bad):
        """A non-dividing slot length would make one slot per day shorter than
        every other sample of the same index — silently comparing a 20-minute
        window against 30-minute ones. Reject it at the door."""
        with pytest.raises(ValueError):
            slots_per_day(bad)

    def test_slot_key_is_local_not_utc(self):
        """17:00 UTC on 2026-09-01 is 10:00 PDT Tuesday -> (dow=1, slot=20).

        Keyed in UTC it would land on slot 34, smearing the whole weekday
        pattern by seven slots.
        """
        assert slot_key(TUE_1000_LOCAL, 30) == (1, 20)

    def test_slot_key_boundaries(self):
        # Local midnight Tuesday = 07:00 UTC (PDT).
        assert slot_key(_utc(2026, 9, 1, 7, 0), 30) == (1, 0)
        assert slot_key(_utc(2026, 9, 1, 7, 29, 59), 30) == (1, 0)
        assert slot_key(_utc(2026, 9, 1, 7, 30), 30) == (1, 1)
        # One second before local midnight is still Monday's last slot.
        assert slot_key(_utc(2026, 9, 1, 6, 59, 59), 30) == (0, 47)

    def test_slot_start_floors_within_the_slot(self):
        start = slot_start(_utc(2026, 9, 1, 17, 19, 42), 30)
        assert start == _utc(2026, 9, 1, 17, 0)
        assert slot_start(start, 30) == start  # idempotent on a boundary

    def test_slot_start_handles_the_repeated_dst_hour(self):
        """2026-11-01 01:45 local occurs twice: 08:45 UTC (PDT) and 09:45 UTC (PST).

        Both are the same LOCAL slot — (Sunday, 3) — but they are different
        instants and must floor to different slot STARTS, one hour apart. Losing
        the `fold` here would collapse both into one observation, so the repeated
        hour of the fall-back day would overwrite itself instead of contributing
        two samples.
        """
        first = _utc(2026, 11, 1, 8, 45)
        second = _utc(2026, 11, 1, 9, 45)
        assert slot_key(first, 30) == (6, 3)
        assert slot_key(second, 30) == (6, 3)
        assert slot_start(first, 30) == _utc(2026, 11, 1, 8, 30)
        assert slot_start(second, 30) == _utc(2026, 11, 1, 9, 30)
        assert slot_start(first, 30) != slot_start(second, 30)

    def test_naive_datetimes_are_treated_as_utc(self):
        assert slot_key(datetime(2026, 9, 1, 17, 0), 30) == slot_key(TUE_1000_LOCAL, 30)


class TestIterCompletedSlots:
    def test_only_fully_completed_slots_are_emitted(self):
        """A half-observed slot would yield a fraction over a shorter window than
        its siblings and corrupt that slot's mean."""
        slots = iter_completed_slots(
            _utc(2026, 9, 1, 17, 0), _utc(2026, 9, 1, 18, 20), 30
        )
        assert slots == [
            (_utc(2026, 9, 1, 17, 0), _utc(2026, 9, 1, 17, 30)),
            (_utc(2026, 9, 1, 17, 30), _utc(2026, 9, 1, 18, 0)),
        ]

    def test_partial_leading_slot_is_skipped(self):
        """`since` mid-slot means that slot was already elapsing when we started
        watching — it cannot yield a full-window fraction."""
        slots = iter_completed_slots(
            _utc(2026, 9, 1, 17, 10), _utc(2026, 9, 1, 18, 5), 30
        )
        assert slots == [(_utc(2026, 9, 1, 17, 30), _utc(2026, 9, 1, 18, 0))]

    def test_empty_when_no_slot_has_completed(self):
        assert (
            iter_completed_slots(_utc(2026, 9, 1, 17, 0), _utc(2026, 9, 1, 17, 20), 30)
            == []
        )
        assert (
            iter_completed_slots(_utc(2026, 9, 1, 18, 0), _utc(2026, 9, 1, 17, 0), 30)
            == []
        )

    def test_multi_slot_gap_after_a_restart(self):
        """§9.1: 'multi-slot gap after a simulated restart'. A 6-hour outage owes
        12 slots, oldest first."""
        slots = iter_completed_slots(
            _utc(2026, 9, 1, 12, 0), _utc(2026, 9, 1, 18, 0), 30
        )
        assert len(slots) == 12
        assert slots[0][0] == _utc(2026, 9, 1, 12, 0)
        assert slots[-1][1] == _utc(2026, 9, 1, 18, 0)

    def test_max_slots_caps_the_batch_oldest_first(self):
        slots = iter_completed_slots(
            _utc(2026, 9, 1, 0, 0), _utc(2026, 9, 3, 0, 0), 30, max_slots=4
        )
        assert len(slots) == 4
        assert slots[0][0] == _utc(
            2026, 9, 1, 0, 0
        )  # oldest first, so catch-up progresses

    def test_slots_stay_contiguous_across_the_dst_boundary(self):
        """The fall-back day is 25 hours long: 50 slots, no gap and no overlap."""
        slots = iter_completed_slots(
            _utc(2026, 11, 1, 7, 0), _utc(2026, 11, 2, 8, 0), 30, max_slots=None
        )
        assert len(slots) == 50
        for (_, end), (nxt, _) in zip(slots[:-1], slots[1:], strict=True):
            assert end == nxt


# ── occupied_fraction ─────────────────────────────────────────────────────────


class TestOccupiedFraction:
    def test_hand_computed_fraction(self):
        """Window 17:00–17:30 (1800 s). On from 16:50, off at 17:10, on at 17:25.
        Occupied = 600 s + 300 s = 900 s -> exactly 0.5."""
        timeline = [
            (_utc(2026, 9, 1, 16, 50), True),
            (_utc(2026, 9, 1, 17, 10), False),
            (_utc(2026, 9, 1, 17, 25), True),
        ]
        f = occupied_fraction(
            timeline, _utc(2026, 9, 1, 17, 0), _utc(2026, 9, 1, 17, 30)
        )
        assert f == pytest.approx(0.5)

    def test_fully_occupied_and_fully_clear(self):
        start, end = _utc(2026, 9, 1, 17, 0), _utc(2026, 9, 1, 17, 30)
        assert occupied_fraction([(_utc(2026, 9, 1, 12, 0), True)], start, end) == 1.0
        # A genuine, known 0.0 — distinct from the None cases below.
        assert occupied_fraction([(_utc(2026, 9, 1, 12, 0), False)], start, end) == 0.0

    def test_unsorted_input_is_handled(self):
        timeline = [
            (_utc(2026, 9, 1, 17, 25), True),
            (_utc(2026, 9, 1, 16, 50), True),
            (_utc(2026, 9, 1, 17, 10), False),
        ]
        f = occupied_fraction(
            timeline, _utc(2026, 9, 1, 17, 0), _utc(2026, 9, 1, 17, 30)
        )
        assert f == pytest.approx(0.5)

    def test_transitions_outside_the_window_are_clipped_not_counted(self):
        """On at 16:00, off at 17:45 — the window is wholly inside one 'on' run."""
        timeline = [(_utc(2026, 9, 1, 16, 0), True), (_utc(2026, 9, 1, 17, 45), False)]
        assert (
            occupied_fraction(
                timeline, _utc(2026, 9, 1, 17, 0), _utc(2026, 9, 1, 17, 30)
            )
            == 1.0
        )

    def test_empty_timeline_is_none_not_zero(self):
        """THE load-bearing assertion of this module. A purged window returns
        nothing; 0.0 would mean 'always free' and argue for dispatching."""
        assert (
            occupied_fraction([], _utc(2026, 9, 1, 17, 0), _utc(2026, 9, 1, 17, 30))
            is None
        )

    def test_no_state_known_at_window_open_is_none_not_zero(self):
        """Every record is INSIDE the window, so the leading segment's state is
        unknown. Attributing it either way is a guess; refuse."""
        timeline = [(_utc(2026, 9, 1, 17, 5), True)]
        assert (
            occupied_fraction(
                timeline, _utc(2026, 9, 1, 17, 0), _utc(2026, 9, 1, 17, 30)
            )
            is None
        )

    def test_records_only_after_the_window_are_none(self):
        timeline = [(_utc(2026, 9, 1, 19, 0), True)]
        assert (
            occupied_fraction(
                timeline, _utc(2026, 9, 1, 17, 0), _utc(2026, 9, 1, 17, 30)
            )
            is None
        )

    def test_degenerate_window_is_none(self):
        t = [(_utc(2026, 9, 1, 12, 0), True)]
        assert (
            occupied_fraction(t, _utc(2026, 9, 1, 17, 0), _utc(2026, 9, 1, 17, 0))
            is None
        )

    def test_result_is_always_within_zero_and_one(self):
        timeline = [
            (_utc(2026, 9, 1, 10, 0), True),
            (_utc(2026, 9, 1, 17, 7), False),
            (_utc(2026, 9, 1, 17, 8), True),
            (_utc(2026, 9, 1, 23, 0), False),
        ]
        f = occupied_fraction(
            timeline, _utc(2026, 9, 1, 17, 0), _utc(2026, 9, 1, 17, 30)
        )
        assert 0.0 <= f <= 1.0
        # 1800 s window minus the single 60 s off-segment.
        assert f == pytest.approx(1740 / 1800)


# ── Observations, merging, retention ──────────────────────────────────────────


def _obs(f: float, at: datetime, src: str = "native") -> PriorObservation:
    return PriorObservation(f=f, at=at, src=src)  # type: ignore[arg-type]


class TestMergeObservations:
    def test_appends_and_sorts_oldest_first(self):
        base = _utc(2026, 9, 1, 17, 0)
        merged = merge_observations(
            [_obs(0.2, base)], [_obs(0.4, base + timedelta(days=7))], retention=8
        )
        assert [o.f for o in merged] == [0.2, 0.4]

    def test_dedupes_on_the_slot_start_instant(self):
        """Re-observing a slot replaces rather than appends. This is what makes a
        backfill re-run and a mid-catch-up restart both idempotent."""
        base = _utc(2026, 9, 1, 17, 0)
        merged = merge_observations([_obs(0.2, base)], [_obs(0.9, base)], retention=8)
        assert len(merged) == 1
        assert merged[0].f == 0.9

    def test_backfill_never_overwrites_a_native_observation(self):
        """Re-running the backfill after the learner has been live must not walk
        native_count backwards — that would demote a slot from 'good' to 'thin'
        and, at A4, un-actuate a rule that had already earned its soak."""
        base = _utc(2026, 9, 1, 17, 0)
        merged = merge_observations(
            [_obs(0.2, base, "native")], [_obs(0.9, base, "backfill")], retention=8
        )
        assert len(merged) == 1
        assert merged[0].src == "native"
        assert merged[0].f == 0.2

    def test_native_does_overwrite_a_backfilled_observation(self):
        """The other direction is an upgrade and must be allowed: a live
        observation of a slot is strictly better evidence than a seeded one."""
        base = _utc(2026, 9, 1, 17, 0)
        merged = merge_observations(
            [_obs(0.9, base, "backfill")], [_obs(0.2, base, "native")], retention=8
        )
        assert merged[0].src == "native"
        assert merged[0].f == 0.2

    def test_retention_evicts_oldest_first(self):
        """§9.1: 'retention eviction at 8 observations'."""
        base = _utc(2026, 9, 1, 17, 0)
        incoming = [_obs(i / 10, base + timedelta(days=7 * i)) for i in range(11)]
        merged = merge_observations([], incoming, retention=8)
        assert len(merged) == 8
        assert [round(o.f, 1) for o in merged] == [
            0.3,
            0.4,
            0.5,
            0.6,
            0.7,
            0.8,
            0.9,
            1.0,
        ]

    def test_fifo_eviction_ages_backfilled_rows_out_first(self):
        """§4.2 wants backfilled rows aged out as native samples arrive. Because
        backfill seeds the PAST, FIFO eviction delivers that for free — assert it
        rather than assume it."""
        base = _utc(2026, 9, 1, 17, 0)
        seeded = [_obs(0.5, base + timedelta(days=7 * i), "backfill") for i in range(8)]
        native = [
            _obs(0.1, base + timedelta(days=7 * (8 + i)), "native") for i in range(3)
        ]
        merged = merge_observations(seeded, native, retention=8)
        assert len(merged) == 8
        assert sum(1 for o in merged if o.src == "native") == 3
        assert sum(1 for o in merged if o.src == "backfill") == 5


class TestSummarizeAndConfidence:
    def test_mean_and_sample_stddev(self):
        base = _utc(2026, 9, 1, 17, 0)
        obs = [_obs(0.2, base), _obs(0.4, base + timedelta(days=7))]
        mean, stddev, native = summarize(obs)
        assert mean == pytest.approx(0.3)
        # Sample (n-1) stddev of {0.2, 0.4} is 0.1414..., not the population 0.1.
        assert stddev == pytest.approx(0.14142, abs=1e-4)
        assert native == 2

    def test_stddev_is_none_below_two_samples(self):
        mean, stddev, native = summarize([_obs(0.7, _utc(2026, 9, 1, 17, 0))])
        assert mean == 0.7
        assert stddev is None
        assert native == 1

    def test_empty_summarizes_to_zero_with_no_samples(self):
        assert summarize([]) == (0.0, None, 0)

    def test_native_count_excludes_backfill(self):
        base = _utc(2026, 9, 1, 17, 0)
        obs = [
            _obs(0.2, base, "backfill"),
            _obs(0.4, base + timedelta(days=7), "backfill"),
            _obs(0.6, base + timedelta(days=14), "native"),
        ]
        mean, _, native = summarize(obs)
        assert native == 1
        # Backfilled samples DO move the mean — that is what removes the cold start.
        assert mean == pytest.approx(0.4)

    @pytest.mark.parametrize(
        ("native", "total", "expected"),
        [
            (0, 0, "unavailable"),
            (0, 1, "thin"),
            (2, 4, "thin"),
            (3, 3, "good"),
            (5, 8, "good"),
        ],
    )
    def test_confidence_accounting(self, native, total, expected):
        """§9.1: 'native-vs-backfill confidence accounting'. Only native samples
        promote to good; backfill can produce 'thin' but never 'good'."""
        assert confidence_for(native, total, min_slot_samples=3) == expected

    def test_backfill_alone_can_never_reach_good(self):
        base = _utc(2026, 9, 1, 17, 0)
        obs = [_obs(0.5, base + timedelta(days=7 * i), "backfill") for i in range(8)]
        _, _, native = summarize(obs)
        assert confidence_for(native, len(obs), 3) == "thin"


class TestObservationSerialisation:
    def test_round_trip(self):
        obs = _obs(0.4217, _utc(2026, 9, 1, 17, 0), "backfill")
        back = PriorObservation.from_json(obs.to_json())
        assert back is not None
        assert back.src == "backfill"
        assert back.f == pytest.approx(0.4217)
        assert back.at == obs.at

    def test_parse_observations_tolerates_a_corrupt_element(self):
        """One bad element must cost that observation, not the whole slot — a row
        that fails to parse would otherwise take an (entity, dow, slot) out of
        service permanently."""
        good = _obs(0.5, _utc(2026, 9, 1, 17, 0)).to_json()
        parsed = parse_observations(
            [
                good,
                {"f": "not-a-number", "at": "x"},
                {"nope": 1},
                "junk",
                {"f": 5.0, "at": good["at"]},
            ]
        )
        assert len(parsed) == 1
        assert parsed[0].f == 0.5

    def test_parse_observations_accepts_a_json_string(self):
        import json

        raw = json.dumps([_obs(0.25, _utc(2026, 9, 1, 17, 0)).to_json()])
        assert len(parse_observations(raw)) == 1

    @pytest.mark.parametrize("bad", [None, "not json", 42, {"a": 1}])
    def test_parse_observations_never_raises(self, bad):
        assert parse_observations(bad) == []


class TestBuildPrior:
    """PriorStore.build_prior is pure — no session is ever touched."""

    def _store(self) -> PriorStore:
        return PriorStore(
            lambda: None, slot_minutes=30, retention=8, min_slot_samples=3
        )  # type: ignore[arg-type]

    def test_absent_row_is_unavailable_not_zero_percent_occupied(self):
        prior = self._store().build_prior("binary_sensor.x", 1, 20, [])
        assert prior.confidence == "unavailable"
        assert prior.sample_count == 0
        # mean_occupied is 0.0 only because the field needs a number. A2 must
        # branch on the confidence label, never read this as "0% occupied" —
        # p_clear would otherwise be a perfect 1.0 for an entity we know nothing
        # about.
        assert prior.p_clear == 1.0

    def test_populated_row_summarises_and_labels(self):
        base = _utc(2026, 9, 1, 17, 0)
        obs = [
            _obs(0.2, base),
            _obs(0.4, base + timedelta(days=7)),
            _obs(0.6, base + timedelta(days=14)),
        ]
        prior = self._store().build_prior("binary_sensor.x", 1, 20, obs)
        assert prior.mean_occupied == pytest.approx(0.4)
        assert prior.p_clear == pytest.approx(0.6)
        assert prior.native_count == 3
        assert prior.confidence == "good"
        assert prior.last_sample_at == base + timedelta(days=14)


# ── HA history payload parsing ────────────────────────────────────────────────


class TestHistoryPayloadParsing:
    def test_minimal_response_shape(self):
        """First element carries the full dict, the rest are trimmed — HA's
        `minimal_response` shape. Both must parse."""
        payload = [
            [
                {
                    "entity_id": "binary_sensor.first_floor_occupancy_status",
                    "state": "on",
                    "last_changed": "2026-09-01T17:00:00+00:00",
                    "attributes": {"device_class": "occupancy"},
                },
                {"state": "off", "last_changed": "2026-09-01T17:10:00+00:00"},
                {"state": "on", "last_changed": "2026-09-01T17:25:00+00:00"},
            ]
        ]
        assert _parse_history_payload(payload) == [
            (_utc(2026, 9, 1, 17, 0), True),
            (_utc(2026, 9, 1, 17, 10), False),
            (_utc(2026, 9, 1, 17, 25), True),
        ]

    def test_unavailable_records_are_dropped_not_read_as_off(self):
        """A flapping integration must not read as 'the room emptied'. Dropping
        the non-state extends the surrounding known state across the gap."""
        payload = [
            [
                {"state": "on", "last_changed": "2026-09-01T17:00:00+00:00"},
                {"state": "unavailable", "last_changed": "2026-09-01T17:05:00+00:00"},
                {"state": "unknown", "last_changed": "2026-09-01T17:06:00+00:00"},
            ]
        ]
        parsed = _parse_history_payload(payload)
        assert parsed == [(_utc(2026, 9, 1, 17, 0), True)]
        # And the whole window still reads as occupied, not half-empty.
        assert (
            occupied_fraction(parsed, _utc(2026, 9, 1, 17, 0), _utc(2026, 9, 1, 17, 30))
            == 1.0
        )

    def test_a_window_of_only_non_states_parses_empty_so_it_reads_as_no_data(self):
        payload = [
            [{"state": "unavailable", "last_changed": "2026-09-01T17:00:00+00:00"}]
        ]
        parsed = _parse_history_payload(payload)
        assert parsed == []
        assert (
            occupied_fraction(parsed, _utc(2026, 9, 1, 17, 0), _utc(2026, 9, 1, 17, 30))
            is None
        )

    def test_truthy_states_match_the_synth(self):
        payload = [
            [
                {"state": "on", "last_changed": "2026-09-01T17:00:00+00:00"},
                {"state": "off", "last_changed": "2026-09-01T17:01:00+00:00"},
                {"state": "true", "last_changed": "2026-09-01T17:02:00+00:00"},
                {"state": "1", "last_changed": "2026-09-01T17:03:00+00:00"},
            ]
        ]
        assert [v for _, v in _parse_history_payload(payload)] == [
            True,
            False,
            True,
            True,
        ]

    def test_zulu_and_naive_timestamps_normalise_to_utc(self):
        payload = [
            [
                {"state": "on", "last_changed": "2026-09-01T17:00:00Z"},
                {"state": "off", "last_changed": "2026-09-01T17:05:00"},
            ]
        ]
        assert _parse_history_payload(payload) == [
            (_utc(2026, 9, 1, 17, 0), True),
            (_utc(2026, 9, 1, 17, 5), False),
        ]

    def test_output_is_sorted_even_if_ha_is_not(self):
        payload = [
            [
                {"state": "off", "last_changed": "2026-09-01T17:20:00+00:00"},
                {"state": "on", "last_changed": "2026-09-01T17:00:00+00:00"},
            ]
        ]
        assert [t for t, _ in _parse_history_payload(payload)] == [
            _utc(2026, 9, 1, 17, 0),
            _utc(2026, 9, 1, 17, 20),
        ]

    def test_falls_back_to_last_updated_and_drops_untimestamped_records(self):
        payload = [
            [
                {"state": "on", "last_updated": "2026-09-01T17:00:00+00:00"},
                {"state": "off"},
                {"state": "on", "last_changed": 12345},
            ]
        ]
        assert _parse_history_payload(payload) == [(_utc(2026, 9, 1, 17, 0), True)]

    @pytest.mark.parametrize(
        "bad", [None, {}, "junk", [None], [{"state": "on"}], [[7]]]
    )
    def test_malformed_payloads_never_raise(self, bad):
        assert _parse_history_payload(bad) == []


# ── Learner ───────────────────────────────────────────────────────────────────


ENTITY = "binary_sensor.first_floor_occupancy_status"


def _learner(store, ha, redis, **kw) -> PriorLearner:
    kw.setdefault("entities", [ENTITY])
    kw.setdefault("slot_minutes", 30)
    kw.setdefault("max_catchup_slots", 48)
    kw.setdefault("max_lookback_days", 28)
    return PriorLearner(store, ha, redis, **kw)


class TestPriorLearner:
    @pytest.mark.asyncio
    async def test_cold_start_seeds_the_watermark_and_records_nothing(self):
        """Historical slots are the BACKFILL's job. Replaying them here would
        duplicate its work against a far smaller lookback."""
        store, ha, redis = FakePriorStore(), FakeHAAdapter(), FakeRedis()
        run = await _learner(store, ha, redis).close_out_due_slots(
            _utc(2026, 9, 1, 17, 42)
        )

        assert run.slots_closed == 0
        assert store.rows == {}
        assert ha.calls == []
        assert redis.data[WATERMARK_KEY] == _utc(2026, 9, 1, 17, 30).isoformat()

    @pytest.mark.asyncio
    async def test_closes_out_a_completed_slot_from_a_synthetic_timeline(self):
        """§9.1: 'slot close-out from a synthetic state timeline'. The 0.5 here is
        the hand-computed value from TestOccupiedFraction."""
        timeline = [
            (_utc(2026, 9, 1, 16, 50), True),
            (_utc(2026, 9, 1, 17, 10), False),
            (_utc(2026, 9, 1, 17, 25), True),
        ]
        store = FakePriorStore()
        ha = FakeHAAdapter({ENTITY: timeline})
        redis = FakeRedis({WATERMARK_KEY: _utc(2026, 9, 1, 17, 0).isoformat()})

        run = await _learner(store, ha, redis).close_out_due_slots(
            _utc(2026, 9, 1, 17, 35)
        )

        assert run.slots_closed == 1
        assert run.observations_written == 1
        rows = store.rows[(ENTITY, 1, 20)]  # Tuesday, 10:00 local
        assert len(rows) == 1
        assert rows[0].f == pytest.approx(0.5)
        assert rows[0].src == "native"
        assert redis.data[WATERMARK_KEY] == _utc(2026, 9, 1, 17, 30).isoformat()

    @pytest.mark.asyncio
    async def test_in_progress_slot_is_not_closed_out(self):
        store = FakePriorStore()
        ha = FakeHAAdapter({ENTITY: [(_utc(2026, 9, 1, 12, 0), True)]})
        redis = FakeRedis({WATERMARK_KEY: _utc(2026, 9, 1, 17, 0).isoformat()})

        run = await _learner(store, ha, redis).close_out_due_slots(
            _utc(2026, 9, 1, 17, 29)
        )

        assert run.slots_closed == 0
        assert store.rows == {}
        assert ha.calls == []  # no boundary crossed -> no I/O at all on most ticks

    @pytest.mark.asyncio
    async def test_multi_slot_catch_up_after_a_restart_is_one_history_call(self):
        """§9.1: 'multi-slot gap after a simulated restart'. A six-hour gap owes 12
        slots and must cost ONE history read per entity, not twelve."""
        timeline = [(_utc(2026, 9, 1, 10, 0), True)]
        store = FakePriorStore()
        ha = FakeHAAdapter({ENTITY: timeline})
        redis = FakeRedis({WATERMARK_KEY: _utc(2026, 9, 1, 12, 0).isoformat()})

        run = await _learner(store, ha, redis).close_out_due_slots(
            _utc(2026, 9, 1, 18, 0)
        )

        assert run.slots_closed == 12
        assert run.observations_written == 12
        assert len(ha.calls) == 1
        assert ha.calls[0][1] == _utc(2026, 9, 1, 12, 0)
        assert ha.calls[0][2] == _utc(2026, 9, 1, 18, 0)
        assert all(o[0].f == 1.0 for o in store.rows.values())

    @pytest.mark.asyncio
    async def test_catch_up_is_capped_and_resumes_from_where_it_stopped(self):
        store = FakePriorStore()
        ha = FakeHAAdapter({ENTITY: [(_utc(2026, 9, 1, 0, 0), True)]})
        redis = FakeRedis({WATERMARK_KEY: _utc(2026, 9, 1, 12, 0).isoformat()})
        learner = _learner(store, ha, redis, max_catchup_slots=4)

        run = await learner.close_out_due_slots(_utc(2026, 9, 1, 18, 0))
        assert run.slots_closed == 4
        assert redis.data[WATERMARK_KEY] == _utc(2026, 9, 1, 14, 0).isoformat()

        # Next tick picks up exactly where the cap stopped — no slot skipped.
        run2 = await learner.close_out_due_slots(_utc(2026, 9, 1, 18, 0))
        assert run2.slots_closed == 4
        assert redis.data[WATERMARK_KEY] == _utc(2026, 9, 1, 16, 0).isoformat()

    @pytest.mark.asyncio
    async def test_a_slot_with_no_history_writes_nothing(self):
        """EMPTY IS NOT ZERO, at the learner level. The slot is counted as no-data
        and skipped; a 0.0 here would claim the floor is always free."""
        store = FakePriorStore()
        ha = FakeHAAdapter({ENTITY: []})  # call succeeded, recorder had nothing
        redis = FakeRedis({WATERMARK_KEY: _utc(2026, 9, 1, 17, 0).isoformat()})

        run = await _learner(store, ha, redis).close_out_due_slots(
            _utc(2026, 9, 1, 17, 35)
        )

        assert run.slots_closed == 1
        assert run.slots_no_data == 1
        assert run.observations_written == 0
        assert store.rows == {}
        # The read SUCCEEDED, so the watermark still advances — otherwise a
        # genuinely empty window would wedge the learner retrying it forever.
        assert redis.data[WATERMARK_KEY] == _utc(2026, 9, 1, 17, 30).isoformat()

    @pytest.mark.asyncio
    async def test_watermark_does_not_advance_when_every_history_read_fails(self):
        """A total HA outage must leave the slots to be retried. Advancing through
        it would punch a permanent, silent hole in the table."""
        store = FakePriorStore()
        ha = FakeHAAdapter(fail={ENTITY})
        redis = FakeRedis({WATERMARK_KEY: _utc(2026, 9, 1, 17, 0).isoformat()})

        run = await _learner(store, ha, redis).close_out_due_slots(
            _utc(2026, 9, 1, 17, 35)
        )

        assert run.entities_failed == 1
        assert run.watermark_advanced_to is None
        assert redis.data[WATERMARK_KEY] == _utc(2026, 9, 1, 17, 0).isoformat()

    @pytest.mark.asyncio
    async def test_partial_entity_failure_still_advances_and_records_the_rest(self):
        other = "binary_sensor.kitchen_occupancy_status"
        store = FakePriorStore()
        ha = FakeHAAdapter({other: [(_utc(2026, 9, 1, 12, 0), True)]}, fail={ENTITY})
        redis = FakeRedis({WATERMARK_KEY: _utc(2026, 9, 1, 17, 0).isoformat()})

        run = await _learner(
            store, ha, redis, entities=[ENTITY, other]
        ).close_out_due_slots(_utc(2026, 9, 1, 17, 35))

        assert run.entities_failed == 1
        assert run.observations_written == 1
        assert (other, 1, 20) in store.rows
        assert (ENTITY, 1, 20) not in store.rows
        assert redis.data[WATERMARK_KEY] == _utc(2026, 9, 1, 17, 30).isoformat()

    @pytest.mark.asyncio
    async def test_stale_watermark_is_clamped_to_the_lookback_horizon(self):
        """An instance down for months must not spend every tick reading windows
        the recorder has long since purged."""
        store = FakePriorStore()
        ha = FakeHAAdapter({ENTITY: [(_utc(2026, 1, 1, 0, 0), True)]})
        redis = FakeRedis({WATERMARK_KEY: _utc(2026, 1, 1, 0, 0).isoformat()})
        learner = _learner(store, ha, redis, max_lookback_days=28, max_catchup_slots=4)

        await learner.close_out_due_slots(_utc(2026, 9, 1, 18, 0))

        assert ha.calls[0][1] >= _utc(2026, 8, 4, 0, 0)

    @pytest.mark.asyncio
    async def test_repeated_runs_over_the_same_slot_do_not_duplicate(self):
        """A restart that loses nothing but replays a slot must be a no-op — the
        dedupe on `at` is what guarantees it."""
        store = FakePriorStore()
        ha = FakeHAAdapter({ENTITY: [(_utc(2026, 9, 1, 12, 0), True)]})

        for _ in range(3):
            redis = FakeRedis({WATERMARK_KEY: _utc(2026, 9, 1, 17, 0).isoformat()})
            await _learner(store, ha, redis).close_out_due_slots(
                _utc(2026, 9, 1, 17, 35)
            )

        assert len(store.rows[(ENTITY, 1, 20)]) == 1

    @pytest.mark.asyncio
    async def test_unparseable_watermark_is_treated_as_a_cold_start(self):
        store, ha = FakePriorStore(), FakeHAAdapter()
        redis = FakeRedis({WATERMARK_KEY: "not-a-timestamp"})

        await _learner(store, ha, redis).close_out_due_slots(_utc(2026, 9, 1, 17, 42))

        assert redis.data[WATERMARK_KEY] == _utc(2026, 9, 1, 17, 30).isoformat()

    @pytest.mark.asyncio
    async def test_a_raising_history_adapter_is_survived(self):
        class Boom:
            async def get_state_history(self, *_a, **_k):
                raise RuntimeError("HA exploded")

        store = FakePriorStore()
        redis = FakeRedis({WATERMARK_KEY: _utc(2026, 9, 1, 17, 0).isoformat()})
        run = await _learner(store, Boom(), redis).close_out_due_slots(
            _utc(2026, 9, 1, 17, 35)
        )

        assert run.entities_failed == 1
        assert redis.data[WATERMARK_KEY] == _utc(2026, 9, 1, 17, 0).isoformat()

    @pytest.mark.asyncio
    async def test_a_raising_store_does_not_stop_the_other_entities(self):
        other = "binary_sensor.kitchen_occupancy_status"

        class HalfBrokenStore(FakePriorStore):
            async def record(self, entity_id, observations):
                if entity_id == ENTITY:
                    raise RuntimeError("db down")
                return await super().record(entity_id, observations)

        store = HalfBrokenStore()
        ha = FakeHAAdapter(
            {e: [(_utc(2026, 9, 1, 12, 0), True)] for e in (ENTITY, other)}
        )
        redis = FakeRedis({WATERMARK_KEY: _utc(2026, 9, 1, 17, 0).isoformat()})

        run = await _learner(
            store, ha, redis, entities=[ENTITY, other]
        ).close_out_due_slots(_utc(2026, 9, 1, 17, 35))

        assert run.observations_written == 1
        assert (other, 1, 20) in store.rows


# ── Backfill ──────────────────────────────────────────────────────────────────


def _always_on(since: datetime) -> list[tuple[datetime, bool]]:
    return [(since, True)]


class TestBackfill:
    @pytest.mark.asyncio
    async def test_seeds_slots_marked_as_backfill(self):
        now = _utc(2026, 9, 1, 17, 0)
        store = FakePriorStore()
        ha = FakeHAAdapter({ENTITY: _always_on(_utc(2026, 8, 20, 0, 0))})

        report = await backfill_priors(
            store, ha, entities=[ENTITY], now=now, lookback_days=7, chunk_days=7
        )

        assert report.ok
        assert report.slots_seeded == 7 * 48
        assert all(o.src == "backfill" for obs in store.rows.values() for o in obs)
        # 7 days of a weekly-recurring slot grid = exactly one observation per slot.
        assert len(store.rows) == 7 * 48

    @pytest.mark.asyncio
    async def test_running_twice_leaves_the_table_identical(self):
        """§9.1: 'backfill idempotency (run twice -> identical table)'."""
        now = _utc(2026, 9, 1, 17, 0)
        store = FakePriorStore()
        ha = FakeHAAdapter({ENTITY: _always_on(_utc(2026, 8, 20, 0, 0))})
        kwargs = dict(entities=[ENTITY], now=now, lookback_days=7, chunk_days=3)

        await backfill_priors(store, ha, **kwargs)
        first = store.snapshot()
        second_report = await backfill_priors(store, ha, **kwargs)

        assert store.snapshot() == first
        assert second_report.rows_written == 0  # nothing changed, so nothing written

    @pytest.mark.asyncio
    async def test_does_not_downgrade_native_samples_on_a_rerun(self):
        """The scenario that actually matters: the learner has been live for a
        while, then someone re-runs the backfill (a lost Redis flag is enough).
        Native samples and native_count must be untouched."""
        now = _utc(2026, 9, 1, 17, 0)
        store = FakePriorStore()
        native_at = _utc(2026, 8, 30, 17, 0)
        await store.record(ENTITY, [_obs(0.11, native_at, "native")])
        key = (ENTITY, slot_key(native_at, 30)[0], slot_key(native_at, 30)[1])

        ha = FakeHAAdapter({ENTITY: _always_on(_utc(2026, 8, 20, 0, 0))})
        await backfill_priors(store, ha, entities=[ENTITY], now=now, lookback_days=7)

        surviving = [o for o in store.rows[key] if o.at == native_at]
        assert len(surviving) == 1
        assert surviving[0].src == "native"
        assert surviving[0].f == pytest.approx(0.11)

    @pytest.mark.asyncio
    async def test_purged_windows_seed_nothing_rather_than_zero(self):
        """The real-world case as of 2026-09-04: the recorder keeps 20 days
        (configuration.yaml:13) but the default lookback asks for 28, so the
        oldest 8 days come back empty on EVERY run. Those slots must stay absent,
        not be seeded as permanently free."""
        now = _utc(2026, 9, 1, 17, 0)
        store = FakePriorStore()
        # Data only exists from 2026-08-29 onward — the earlier window is purged.
        ha = FakeHAAdapter({ENTITY: _always_on(_utc(2026, 8, 29, 0, 0))})

        report = await backfill_priors(
            store, ha, entities=[ENTITY], now=now, lookback_days=7, chunk_days=1
        )

        assert report.slots_no_data > 0
        assert report.earliest_data_at == _utc(2026, 8, 29, 0, 0)
        # Every seeded slot is at or after the first real datum; nothing before it.
        assert min(o.at for obs in store.rows.values() for o in obs) >= _utc(
            2026, 8, 29, 0, 0
        )
        assert all(o.f == 1.0 for obs in store.rows.values() for o in obs)

    @pytest.mark.asyncio
    async def test_total_history_failure_seeds_nothing_and_reports_not_ok(self):
        store = FakePriorStore()
        ha = FakeHAAdapter(fail={ENTITY})

        report = await backfill_priors(
            store, ha, entities=[ENTITY], now=_utc(2026, 9, 1, 17, 0), lookback_days=7
        )

        assert not report.ok
        assert report.entities_failed == 1
        assert store.rows == {}

    @pytest.mark.asyncio
    async def test_a_partial_chunk_failure_still_seeds_the_readable_chunks(self):
        """A failed chunk costs its own slots, not the whole entity."""
        now = _utc(2026, 9, 1, 17, 0)
        calls = {"n": 0}

        class FlakyHA(FakeHAAdapter):
            async def get_state_history(self, entity_id, start, end):
                calls["n"] += 1
                if calls["n"] == 1:  # first chunk (oldest) fails
                    return None
                return await super().get_state_history(entity_id, start, end)

        store = FakePriorStore()
        ha = FlakyHA({ENTITY: _always_on(_utc(2026, 8, 20, 0, 0))})

        report = await backfill_priors(
            store, ha, entities=[ENTITY], now=now, lookback_days=4, chunk_days=1
        )

        assert report.chunks_failed == 1
        assert report.entities_failed == 0
        assert report.ok
        assert report.slots_seeded > 0

    @pytest.mark.asyncio
    async def test_the_in_progress_slot_is_excluded(self):
        now = _utc(2026, 9, 1, 17, 20)  # mid-slot
        store = FakePriorStore()
        ha = FakeHAAdapter({ENTITY: _always_on(_utc(2026, 8, 20, 0, 0))})

        await backfill_priors(store, ha, entities=[ENTITY], now=now, lookback_days=2)

        latest = max(o.at for obs in store.rows.values() for o in obs)
        assert latest == _utc(2026, 9, 1, 16, 30)  # the last COMPLETED slot

    @pytest.mark.asyncio
    async def test_retention_bounds_a_long_lookback(self):
        """A 28-day seed offers 4 recurrences of each slot; retention keeps 8, so
        nothing is evicted — but assert the cap holds rather than assuming it."""
        now = _utc(2026, 9, 1, 17, 0)
        store = FakePriorStore(retention=8)
        ha = FakeHAAdapter({ENTITY: _always_on(_utc(2026, 6, 1, 0, 0))})

        await backfill_priors(store, ha, entities=[ENTITY], now=now, lookback_days=28)

        assert all(len(obs) <= 8 for obs in store.rows.values())
        assert max(len(obs) for obs in store.rows.values()) == 4

    @pytest.mark.asyncio
    async def test_multiple_entities_are_seeded_independently(self):
        now = _utc(2026, 9, 1, 17, 0)
        other = "binary_sensor.kitchen_occupancy_status"
        store = FakePriorStore()
        ha = FakeHAAdapter(
            {
                ENTITY: _always_on(_utc(2026, 8, 20, 0, 0)),
                other: [(_utc(2026, 8, 20, 0, 0), False)],
            }
        )

        report = await backfill_priors(
            store, ha, entities=[ENTITY, other], now=now, lookback_days=2
        )

        assert report.entities == 2
        assert report.per_entity[ENTITY] == report.per_entity[other] == 2 * 48
        assert all(
            o.f == 1.0
            for (e, _, _), obs in store.rows.items()
            if e == ENTITY
            for o in obs
        )
        assert all(
            o.f == 0.0
            for (e, _, _), obs in store.rows.items()
            if e == other
            for o in obs
        )


# ── Config / settings wiring ──────────────────────────────────────────────────

_REQUIRED_ENV = {
    "DATABASE_URL": "mysql+aiomysql://u:p@localhost:3306/cortex",
    "REDIS_URL": "redis://localhost:6379/0",
    "CORTEX_SECRET_KEY": "test-secret",
}


def _settings_with(monkeypatch, **overrides):
    from cortex_python.config.settings import Settings

    for key, value in {**_REQUIRED_ENV, **overrides}.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


class TestPriorLearnerSettingsWiring:
    """A kill switch that ships unwired is a KNOWN ARIIA finding in this module.

    Finding 1 on the original CORTEX_VACUUMOPS_MOP_ENABLED was exactly that: the
    dataclass field existed, the env var was documented, and nothing connected
    them. Tests that construct VacuumOpsConfig directly cannot catch it — they
    have to go through build_vacuumops_config(), which is what these do.
    """

    def test_env_var_can_disable_the_learner(self, monkeypatch):
        from cortex_python.modules.vacuumops.config import build_vacuumops_config

        settings = _settings_with(
            monkeypatch, CORTEX_VACUUMOPS_PRIOR_LEARNER_ENABLED="false"
        )
        assert build_vacuumops_config(settings).prior_learner_enabled is False

    def test_env_var_can_enable_the_learner(self, monkeypatch):
        from cortex_python.modules.vacuumops.config import build_vacuumops_config

        settings = _settings_with(
            monkeypatch, CORTEX_VACUUMOPS_PRIOR_LEARNER_ENABLED="true"
        )
        assert build_vacuumops_config(settings).prior_learner_enabled is True

    def test_defaults_on_when_unset(self, monkeypatch):
        """Unlike mop_enabled, the learner defaults ON: it writes rows nothing
        reads, so the cost of running it is a few HA calls per half hour, while
        the cost of NOT running it is wall-clock time that cannot be recovered."""
        from cortex_python.modules.vacuumops.config import build_vacuumops_config

        monkeypatch.delenv("CORTEX_VACUUMOPS_PRIOR_LEARNER_ENABLED", raising=False)
        settings = _settings_with(monkeypatch)
        assert build_vacuumops_config(settings).prior_learner_enabled is True


class TestLearnerConfigDefaults:
    def test_defaults_match_the_spec_section_7_table(self):
        from cortex_python.modules.vacuumops.config import VacuumOpsConfig

        cfg = VacuumOpsConfig()
        assert cfg.prior_learner_slot_minutes == 30
        assert cfg.prior_learner_retention_weeks == 8
        assert cfg.prior_learner_retention == 8
        assert cfg.opportunity_min_slot_samples == 3
        assert (
            cfg.prior_learner_entities[0]
            == "binary_sensor.first_floor_occupancy_status"
        )
        assert len(cfg.prior_learner_entities) == 5

    def test_slot_length_divides_the_day(self):
        from cortex_python.modules.vacuumops.config import VacuumOpsConfig

        assert slots_per_day(VacuumOpsConfig().prior_learner_slot_minutes) == 48

    def test_the_gate_signal_is_the_primary_entity(self):
        """R1 gates on binary_sensor.first_floor_occupancy_status (homeOps#201).
        The learner must learn THAT entity, not a reconstruction of the floor
        from its member areas — that equivalence is the whole simplification D1
        bought, and a drift here would silently reintroduce the OR-vs-MEAN
        calibration gap §4.2 removed."""
        from cortex_python.modules.vacuumops.config import VacuumOpsConfig
        from cortex_python.synth.vacuumops_synth import _FLOOR_OCCUPANCY_ENTITY

        assert (
            VacuumOpsConfig().prior_learner_entities[0] == _FLOOR_OCCUPANCY_ENTITY["1F"]
        )


# ── Adapter: get_state_history ────────────────────────────────────────────────


def _adapter(handler) -> HARestAdapter:
    """HARestAdapter wired to a MockTransport, bypassing only the transport."""

    class _Settings:
        homeassistant_url = "http://ha.test:8123"
        homeassistant_token = "tok"

    adapter = HARestAdapter(_Settings())  # type: ignore[arg-type]
    adapter._client = lambda: httpx.AsyncClient(  # type: ignore[method-assign]
        base_url="http://ha.test:8123", transport=httpx.MockTransport(handler)
    )
    return adapter


class TestGetStateHistory:
    """The None-vs-empty contract is what the learner's watermark branches on, so
    it is asserted here rather than left to the learner's fakes."""

    @pytest.mark.asyncio
    async def test_parses_a_real_history_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    [
                        {"state": "on", "last_changed": "2026-09-01T17:00:00+00:00"},
                        {"state": "off", "last_changed": "2026-09-01T17:10:00+00:00"},
                    ]
                ],
            )

        got = await _adapter(handler).get_state_history(
            ENTITY, _utc(2026, 9, 1, 17, 0), _utc(2026, 9, 1, 17, 30)
        )
        assert got == [
            (_utc(2026, 9, 1, 17, 0), True),
            (_utc(2026, 9, 1, 17, 10), False),
        ]

    @pytest.mark.asyncio
    async def test_request_shape(self):
        """`minimal_response` / `no_attributes` must go out as bare valueless
        flags — HA tests for parameter PRESENCE. `significant_changes_only` must
        be ABSENT: omitting it is what returns every state change, and an exact
        timeline is the entire premise of the learner."""
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json=[[]])

        await _adapter(handler).get_state_history(
            ENTITY, _utc(2026, 9, 1, 17, 0), _utc(2026, 9, 1, 17, 30)
        )

        assert "/api/history/period/2026-09-01T17:00:00+00:00" in seen["url"]
        assert seen["params"]["filter_entity_id"] == ENTITY
        assert seen["params"]["end_time"] == "2026-09-01T17:30:00+00:00"
        assert seen["params"]["minimal_response"] == ""
        assert seen["params"]["no_attributes"] == ""
        assert "significant_changes_only" not in seen["params"]

    @pytest.mark.asyncio
    async def test_naive_datetimes_are_sent_as_utc(self):
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json=[[]])

        await _adapter(handler).get_state_history(
            ENTITY, datetime(2026, 9, 1, 17, 0), datetime(2026, 9, 1, 17, 30)
        )
        assert seen["params"]["end_time"] == "2026-09-01T17:30:00+00:00"

    @pytest.mark.asyncio
    async def test_empty_recorder_window_returns_empty_list_not_none(self):
        """A purged window is a SUCCESSFUL read with no data. The learner advances
        its watermark past it; returning None here would wedge it retrying a
        window that can never come back."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[[]])

        got = await _adapter(handler).get_state_history(
            ENTITY, _utc(2026, 9, 1, 17, 0), _utc(2026, 9, 1, 17, 30)
        )
        assert got == []
        assert got is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 404, 500, 503])
    async def test_error_statuses_return_none_not_empty(self, status):
        """A failed read must NOT be mistaken for an empty one — that is what
        stops an HA outage from silently punching a hole in the table."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"message": "nope"})

        got = await _adapter(handler).get_state_history(
            ENTITY, _utc(2026, 9, 1, 17, 0), _utc(2026, 9, 1, 17, 30)
        )
        assert got is None

    @pytest.mark.asyncio
    async def test_transport_error_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        got = await _adapter(handler).get_state_history(
            ENTITY, _utc(2026, 9, 1, 17, 0), _utc(2026, 9, 1, 17, 30)
        )
        assert got is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        got = await _adapter(handler).get_state_history(
            ENTITY, _utc(2026, 9, 1, 17, 0), _utc(2026, 9, 1, 17, 30)
        )
        assert got is None

    @pytest.mark.asyncio
    async def test_malformed_json_body_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        got = await _adapter(handler).get_state_history(
            ENTITY, _utc(2026, 9, 1, 17, 0), _utc(2026, 9, 1, 17, 30)
        )
        assert got is None


# ── PriorStore against a real database ────────────────────────────────────────
#
# The fake above proves the ORCHESTRATION. These prove the SQL. PriorStore's
# statements are hand-written text(), which is exactly the kind of code a fake
# can never catch a typo in — and it is the write path for a table created by a
# migration, so it is the part of this PR with the least margin for a silent
# error. SQLite via aiosqlite (a dev-only dependency); production is MariaDB.


@pytest_asyncio.fixture
async def sqlite_store(tmp_path):
    """A real PriorStore over a real, migrated-shape SQLite database."""
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'priors.db'}")
    # Mirrors the migration's DDL in SQLite terms. Kept minimal and explicit:
    # the migration itself is verified separately by running the real alembic
    # chain; what matters here is that PriorStore's SQL works against a table of
    # this shape with this unique constraint.
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "CREATE TABLE cortex_occupancy_priors ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " entity_id VARCHAR(128) NOT NULL,"
                " day_of_week SMALLINT NOT NULL,"
                " slot SMALLINT NOT NULL,"
                " observations JSON NOT NULL,"
                " native_count SMALLINT NOT NULL DEFAULT 0,"
                " mean_occupied NUMERIC(5,4) NOT NULL,"
                " stddev_occupied NUMERIC(5,4) NULL,"
                " last_sample_at DATETIME NOT NULL,"
                " created_at DATETIME NOT NULL,"
                " updated_at DATETIME NOT NULL,"
                " CONSTRAINT uq_entity_slot UNIQUE (entity_id, day_of_week, slot))"
            )
        )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    yield PriorStore(sessions, slot_minutes=30, retention=8, min_slot_samples=3), engine
    await engine.dispose()


# sqlite3's default datetime adapter is deprecated in Python 3.12+. It fires only
# here, because only this SQLite-backed fixture round-trips a datetime parameter
# through the stdlib driver; production binds through aiomysql, which is
# unaffected. Filtered so the ~350 repeats do not bury a real warning.
@pytest.mark.filterwarnings("ignore:The default datetime adapter is deprecated")
class TestPriorStoreAgainstARealDatabase:
    @pytest.mark.asyncio
    async def test_insert_then_read_back(self, sqlite_store):
        store, _ = sqlite_store
        at = _utc(2026, 9, 1, 17, 0)  # Tuesday 10:00 local -> (1, 20)

        assert await store.record(ENTITY, [_obs(0.5, at)]) == 1

        prior = await store.read_slot(ENTITY, 1, 20)
        assert prior.mean_occupied == pytest.approx(0.5)
        assert prior.native_count == 1
        assert prior.sample_count == 1
        assert prior.confidence == "thin"
        assert prior.stddev_occupied is None
        assert prior.last_sample_at == at

    @pytest.mark.asyncio
    async def test_update_path_merges_rather_than_duplicating_the_row(
        self, sqlite_store
    ):
        """Second write to the same slot must UPDATE. If it INSERTed, the unique
        constraint would raise — so this also proves the constraint is the thing
        keeping the table one-row-per-slot."""
        store, engine = sqlite_store
        import sqlalchemy as sa

        base = _utc(2026, 9, 1, 17, 0)
        await store.record(ENTITY, [_obs(0.2, base)])
        await store.record(ENTITY, [_obs(0.8, base + timedelta(days=7))])

        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.text("SELECT COUNT(*) FROM cortex_occupancy_priors")
                )
            ).scalar()
        assert rows == 1

        prior = await store.read_slot(ENTITY, 1, 20)
        assert prior.sample_count == 2
        assert prior.mean_occupied == pytest.approx(0.5)
        assert prior.stddev_occupied == pytest.approx(0.42426, abs=1e-4)

    @pytest.mark.asyncio
    async def test_denormalised_scalars_match_the_observations_array(
        self, sqlite_store
    ):
        """mean/stddev/native_count are derived columns. If the writer and the
        array ever disagree, A2 reads a lie from an indexed column and never
        notices — so pin them against a recomputation from the JSON."""
        store, engine = sqlite_store
        import json as _json

        import sqlalchemy as sa

        base = _utc(2026, 9, 1, 17, 0)
        await store.record(
            ENTITY,
            [
                _obs(0.2, base, "backfill"),
                _obs(0.4, base + timedelta(days=7), "native"),
                _obs(0.9, base + timedelta(days=14), "native"),
            ],
        )

        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.text(
                            "SELECT observations, native_count, mean_occupied, stddev_occupied,"
                            " last_sample_at FROM cortex_occupancy_priors"
                        )
                    )
                )
                .mappings()
                .first()
            )

        stored = _json.loads(row["observations"])
        assert [o["src"] for o in stored] == ["backfill", "native", "native"]
        assert row["native_count"] == 2  # backfill excluded
        assert float(row["mean_occupied"]) == pytest.approx(0.5, abs=1e-4)
        assert float(row["stddev_occupied"]) == pytest.approx(0.36056, abs=1e-4)
        # Newest last, and last_sample_at tracks it.
        assert stored[-1]["at"] == (base + timedelta(days=14)).isoformat()

    @pytest.mark.asyncio
    async def test_absent_row_reads_as_unavailable(self, sqlite_store):
        store, _ = sqlite_store
        prior = await store.read_slot(ENTITY, 3, 47)
        assert prior.confidence == "unavailable"
        assert prior.sample_count == 0

    @pytest.mark.asyncio
    async def test_repeat_write_of_identical_data_is_a_no_op(self, sqlite_store):
        """The idempotency short-circuit: an unchanged merge must not issue a
        write at all. This is what makes a backfill re-run cheap as well as safe."""
        store, _ = sqlite_store
        at = _utc(2026, 9, 1, 17, 0)

        assert await store.record(ENTITY, [_obs(0.5, at)]) == 1
        assert await store.record(ENTITY, [_obs(0.5, at)]) == 0

    @pytest.mark.asyncio
    async def test_a_mixed_batch_spanning_many_slots_writes_one_row_each(
        self, sqlite_store
    ):
        """The backfill passes thousands of observations in a single record()
        call; they must be grouped by their own slot, not the first one's."""
        store, engine = sqlite_store
        import sqlalchemy as sa

        base = _utc(2026, 9, 1, 17, 0)
        batch = [
            _obs(0.1 * i, base + timedelta(minutes=30 * i), "backfill")
            for i in range(6)
        ]
        written = await store.record(ENTITY, batch)
        assert written == 6

        async with engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.text(
                            "SELECT slot FROM cortex_occupancy_priors ORDER BY slot"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert rows == [20, 21, 22, 23, 24, 25]

    @pytest.mark.asyncio
    async def test_retention_is_enforced_through_the_real_write_path(
        self, sqlite_store
    ):
        store, _ = sqlite_store
        base = _utc(2026, 9, 1, 17, 0)
        for i in range(12):
            await store.record(ENTITY, [_obs(i / 20, base + timedelta(days=7 * i))])

        prior = await store.read_slot(ENTITY, 1, 20)
        assert (
            prior.sample_count == 8
        )  # retention cap holds in the DB, not just in memory

    @pytest.mark.asyncio
    async def test_native_survives_a_backfill_rerun_through_the_real_write_path(
        self, sqlite_store
    ):
        """The precedence rule, end to end. Merge-level coverage exists above;
        this proves it survives the round trip through JSON and back."""
        store, _ = sqlite_store
        at = _utc(2026, 9, 1, 17, 0)

        await store.record(ENTITY, [_obs(0.33, at, "native")])
        await store.record(ENTITY, [_obs(0.99, at, "backfill")])

        prior = await store.read_slot(ENTITY, 1, 20)
        assert prior.native_count == 1
        assert prior.mean_occupied == pytest.approx(0.33)

    @pytest.mark.asyncio
    async def test_empty_observation_batch_writes_nothing(self, sqlite_store):
        store, _ = sqlite_store
        assert await store.record(ENTITY, []) == 0

    @pytest.mark.asyncio
    async def test_learner_end_to_end_against_the_real_store(self, sqlite_store):
        """The full A1 path with no fake store in it: HA history -> fraction ->
        merge -> SQL -> read back."""
        store, _ = sqlite_store
        ha = FakeHAAdapter(
            {
                ENTITY: [
                    (_utc(2026, 9, 1, 16, 50), True),
                    (_utc(2026, 9, 1, 17, 10), False),
                    (_utc(2026, 9, 1, 17, 25), True),
                ]
            }
        )
        redis = FakeRedis({WATERMARK_KEY: _utc(2026, 9, 1, 17, 0).isoformat()})

        run = await _learner(store, ha, redis).close_out_due_slots(
            _utc(2026, 9, 1, 17, 35)
        )
        assert run.observations_written == 1

        prior = await store.read_slot(ENTITY, 1, 20)
        assert prior.mean_occupied == pytest.approx(0.5)
        assert prior.native_count == 1

    @pytest.mark.asyncio
    async def test_backfill_end_to_end_against_the_real_store_is_idempotent(
        self, sqlite_store
    ):
        store, engine = sqlite_store
        import sqlalchemy as sa

        now = _utc(2026, 9, 1, 17, 0)
        ha = FakeHAAdapter({ENTITY: _always_on(_utc(2026, 8, 20, 0, 0))})
        kwargs = dict(entities=[ENTITY], now=now, lookback_days=2, chunk_days=1)

        await backfill_priors(store, ha, **kwargs)

        async def dump():
            async with engine.connect() as conn:
                return (
                    await conn.execute(
                        sa.text(
                            "SELECT entity_id, day_of_week, slot, observations, native_count,"
                            " mean_occupied FROM cortex_occupancy_priors"
                            " ORDER BY day_of_week, slot"
                        )
                    )
                ).all()

        first = await dump()
        assert len(first) == 2 * 48

        report2 = await backfill_priors(store, ha, **kwargs)
        assert report2.rows_written == 0
        assert await dump() == first  # byte-identical, per §9.1


# ── Loop wiring ───────────────────────────────────────────────────────────────


class TestLoopBackfillGuard:
    """_maybe_run_prior_backfill's own logic — chiefly the not-ok branch.

    The interesting case is an HA that is unreachable at boot: setting the
    done-flag then would leave the table permanently cold with no retry and no
    signal, which is precisely the "logged once, never re-checked" shape Dream
    Pass v5 #1 is about.
    """

    @pytest.mark.asyncio
    async def test_sets_the_done_flag_after_a_successful_seed(self):
        from cortex_python.modules.vacuumops.config import VacuumOpsConfig
        from cortex_python.modules.vacuumops.loop import _maybe_run_prior_backfill
        from cortex_python.modules.vacuumops.priors import BACKFILL_DONE_KEY

        store = FakePriorStore()
        ha = FakeHAAdapter({ENTITY: _always_on(_utc(2026, 8, 20, 0, 0))})
        redis = FakeRedis()
        cfg = VacuumOpsConfig(
            prior_learner_entities=(ENTITY,), prior_learner_backfill_days=2
        )

        await _maybe_run_prior_backfill(store, ha, redis, cfg, _utc(2026, 9, 1, 17, 0))

        assert BACKFILL_DONE_KEY in redis.data
        assert store.rows

    @pytest.mark.asyncio
    async def test_does_not_set_the_flag_when_nothing_was_seeded(self):
        """An HA outage at boot must leave the backfill retryable on next start."""
        from cortex_python.modules.vacuumops.config import VacuumOpsConfig
        from cortex_python.modules.vacuumops.loop import _maybe_run_prior_backfill
        from cortex_python.modules.vacuumops.priors import BACKFILL_DONE_KEY

        store = FakePriorStore()
        ha = FakeHAAdapter(fail={ENTITY})
        redis = FakeRedis()
        cfg = VacuumOpsConfig(
            prior_learner_entities=(ENTITY,), prior_learner_backfill_days=2
        )

        await _maybe_run_prior_backfill(store, ha, redis, cfg, _utc(2026, 9, 1, 17, 0))

        assert BACKFILL_DONE_KEY not in redis.data
        assert store.rows == {}

    @pytest.mark.asyncio
    async def test_skips_entirely_when_the_flag_is_already_set(self):
        from cortex_python.modules.vacuumops.config import VacuumOpsConfig
        from cortex_python.modules.vacuumops.loop import _maybe_run_prior_backfill
        from cortex_python.modules.vacuumops.priors import BACKFILL_DONE_KEY

        store = FakePriorStore()
        ha = FakeHAAdapter({ENTITY: _always_on(_utc(2026, 8, 20, 0, 0))})
        redis = FakeRedis({BACKFILL_DONE_KEY: "2026-08-01T00:00:00+00:00"})
        cfg = VacuumOpsConfig(prior_learner_entities=(ENTITY,))

        await _maybe_run_prior_backfill(store, ha, redis, cfg, _utc(2026, 9, 1, 17, 0))

        assert ha.calls == []
        assert store.rows == {}

    @pytest.mark.asyncio
    async def test_a_raising_backfill_never_propagates_into_the_loop(self):
        from cortex_python.modules.vacuumops.config import VacuumOpsConfig
        from cortex_python.modules.vacuumops.loop import _maybe_run_prior_backfill

        class Boom:
            async def get_state_history(self, *_a, **_k):
                raise RuntimeError("HA exploded")

        class BoomStore(FakePriorStore):
            async def record(self, entity_id, observations):
                raise RuntimeError("db down")

        cfg = VacuumOpsConfig(
            prior_learner_entities=(ENTITY,), prior_learner_backfill_days=2
        )
        # Must not raise — a failed backfill degrades to a cold learner, never to
        # a loop that will not start.
        await _maybe_run_prior_backfill(
            BoomStore(), Boom(), FakeRedis(), cfg, _utc(2026, 9, 1, 17, 0)
        )
