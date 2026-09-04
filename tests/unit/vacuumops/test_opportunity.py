"""Tests for the predictive-patience decision core (PR A2).

Three properties carry disproportionate weight and each has its own class:

  * `TestMissingPriorDataIsNotClear` — "empty is not zero". A slot with no
    observations must never read as "always clear". This is the single most
    safety-critical property in the PR and the standard ARIIA held A1 to.
  * `TestActiveDurationOnly` — the fit check sizes against
    `*_active_duration_min`, NEVER `*_duration_min`. Using the wall-clock field
    silently over-reserves by ~70% and turns every fit check into a deferral.
  * `TestPatienceIsATwoBandStep` — patience is a step with an absolute cap, not
    a ramp. A ramp would imply a precision the input does not have.

Fit arithmetic is asserted to the decimal place against hand-computed values
(spec §9.1: "assert the numbers, not just no exception").
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from cortex_python.modules.vacuumops.config import VacuumOpsConfig
from cortex_python.modules.vacuumops.opportunity import (
    _ACTIVE_MEAN_FIELD,
    _ACTIVE_PERCENTILE_FIELDS,
    _WALL_CLOCK_FIELDS,
    BASIS_MEAN_FALLBACK,
    BASIS_UNAVAILABLE,
    IMPATIENT,
    PATIENT,
    DurationEstimate,
    duration_estimate,
    format_opportunity,
    forward_slot_keys,
    lookahead_slot_count,
    opportunity,
    over_threshold_since_key,
    patience,
    patience_band,
    required_slot_count,
)
from cortex_python.modules.vacuumops.priors import (
    CONFIDENCE_GOOD,
    CONFIDENCE_THIN,
    CONFIDENCE_UNAVAILABLE,
    HOUSEHOLD_TZ,
    SlotPrior,
    slot_key,
)

CFG = VacuumOpsConfig()


# 2026-09-08 is a Tuesday (day_of_week 1). Times are built in HOUSEHOLD-LOCAL and
# converted to UTC, because the whole premise of the prior table is a wall-clock
# statement ("Tuesday around noon is usually busy") — a UTC-keyed fixture would
# pass while smearing every weekday pattern by an hour twice a year.
def local(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=HOUSEHOLD_TZ).astimezone(UTC)


NOW = local(2026, 9, 8, 12, 0)  # Tuesday 12:00 PST, exactly on a slot boundary


def prior(
    occupied: float,
    *,
    offset: int = 0,
    native: int = 5,
    samples: int | None = None,
    confidence: str = CONFIDENCE_GOOD,
) -> SlotPrior:
    """A learned slot with a real observation behind it."""
    dow, slot = slot_key(NOW + timedelta(minutes=30 * offset))
    return SlotPrior(
        entity_id="binary_sensor.first_floor_occupancy_status",
        day_of_week=dow,
        slot=slot,
        mean_occupied=occupied,
        stddev_occupied=0.1,
        native_count=native,
        sample_count=native if samples is None else samples,
        confidence=confidence,
    )


def ladder(*occupied: float) -> list[SlotPrior]:
    """Contiguous forward slots, `occupied[i]` for the slot i ahead of now."""
    return [prior(value, offset=i) for i, value in enumerate(occupied)]


def stats(**kwargs: float) -> dict[str, float]:
    return dict(kwargs)


P75_35 = DurationEstimate(minutes=35.0, basis="p75_active+return_leg", source="robot")


# ── The safety property ──────────────────────────────────────────────────────


class TestMissingPriorDataIsNotClear:
    """Missing prior data must NEVER be read as "this slot is clear".

    A1's `SlotPrior.unavailable()` carries `mean_occupied = 0.0` because the
    field needs a number. `1 - 0.0 = 1.0` would say "always clear, dispatch now",
    which turns a recorder outage into the strongest possible argument FOR
    running the vacuum. Every assertion below is a different route to that same
    inversion.
    """

    def test_all_slots_unavailable_never_reports_clear(self) -> None:
        slots = [
            SlotPrior.unavailable("binary_sensor.first_floor_occupancy_status", 0, i)
            for i in range(12)
        ]
        read = opportunity(now=NOW, slots=slots, duration=P75_35, cfg=CFG)

        assert read.confidence == CONFIDENCE_UNAVAILABLE
        # The inversion, asserted directly: not 1.0, not "fits perfectly".
        assert read.p_clear_now == 0.0
        assert read.expected_fit_now == 0.0
        assert read.best_slot_offset is None
        assert read.best_slot_gain == 0.0
        assert read.degraded_reason is not None
        assert "prior_slot_unavailable" in read.degraded_reason

    def test_one_missing_slot_anywhere_collapses_the_whole_read(self) -> None:
        """The consulted set is every slot a candidate mission touches, not just now's.

        Offset 8 is 4 hours out — nowhere near the mission this decision would
        dispatch — but it is inside the tail of the LAST candidate window, so it
        must still be able to veto the read. A "check the current slot only"
        implementation passes every other test in this class and fails this one.
        """
        slots = ladder(*[0.05 * i for i in range(12)])
        slots[8] = SlotPrior.unavailable(
            "binary_sensor.first_floor_occupancy_status", 0, 8
        )

        read = opportunity(now=NOW, slots=slots, duration=P75_35, cfg=CFG)

        assert read.confidence == CONFIDENCE_UNAVAILABLE
        assert read.degraded_reason is not None
        assert "prior_slot_unavailable" in read.degraded_reason
        assert "offset=8" in read.degraded_reason
        # The current slot was perfectly good; the read still refuses to score.
        assert read.expected_fit_now == 0.0

    def test_a_none_entry_is_treated_exactly_like_an_unavailable_slot(self) -> None:
        slots: list[SlotPrior | None] = list(ladder(*[0.2] * 12))
        slots[3] = None
        read = opportunity(now=NOW, slots=slots, duration=P75_35, cfg=CFG)
        assert read.confidence == CONFIDENCE_UNAVAILABLE
        assert read.expected_fit_now == 0.0

    def test_zero_sample_count_is_unavailable_even_if_labelled_good(self) -> None:
        """A mislabelled row must not smuggle a phantom "clear" through.

        `sample_count == 0` is checked independently of the `confidence` string
        so a store bug (or a future caching layer) cannot promote an empty slot
        by writing the wrong label onto it.
        """
        slots = ladder(*[0.3] * 12)
        slots[0] = prior(0.0, offset=0, native=9, samples=0, confidence=CONFIDENCE_GOOD)

        read = opportunity(now=NOW, slots=slots, duration=P75_35, cfg=CFG)
        assert read.confidence == CONFIDENCE_UNAVAILABLE
        assert read.p_clear_now == 0.0

    def test_all_zero_table_is_rejected_not_treated_as_a_perfect_window(self) -> None:
        """The A1-regression tripwire: an "empty written as 0.0" table.

        If A1 ever regressed and wrote 0.0 for unobserved windows, those rows
        would arrive here looking perfectly well-formed and would score a
        flawless fit. The degenerate-uniform rule catches that shape.
        """
        read = opportunity(now=NOW, slots=ladder(*[0.0] * 12), duration=P75_35, cfg=CFG)

        assert read.confidence == CONFIDENCE_UNAVAILABLE
        assert read.expected_fit_now == 0.0
        assert read.degraded_reason is not None
        assert "degenerate_uniform" in read.degraded_reason

    def test_too_few_slots_supplied_degrades_rather_than_scoring_a_short_table(
        self,
    ) -> None:
        read = opportunity(now=NOW, slots=ladder(0.2, 0.3), duration=P75_35, cfg=CFG)
        assert read.confidence == CONFIDENCE_UNAVAILABLE
        assert read.degraded_reason is not None
        assert "prior_slots_missing" in read.degraded_reason

    def test_cold_learner_is_unavailable_regardless_of_slot_quality(self) -> None:
        read = opportunity(
            now=NOW,
            slots=ladder(*[0.1 * i for i in range(12)]),
            duration=P75_35,
            cfg=CFG,
            learner_native_days=3.0,
        )
        assert read.confidence == CONFIDENCE_UNAVAILABLE
        assert read.degraded_reason is not None
        assert "learner_cold" in read.degraded_reason

    def test_degraded_context_is_unavailable(self) -> None:
        read = opportunity(
            now=NOW,
            slots=ladder(*[0.1 * i for i in range(12)]),
            duration=P75_35,
            cfg=CFG,
            context_degraded=True,
        )
        assert read.confidence == CONFIDENCE_UNAVAILABLE
        assert read.degraded_reason == "ctx_degraded"

    def test_every_degraded_path_names_itself(self) -> None:
        """Invariant 5: no silent no-op. 2026-08-31's root cause was exactly that."""
        cases = [
            dict(slots=[], duration=P75_35),
            dict(
                slots=ladder(*[0.2] * 12),
                duration=DurationEstimate.unavailable("no_stats"),
            ),
            dict(slots=ladder(*[0.2] * 12), duration=P75_35, context_degraded=True),
            dict(slots=ladder(*[0.2] * 12), duration=P75_35, learner_native_days=1.0),
            dict(slots=ladder(*[0.5] * 12), duration=P75_35),
        ]
        for kwargs in cases:
            read = opportunity(now=NOW, cfg=CFG, **kwargs)  # type: ignore[arg-type]
            assert read.confidence == CONFIDENCE_UNAVAILABLE
            assert read.degraded_reason, f"silent degradation for {kwargs}"

    def test_thin_slots_are_usable_but_can_never_justify_a_defer(self) -> None:
        """A3 requires confidence=="good" to DEFER; thin can only escalate to L1."""
        slots = ladder(*[0.9, 0.9, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1])
        slots[4] = prior(0.1, offset=4, native=1)  # below opportunity_min_slot_samples

        read = opportunity(now=NOW, slots=slots, duration=P75_35, cfg=CFG)

        assert read.confidence == CONFIDENCE_THIN
        assert read.degraded_reason is not None
        assert "native=1<3" in read.degraded_reason
        # Still a usable read — thin is degraded, not void.
        assert read.expected_fit_now > 0.0

    def test_unavailable_read_pins_numbers_pessimistic_not_neutral(self) -> None:
        """Invariant 3: a caller who forgets to check confidence must land safely.

        `expected_fit_now = 0.0` routes A3 to `fit_marginal` -> AMBIGUOUS -> L1
        (human/LLM judgment). A neutral 0.5 or an optimistic 1.0 would route to
        `fit_ok` -> PASS -> dispatch on an absence of evidence.
        """
        read = opportunity(now=NOW, slots=[], duration=P75_35, cfg=CFG)
        assert read.expected_fit_now <= CFG.opportunity_marginal_fit
        assert read.p_clear_curve == ()


# ── Duration: active only ────────────────────────────────────────────────────


class TestActiveDurationOnly:
    """`*_active_duration_min`, never `*_duration_min` (A0's own load-bearing note).

    `avg_duration_min` (Saros: 44.8) measures dispatch -> mission-log close-out
    including the return leg and a double dock-bounce; `avg_active_duration_min`
    (26.3) matches the robot's own cleaning window. Sizing on the wall clock
    over-reserves by ~70%.
    """

    def test_the_two_field_sets_never_intersect(self) -> None:
        """Structural, not advisory: the whitelist and the ban list are disjoint."""
        readable = set(_ACTIVE_PERCENTILE_FIELDS.values()) | {_ACTIVE_MEAN_FIELD}
        assert readable.isdisjoint(_WALL_CLOCK_FIELDS)
        assert all(name.endswith("_active_duration_min") for name in readable)

    def test_wall_clock_fields_are_ignored_even_when_they_are_the_only_data(
        self,
    ) -> None:
        """A pre-A0 stats payload yields NOTHING, rather than silently using 44.8."""
        est = duration_estimate(
            cfg=CFG,
            robot_stats=stats(
                avg_duration_min=44.8, p75_duration_min=58.0, p90_duration_min=71.0
            ),
            zone_count=6,
        )
        assert est.minutes is None
        assert est.basis == BASIS_UNAVAILABLE

    def test_active_percentile_is_preferred_over_every_wall_clock_field(self) -> None:
        est = duration_estimate(
            cfg=CFG,
            robot_stats=stats(
                avg_duration_min=44.8,
                p75_duration_min=58.0,
                p90_duration_min=71.0,
                avg_active_duration_min=26.3,
                p75_active_duration_min=30.0,
                p90_active_duration_min=38.0,
            ),
            zone_count=6,
        )
        # 30.0 (p75 ACTIVE) + 5.0 return leg. Not 58.0, and not 63.0.
        assert est.minutes == pytest.approx(35.0)
        assert est.basis == "p75_active+return_leg"
        assert est.forces_thin is False

    def test_p90_percentile_selects_the_more_conservative_active_field(self) -> None:
        cfg = VacuumOpsConfig(opportunity_duration_percentile="p90")
        est = duration_estimate(
            cfg=cfg,
            robot_stats=stats(
                p75_active_duration_min=30.0, p90_active_duration_min=38.0
            ),
            zone_count=6,
        )
        assert est.minutes == pytest.approx(43.0)
        assert est.basis == "p90_active+return_leg"

    def test_mean_fallback_uses_mean_ACTIVE_and_forces_thin(self) -> None:
        est = duration_estimate(
            cfg=CFG,
            robot_stats=stats(avg_duration_min=44.8, avg_active_duration_min=26.3),
            zone_count=6,
        )
        assert est.minutes == pytest.approx(26.3 * 1.4 + 5.0)
        assert est.basis == BASIS_MEAN_FALLBACK
        assert est.forces_thin is True

    def test_mean_fallback_forces_a_thin_read_end_to_end(self) -> None:
        est = duration_estimate(
            cfg=CFG, robot_stats=stats(avg_active_duration_min=20.0), zone_count=6
        )
        read = opportunity(
            now=NOW, slots=ladder(*[0.05 * i for i in range(12)]), duration=est, cfg=CFG
        )
        assert read.confidence == CONFIDENCE_THIN
        assert read.duration_basis == BASIS_MEAN_FALLBACK

    def test_zero_duration_is_rejected_not_treated_as_an_instant_mission(self) -> None:
        """Sam and Ethan report `avg_duration_min: 0`. Zero would fit any window."""
        est = duration_estimate(
            cfg=CFG,
            robot_stats=stats(
                avg_duration_min=0.0,
                avg_active_duration_min=0.0,
                p75_active_duration_min=0.0,
            ),
            zone_count=3,
        )
        assert est.minutes is None
        assert est.basis == BASIS_UNAVAILABLE

    @pytest.mark.parametrize("bad", [None, "", "n/a", float("nan"), -3.0, True])
    def test_non_numeric_and_nonsense_values_are_rejected(self, bad: object) -> None:
        est = duration_estimate(
            cfg=CFG, robot_stats={"p75_active_duration_min": bad}, zone_count=1
        )
        assert est.minutes is None

    def test_single_zone_mission_prefers_zone_stats(self) -> None:
        est = duration_estimate(
            cfg=CFG,
            zone_stats=stats(p75_active_duration_min=28.0),
            robot_stats=stats(p75_active_duration_min=40.0),
            zone_count=1,
        )
        assert est.minutes == pytest.approx(33.0)
        assert est.source == "zone"

    def test_batch_prefers_robot_wide_stats(self) -> None:
        """Summing per-zone means over-reserves: a batch shares one transit."""
        est = duration_estimate(
            cfg=CFG,
            zone_stats=stats(p75_active_duration_min=28.0),
            robot_stats=stats(p75_active_duration_min=40.0),
            zone_count=6,
        )
        assert est.minutes == pytest.approx(45.0)
        assert est.source == "robot"

    def test_falls_back_to_the_other_scope_and_records_the_substitution(self) -> None:
        est = duration_estimate(
            cfg=CFG,
            zone_stats=stats(avg_duration_min=22.5),  # wall clock only — unusable
            robot_stats=stats(p75_active_duration_min=30.0),
            zone_count=1,
        )
        assert est.minutes == pytest.approx(35.0)
        assert est.source == "robot"

    def test_no_stats_at_all_degrades_the_read(self) -> None:
        est = duration_estimate(cfg=CFG, zone_count=1)
        read = opportunity(now=NOW, slots=ladder(*[0.2] * 12), duration=est, cfg=CFG)
        assert read.confidence == CONFIDENCE_UNAVAILABLE
        assert read.degraded_reason is not None
        assert "no_active_duration" in read.degraded_reason


# ── patience(): two-band step + absolute cap ─────────────────────────────────


class TestPatienceIsATwoBandStep:
    """A step with an absolute time cap, NOT a continuous ramp.

    The design memo (§2.D-i) measured the dirtiness score moving in +5/+15/+20
    event-driven steps, so a ramp's intermediate values are never visited in a
    way that changes behaviour — its shape would be cosmetic while implying a
    precision the input does not have.
    """

    def test_output_is_only_ever_one_of_two_values(self) -> None:
        seen = {
            patience(
                zone_score=float(score),
                over_threshold_since=NOW - timedelta(hours=hours),
                now=NOW,
                cfg=CFG,
            )
            for score in range(0, 101)
            for hours in range(0, 13)
        }
        assert seen == {PATIENT, IMPATIENT}

    def test_score_axis_is_a_single_step_at_the_impatient_score(self) -> None:
        since = NOW - timedelta(hours=1)
        values = [
            patience(zone_score=float(s), over_threshold_since=since, now=NOW, cfg=CFG)
            for s in range(0, 101)
        ]
        transitions = [i for i in range(1, len(values)) if values[i] != values[i - 1]]
        assert transitions == [int(CFG.patience_impatient_score)]
        assert values[84] == PATIENT
        assert values[85] == IMPATIENT

    def test_time_axis_is_a_single_step_at_the_hard_cap(self) -> None:
        values = [
            patience(
                zone_score=50.0,
                over_threshold_since=NOW - timedelta(minutes=m),
                now=NOW,
                cfg=CFG,
            )
            for m in range(0, 481, 30)
        ]
        transitions = [i for i in range(1, len(values)) if values[i] != values[i - 1]]
        assert len(transitions) == 1
        assert values[transitions[0] - 1] == PATIENT
        assert values[transitions[0]] == IMPATIENT

    def test_hard_cap_fires_while_the_score_is_low(self) -> None:
        """The ABSOLUTE TIME CAP is the primary starvation guard, not the score."""
        assert (
            patience(
                zone_score=51.0,
                over_threshold_since=NOW - timedelta(hours=6, minutes=1),
                now=NOW,
                cfg=CFG,
            )
            == IMPATIENT
        )
        assert (
            patience_band(
                zone_score=51.0,
                over_threshold_since=NOW - timedelta(hours=6, minutes=1),
                now=NOW,
                cfg=CFG,
            )
            == "impatient:hard_cap"
        )

    def test_score_band_fires_while_under_the_cap(self) -> None:
        assert (
            patience(
                zone_score=90.0,
                over_threshold_since=NOW - timedelta(minutes=10),
                now=NOW,
                cfg=CFG,
            )
            == IMPATIENT
        )

    def test_boundaries_are_inclusive_on_the_impatient_side(self) -> None:
        exactly_cap = NOW - timedelta(hours=CFG.patience_hard_cap_h)
        assert (
            patience(
                zone_score=10.0, over_threshold_since=exactly_cap, now=NOW, cfg=CFG
            )
            == IMPATIENT
        )
        assert (
            patience(
                zone_score=CFG.patience_impatient_score,
                over_threshold_since=NOW - timedelta(minutes=5),
                now=NOW,
                cfg=CFG,
            )
            == IMPATIENT
        )

    def test_unknown_threshold_clock_makes_the_rule_inert_not_infinitely_patient(
        self,
    ) -> None:
        """Fail toward today's behaviour.

        Without the clock the starvation cap cannot fire, so a rule that could
        defer indefinitely must not run at all. 0.0 makes A3 short-circuit to
        PASS — the dispatch decision stays exactly where it is today.
        """
        assert (
            patience(zone_score=10.0, over_threshold_since=None, now=NOW, cfg=CFG)
            == IMPATIENT
        )
        assert (
            patience_band(zone_score=10.0, over_threshold_since=None, now=NOW, cfg=CFG)
            == "impatient:no_threshold_clock"
        )

    def test_naive_datetime_raises_rather_than_silently_shifting_seven_hours(
        self,
    ) -> None:
        with pytest.raises(ValueError):
            patience(
                zone_score=10.0,
                over_threshold_since=datetime(2026, 9, 8, 6, 0),
                now=NOW,
                cfg=CFG,
            )

    def test_impatience_collapses_the_lookahead_to_zero_slots(self) -> None:
        """Structural, not just a branch A3 must remember to take."""
        assert lookahead_slot_count(patience_value=IMPATIENT, cfg=CFG) == 0
        read = opportunity(
            now=NOW,
            slots=ladder(*[0.05 * i for i in range(12)]),
            duration=P75_35,
            cfg=CFG,
            patience_value=IMPATIENT,
        )
        assert read.best_slot_offset is None
        assert read.best_slot_gain == 0.0
        assert read.p_clear_curve == ()

    def test_redis_key_shape_matches_the_spec(self) -> None:
        assert (
            over_threshold_since_key(19) == "cortex:vacuumops:over_threshold_since:19"
        )


# ── Fit arithmetic ───────────────────────────────────────────────────────────


class TestFitArithmetic:
    """Hand-computed expected values. Spec §9.1: assert the numbers."""

    def test_mission_wholly_inside_one_slot(self) -> None:
        duration = DurationEstimate(minutes=20.0, basis="p75_active+return_leg")
        read = opportunity(
            now=NOW,
            slots=ladder(0.20, 0.80, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10),
            duration=duration,
            cfg=CFG,
            patience_value=PATIENT,
        )
        # 20 of 30 minutes in slot 0 -> 0.8 ** (20/30)
        assert read.expected_fit_now == pytest.approx(0.8 ** (2 / 3))
        assert read.p_clear_now == pytest.approx(0.8)

    def test_mission_straddling_two_slots(self) -> None:
        duration = DurationEstimate(minutes=45.0, basis="p75_active+return_leg")
        read = opportunity(
            now=NOW,
            slots=ladder(0.20, 0.50, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10),
            duration=duration,
            cfg=CFG,
        )
        # 30 min of slot 0 (p=0.8) + 15 min of slot 1 (p=0.5)
        expected = (0.8**1.0) * (0.5**0.5)
        assert read.expected_fit_now == pytest.approx(expected)

    def test_mission_straddling_three_slots(self) -> None:
        duration = DurationEstimate(minutes=70.0, basis="p75_active+return_leg")
        read = opportunity(
            now=NOW,
            slots=ladder(
                0.20, 0.50, 0.60, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10
            ),
            duration=duration,
            cfg=CFG,
        )
        expected = (0.8**1.0) * (0.5**1.0) * (0.4 ** (10 / 30))
        assert read.expected_fit_now == pytest.approx(expected)

    def test_mid_slot_start_weights_only_the_remainder_of_the_current_slot(
        self,
    ) -> None:
        """A mission starting at :35 gets 25 min of the current slot, not 30."""
        now = local(2026, 9, 8, 12, 35)
        dow, slot0 = slot_key(now)
        slots = []
        for i, occ in enumerate(
            [0.20, 0.50, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10]
        ):
            dow_i, slot_i = slot_key(now + timedelta(minutes=30 * i))
            slots.append(
                SlotPrior(
                    entity_id="e",
                    day_of_week=dow_i,
                    slot=slot_i,
                    mean_occupied=occ,
                    stddev_occupied=None,
                    native_count=5,
                    sample_count=5,
                    confidence=CONFIDENCE_GOOD,
                )
            )
        duration = DurationEstimate(minutes=40.0, basis="p75_active+return_leg")
        read = opportunity(now=now, slots=slots, duration=duration, cfg=CFG)

        # 25 min of slot 0 (p=0.8), 15 min of slot 1 (p=0.5)
        expected = (0.8 ** (25 / 30)) * (0.5 ** (15 / 30))
        assert read.expected_fit_now == pytest.approx(expected)
        assert (dow, slot0) == slot_key(now)

    def test_a_better_forward_window_is_found_and_reported_in_minutes(self) -> None:
        duration = DurationEstimate(minutes=30.0, basis="p75_active+return_leg")
        # Now is bad (0.9 occupied); two slots ahead is excellent (0.02).
        read = opportunity(
            now=NOW,
            slots=ladder(0.90, 0.90, 0.02, 0.02, 0.05, 0.05, 0.10, 0.10, 0.10, 0.10),
            duration=duration,
            cfg=CFG,
        )
        assert read.best_slot_offset == 2
        assert read.best_slot_gain == pytest.approx(0.98 - 0.10)
        assert read.expected_fit_now == pytest.approx(0.10)
        # This is the shape A3 turns into a DEFER — both bands cleared.
        assert read.best_slot_gain >= CFG.opportunity_strong_gain
        assert read.expected_fit_now <= CFG.opportunity_weak_fit
        assert read.confidence == CONFIDENCE_GOOD

    def test_no_forward_slot_beats_now(self) -> None:
        duration = DurationEstimate(minutes=30.0, basis="p75_active+return_leg")
        read = opportunity(
            now=NOW,
            slots=ladder(0.02, 0.02, 0.60, 0.70, 0.80, 0.85, 0.90, 0.90, 0.90, 0.90),
            duration=duration,
            cfg=CFG,
        )
        assert read.best_slot_offset is None
        assert read.best_slot_gain == 0.0
        assert read.expected_fit_now > CFG.opportunity_marginal_fit

    def test_best_offset_can_sit_on_the_lookahead_boundary(self) -> None:
        duration = DurationEstimate(minutes=30.0, basis="p75_active+return_leg")
        occ = [0.90] * 6 + [0.01] * 8  # only the last reachable slot is good
        read = opportunity(now=NOW, slots=ladder(*occ), duration=duration, cfg=CFG)
        assert read.lookahead_slots == 6
        assert read.best_slot_offset == 6

    def test_fit_is_bounded_and_monotone_in_occupancy(self) -> None:
        duration = DurationEstimate(minutes=30.0, basis="p75_active+return_leg")
        previous = 1.1
        for occ in (0.05, 0.25, 0.45, 0.65, 0.85):
            slots = ladder(occ, occ + 0.01, *[0.5 + 0.01 * i for i in range(10)])
            read = opportunity(now=NOW, slots=slots, duration=duration, cfg=CFG)
            assert 0.0 <= read.expected_fit_now <= 1.0
            assert read.expected_fit_now < previous
            previous = read.expected_fit_now


# ── Slot geometry ────────────────────────────────────────────────────────────


class TestSlotGeometry:
    def test_lookahead_is_patience_times_horizon_in_slots(self) -> None:
        assert lookahead_slot_count(patience_value=PATIENT, cfg=CFG) == 6  # 3h / 30min
        assert lookahead_slot_count(patience_value=IMPATIENT, cfg=CFG) == 0

    def test_lookahead_respects_a_non_default_slot_length(self) -> None:
        assert (
            lookahead_slot_count(patience_value=PATIENT, cfg=CFG, slot_minutes=60) == 3
        )

    def test_lookahead_rejects_a_slot_length_that_does_not_divide_a_day(self) -> None:
        with pytest.raises(ValueError):
            lookahead_slot_count(patience_value=PATIENT, cfg=CFG, slot_minutes=7)

    def test_required_slot_count_covers_the_last_candidate_mission(self) -> None:
        # last start = 6 slots ahead (180 min), + up to 30 min of in-slot offset,
        # + a 35 min mission -> ceil(245/30) = 9
        assert required_slot_count(duration_min=35.0, lookahead_slots=6) == 9

    def test_forward_slot_keys_are_contiguous_and_wrap_the_day(self) -> None:
        # Tuesday 23:15 local -> slot 46, then 47, then Wednesday 0 and 1.
        keys = forward_slot_keys(now=local(2026, 9, 8, 23, 15), count=4)
        assert keys == [(1, 46), (1, 47), (2, 0), (2, 1)]

    def test_forward_slot_keys_start_at_the_slot_containing_now(self) -> None:
        keys = forward_slot_keys(now=NOW, count=3)
        assert keys[0] == slot_key(NOW)
        assert len(keys) == 3


# ── The record itself ────────────────────────────────────────────────────────


class TestOpportunityRead:
    def test_it_never_recommends_dispatching(self) -> None:
        """The record carries a fit and at most a better window. Nothing else.

        A2 cannot force a dispatch by construction: `OpportunityRead` has no
        field that means "go", and A3's rule returns only PASS / FAIL /
        AMBIGUOUS on the COMFORT tier — behind effectiveness rules that
        short-circuit first.
        """
        fields = set(OpportunityReadFields())
        assert not fields & {"dispatch", "force", "override", "actuate", "go"}

    def test_format_is_loggable_and_names_degradation(self) -> None:
        read = opportunity(now=NOW, slots=[], duration=P75_35, cfg=CFG)
        rendered = format_opportunity(read)
        assert "conf=unavailable" in rendered
        assert "degraded=" in rendered

        good = opportunity(
            now=NOW,
            slots=ladder(0.90, 0.90, 0.02, 0.02, 0.05, 0.05, 0.10, 0.10, 0.10, 0.10),
            duration=DurationEstimate(minutes=30.0, basis="p75_active+return_leg"),
            cfg=CFG,
        )
        rendered = format_opportunity(good)
        assert "best=+60m" in rendered
        assert "basis=p75_active+return_leg" in rendered

    def test_curve_length_matches_the_lookahead(self) -> None:
        read = opportunity(
            now=NOW,
            slots=ladder(*[0.05 * i for i in range(12)]),
            duration=P75_35,
            cfg=CFG,
        )
        assert len(read.p_clear_curve) == read.lookahead_slots == 6
        assert read.p_clear_curve[0] == pytest.approx(read.p_clear_now)
        assert all(0.0 <= v <= 1.0 and math.isfinite(v) for v in read.p_clear_curve)


def OpportunityReadFields() -> list[str]:
    from dataclasses import fields as dc_fields

    from cortex_python.modules.vacuumops.schemas import OpportunityRead

    return [f.name for f in dc_fields(OpportunityRead)]
