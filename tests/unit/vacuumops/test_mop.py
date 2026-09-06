"""Tests for the mop-cadence gate (modules/vacuumops/mop.py).

Design under test — locked by Carlos 2026-07-03 (D14–D18):
    "Mop intelligence (Saros only): signal -> schedule (7-day) -> score
     threshold -> off. Intensity: light/deep."

Coverage:
  §1 days_since()            — timestamp normalization + clock-skew clamp
  §2 evaluate_mop_need()     — each of the three arms, plus the off paths
  §3 resolve_batch_mop()     — deterministic OR join at the robot boundary
  §4 intensity mapping       — light/deep -> HA values, floor-type safety cap
  §5 job scoping             — Saros rooms mop; litter box and iRobot never do
  §6 trigger_vacuum payload  — the wiring the ticket was actually filed for
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from cortex_python.modules.vacuumops.config import VacuumOpsConfig
from cortex_python.modules.vacuumops.jobs import (
    Ethan3FRoomsJob,
    Saros1FLitterBoxJob,
    Saros1FRoomsJob,
    VacuumJob,
)
from cortex_python.modules.vacuumops.mop import (
    days_since,
    evaluate_mop_need,
    resolve_batch_mop,
)
from cortex_python.modules.vacuumops.schemas import BatchEntry, ZoneMeta
from tests.unit.vacuumops.conftest import make_snapshot

# Saros 1F zone ids (see jobs.py)
_KITCHEN = 19
_LIVING_ROOM = 21
_HALLWAY = 22
_LITTER_BOX = 23

_NOW = datetime(2026, 8, 9, 15, 0, 0, tzinfo=UTC)


# ── helpers ──────────────────────────────────────────────────────────────────


def make_meta(
    zone_id: int,
    *,
    last_mopped_days_ago: float | None = 1.0,
    mop_requested: bool = False,
    floor_type: str | None = "laminate",
    never_mopped: bool = False,
    tracking_available: bool = True,
) -> ZoneMeta:
    """Build a ZoneMeta with mop state relative to _NOW.

    tracking_available defaults True = "HomeOps has shipped migration
    20260809000000". Set it False to simulate a cortex-before-homeops deploy.
    """
    last_mopped = (
        None
        if never_mopped or last_mopped_days_ago is None
        else _NOW - timedelta(days=last_mopped_days_ago)
    )
    return ZoneMeta(
        zone_id=zone_id,
        unit_id=3,
        floor_type=floor_type,
        last_mopped_at=last_mopped,
        mop_requested_at=_NOW - timedelta(hours=2) if mop_requested else None,
        mop_tracking_available=tracking_available,
    )


def enabled_cfg(**kw) -> VacuumOpsConfig:
    """VacuumOpsConfig with the mop gate switched ON.

    The production default is OFF (opt-in), so any test asserting that a mop
    actually happens must enable it explicitly.
    """
    return VacuumOpsConfig(mop_enabled=True, **kw)


def ctx_with(metas: list[ZoneMeta], scores: dict[int, float] | None = None):
    ctx = make_snapshot(timestamp=_NOW)
    ctx.zone_metadata = {m.zone_id: m for m in metas}
    if scores is not None:
        ctx.zone_scores = scores
    return ctx


def batch_of(*zone_ids: int) -> list[BatchEntry]:
    return [BatchEntry(zone=z, bundled=False, score=60.0) for z in zone_ids]


def job_map(zone_ids: list[int], job: VacuumJob) -> dict[int, VacuumJob]:
    return dict.fromkeys(zone_ids, job)


# ── §1: days_since() ─────────────────────────────────────────────────────────


class TestDaysSince:
    def test_none_timestamp_returns_none(self):
        assert days_since(None, _NOW) is None

    def test_exact_days(self):
        assert days_since(_NOW - timedelta(days=7), _NOW) == pytest.approx(7.0)

    def test_fractional_days(self):
        assert days_since(_NOW - timedelta(hours=36), _NOW) == pytest.approx(1.5)

    def test_naive_timestamp_treated_as_utc(self):
        """HomeOps MySQL TIMESTAMPs can deserialize without a timezone.

        Comparing naive against the tz-aware tick timestamp would raise
        TypeError and take down the tick.
        """
        naive = (_NOW - timedelta(days=3)).replace(tzinfo=None)
        assert days_since(naive, _NOW) == pytest.approx(3.0)

    def test_future_timestamp_clamped_to_zero(self):
        """Clock skew must not read as 'hugely overdue' through a sign error."""
        assert days_since(_NOW + timedelta(days=5), _NOW) == 0.0


# ── §2: evaluate_mop_need() — the three arms ─────────────────────────────────


class TestEvaluateMopNeed:
    def test_job_with_mop_disabled_never_needs(self):
        job = Saros1FLitterBoxJob()
        ctx = ctx_with([make_meta(_LITTER_BOX, never_mopped=True)])
        need = evaluate_mop_need(job, _LITTER_BOX, ctx, _NOW)
        assert need.needed is False
        assert need.reason == "job_mop_disabled"

    def test_mop_tracking_unavailable_declines_instead_of_deep_mopping(self):
        """cortex-deployed-before-homeops must NOT read as 'never mopped'.

        Without feature detection, last_mopped_at=None is indistinguishable
        from a genuinely never-mopped zone, and the schedule arm treats that as
        maximally overdue — firing an immediate DEEP mop across every Saros 1F
        zone on the first tick after deploy.
        """
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [make_meta(_KITCHEN, never_mopped=True, tracking_available=False)]
        )
        need = evaluate_mop_need(job, _KITCHEN, ctx, _NOW)
        assert need.needed is False
        assert need.reason == "mop_tracking_unavailable"
        assert need.deep is False

    def test_tracking_unavailable_beats_every_arm(self):
        """Not just the schedule arm — a pending signal must not slip through."""
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [
                make_meta(
                    _KITCHEN,
                    never_mopped=True,
                    mop_requested=True,
                    tracking_available=False,
                )
            ],
            scores={_KITCHEN: 99.0},
        )
        need = evaluate_mop_need(job, _KITCHEN, ctx, _NOW)
        assert need.needed is False
        assert need.reason == "mop_tracking_unavailable"

    def test_missing_metadata_declines_rather_than_guesses(self):
        """Degraded context: get_zone_metadata() returns {} on failure.

        Declining costs one cadence cycle; an unwanted wet run on unknown
        state is the worse failure.
        """
        job = Saros1FRoomsJob()
        ctx = ctx_with([])  # no metadata at all
        need = evaluate_mop_need(job, _KITCHEN, ctx, _NOW)
        assert need.needed is False
        assert need.reason == "metadata_unavailable"

    # ── arm 1: signal ──
    def test_signal_arm_fires_on_request(self):
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [make_meta(_KITCHEN, last_mopped_days_ago=1.0, mop_requested=True)]
        )
        need = evaluate_mop_need(job, _KITCHEN, ctx, _NOW)
        assert need.needed is True
        assert need.arm == "signal"

    def test_signal_outranks_schedule_and_score(self):
        """An explicit request is reported even when other arms would also fire."""
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [make_meta(_KITCHEN, last_mopped_days_ago=30.0, mop_requested=True)],
            scores={_KITCHEN: 99.0},
        )
        need = evaluate_mop_need(job, _KITCHEN, ctx, _NOW)
        assert need.arm == "signal"

    # ── arm 2: 7-day schedule ──
    def test_schedule_arm_fires_at_cadence(self):
        job = Saros1FRoomsJob()  # mop_cadence_days = 7
        ctx = ctx_with([make_meta(_KITCHEN, last_mopped_days_ago=7.0)])
        need = evaluate_mop_need(job, _KITCHEN, ctx, _NOW)
        assert need.needed is True
        assert need.arm == "schedule"

    def test_schedule_arm_does_not_fire_before_cadence(self):
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [make_meta(_KITCHEN, last_mopped_days_ago=6.9)], scores={_KITCHEN: 10.0}
        )
        need = evaluate_mop_need(job, _KITCHEN, ctx, _NOW)
        assert need.needed is False
        assert need.reason.startswith("not_due:")

    def test_never_mopped_is_maximally_overdue(self):
        job = Saros1FRoomsJob()
        ctx = ctx_with([make_meta(_KITCHEN, never_mopped=True)])
        need = evaluate_mop_need(job, _KITCHEN, ctx, _NOW)
        assert need.needed is True
        assert need.arm == "schedule"
        assert need.reason == "schedule:never_mopped"
        assert need.deep is True
        assert need.days_since_mopped is None

    # ── arm 3: score threshold ──
    def test_score_arm_fires_above_threshold(self):
        job = Saros1FRoomsJob()  # mop_score_threshold = 80
        ctx = ctx_with(
            [make_meta(_KITCHEN, last_mopped_days_ago=2.0)], scores={_KITCHEN: 85.0}
        )
        need = evaluate_mop_need(job, _KITCHEN, ctx, _NOW)
        assert need.needed is True
        assert need.arm == "score"
        assert need.reason == "score:85"

    def test_score_arm_ignores_merely_dispatch_eligible_zone(self):
        """dispatch_threshold is 50; a zone at 60 is vacuum-eligible but not mop-eligible."""
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [make_meta(_KITCHEN, last_mopped_days_ago=1.0)], scores={_KITCHEN: 60.0}
        )
        need = evaluate_mop_need(job, _KITCHEN, ctx, _NOW)
        assert need.needed is False

    # ── arm 3: same-day score cooldown ──
    #
    # The score arm and the schedule arm are independent `if`s with no time
    # coupling. Kitchen / Prep Area / Dining Table decay at 20 / 50 / 18 per
    # day and re-saturate to a clamped 100 within hours of a cooking or meal
    # signal, so on score alone they re-triggered a wet pass the same day they
    # were mopped. mop_score_cooldown_days is the missing floor.

    def test_score_arm_suppressed_inside_the_cooldown(self):
        """Score is over threshold, but the zone was mopped hours ago."""
        job = Saros1FRoomsJob()  # mop_score_cooldown_days = 1.0
        ctx = ctx_with(
            [make_meta(_KITCHEN, last_mopped_days_ago=0.3)], scores={_KITCHEN: 100.0}
        )
        need = evaluate_mop_need(job, _KITCHEN, ctx, _NOW)
        assert need.needed is False
        assert need.arm is None
        assert need.days_since_mopped == pytest.approx(0.3)

    def test_cooldown_reason_is_distinct_from_not_due(self):
        """The reason string is the whole audit trail for an unsupervised wet run.

        "the gate wanted to mop and chose not to" must not be hidden behind the
        generic not_due tail.
        """
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [make_meta(_KITCHEN, last_mopped_days_ago=0.5)], scores={_KITCHEN: 92.0}
        )
        need = evaluate_mop_need(job, _KITCHEN, ctx, _NOW)
        assert need.reason == "score_cooldown:0.5d"
        assert not need.reason.startswith("not_due:")
        # mop_reason is VARCHAR(64) in HomeOps.
        assert len(need.reason) <= 64

    def test_score_arm_fires_once_the_cooldown_has_elapsed(self):
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [make_meta(_KITCHEN, last_mopped_days_ago=1.0)], scores={_KITCHEN: 100.0}
        )
        need = evaluate_mop_need(job, _KITCHEN, ctx, _NOW)
        assert need.needed is True
        assert need.arm == "score"
        assert need.reason == "score:100"

    def test_cooldown_length_is_configurable(self):
        job = dataclasses.replace(Saros1FRoomsJob(), mop_score_cooldown_days=3.0)
        ctx = ctx_with(
            [make_meta(_KITCHEN, last_mopped_days_ago=2.0)], scores={_KITCHEN: 95.0}
        )
        need = evaluate_mop_need(job, _KITCHEN, ctx, _NOW)
        assert need.needed is False
        assert need.reason == "score_cooldown:2.0d"

    def test_below_threshold_inside_cooldown_still_reads_not_due(self):
        """The cooldown reason is reserved for a score that actually cleared."""
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [make_meta(_KITCHEN, last_mopped_days_ago=0.4)], scores={_KITCHEN: 60.0}
        )
        need = evaluate_mop_need(job, _KITCHEN, ctx, _NOW)
        assert need.needed is False
        assert need.reason == "not_due:0.4d"

    def test_schedule_arm_is_unaffected_by_the_cooldown(self):
        """The 7-day floor fires unconditionally, whatever the cooldown is set to."""
        job = dataclasses.replace(Saros1FRoomsJob(), mop_score_cooldown_days=30.0)
        ctx = ctx_with(
            [make_meta(_KITCHEN, last_mopped_days_ago=7.5)], scores={_KITCHEN: 100.0}
        )
        need = evaluate_mop_need(job, _KITCHEN, ctx, _NOW)
        assert need.needed is True
        assert need.arm == "schedule"
        assert need.reason == "schedule:7.5d"

    def test_never_mopped_is_unaffected_by_the_cooldown(self):
        """elapsed is None cannot reach the score arm — arm 2 returns first."""
        job = dataclasses.replace(Saros1FRoomsJob(), mop_score_cooldown_days=30.0)
        ctx = ctx_with(
            [make_meta(_KITCHEN, never_mopped=True)], scores={_KITCHEN: 100.0}
        )
        need = evaluate_mop_need(job, _KITCHEN, ctx, _NOW)
        assert need.needed is True
        assert need.arm == "schedule"
        assert need.reason == "schedule:never_mopped"

    def test_signal_arm_outranks_the_cooldown(self):
        """An explicit request is unconditional; the cooldown does not apply to it."""
        job = dataclasses.replace(Saros1FRoomsJob(), mop_score_cooldown_days=30.0)
        ctx = ctx_with(
            [make_meta(_KITCHEN, last_mopped_days_ago=0.1, mop_requested=True)],
            scores={_KITCHEN: 100.0},
        )
        need = evaluate_mop_need(job, _KITCHEN, ctx, _NOW)
        assert need.needed is True
        assert need.arm == "signal"
        assert need.reason == "signal:requested"

    def test_cooldown_suppression_keeps_the_batch_dry(self):
        """End to end: the only score-eligible zone is inside its cooldown."""
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [make_meta(_KITCHEN, last_mopped_days_ago=0.2)], scores={_KITCHEN: 100.0}
        )
        d = resolve_batch_mop(
            batch_of(_KITCHEN), ctx, job_map([_KITCHEN], job), enabled_cfg(), _NOW
        )
        assert d.mop is False
        assert d.reason == "off:no_zone_due"
        assert d.triggering_zones == []

    # ── deep vs light ──
    def test_deep_flag_set_when_far_past_cadence(self):
        job = Saros1FRoomsJob()  # mop_deep_after_days = 14
        ctx = ctx_with([make_meta(_KITCHEN, last_mopped_days_ago=15.0)])
        assert evaluate_mop_need(job, _KITCHEN, ctx, _NOW).deep is True

    def test_deep_flag_not_set_at_ordinary_cadence(self):
        job = Saros1FRoomsJob()
        ctx = ctx_with([make_meta(_KITCHEN, last_mopped_days_ago=8.0)])
        assert evaluate_mop_need(job, _KITCHEN, ctx, _NOW).deep is False


# ── §3: resolve_batch_mop() — the deterministic OR join ──────────────────────


class TestResolveBatchMop:
    def test_empty_batch_is_off(self):
        ctx = ctx_with([])
        d = resolve_batch_mop([], ctx, {}, enabled_cfg(), _NOW)
        assert d.mop is False
        assert d.reason == "off:empty_batch"

    def test_module_kill_switch_forces_off(self):
        """Gate still evaluates, but resolves to dry so the trail can be reviewed."""
        job = Saros1FRoomsJob()
        ctx = ctx_with([make_meta(_KITCHEN, never_mopped=True)])
        cfg = VacuumOpsConfig(mop_enabled=False)
        d = resolve_batch_mop(
            batch_of(_KITCHEN), ctx, job_map([_KITCHEN], job), cfg, _NOW
        )
        assert d.mop is False
        assert d.reason.startswith("off:disabled")

    def test_kill_switch_shadow_mode_records_what_it_would_have_done(self):
        """The switch exists so the trail can be reviewed BEFORE going wet.

        A flat "off:module_disabled" with no per-zone reasoning would make the
        switch useless for that purpose, so the arms must still be evaluated.
        """
        job = Saros1FRoomsJob()
        ctx = ctx_with([make_meta(_KITCHEN, last_mopped_days_ago=9.0)])
        d = resolve_batch_mop(
            batch_of(_KITCHEN),
            ctx,
            job_map([_KITCHEN], job),
            VacuumOpsConfig(mop_enabled=False),
            _NOW,
        )
        assert d.mop is False
        assert d.intensity is None  # never dispatched
        # The real reasoning survives into the decision log.
        assert "would:" in d.reason
        assert "schedule:9.0d" in d.reason
        assert d.triggering_zones == [_KITCHEN]

    def test_shadow_mode_distinguishes_would_mop_from_not_due(self):
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [make_meta(_KITCHEN, last_mopped_days_ago=1.0)], scores={_KITCHEN: 10.0}
        )
        d = resolve_batch_mop(
            batch_of(_KITCHEN),
            ctx,
            job_map([_KITCHEN], job),
            VacuumOpsConfig(mop_enabled=False),
            _NOW,
        )
        assert d.reason == "off:disabled(not_due)"

    def test_mop_reason_fits_the_homeops_column(self):
        """vac_decisions.mop_reason is VARCHAR(64); a longer string would truncate."""
        job = Saros1FRoomsJob()
        ctx = ctx_with([make_meta(_KITCHEN, never_mopped=True, floor_type=None)])
        for cfg in (VacuumOpsConfig(mop_enabled=False), enabled_cfg()):
            d = resolve_batch_mop(
                batch_of(_KITCHEN), ctx, job_map([_KITCHEN], job), cfg, _NOW
            )
            assert len(d.reason) <= 64

    def test_no_zone_due_is_off(self):
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [make_meta(_KITCHEN, last_mopped_days_ago=1.0)],
            scores={_KITCHEN: 20.0},
        )
        d = resolve_batch_mop(
            batch_of(_KITCHEN), ctx, job_map([_KITCHEN], job), enabled_cfg(), _NOW
        )
        assert d.mop is False
        assert d.reason == "off:no_zone_due"

    def test_one_due_zone_makes_the_whole_batch_wet(self):
        """Mop intensity is a unit-level HA setting — the pad is down or it is not."""
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [
                make_meta(_KITCHEN, last_mopped_days_ago=9.0),  # due
                make_meta(_LIVING_ROOM, last_mopped_days_ago=1.0),  # not due
                make_meta(_HALLWAY, last_mopped_days_ago=2.0),  # not due
            ],
            scores={_KITCHEN: 60.0, _LIVING_ROOM: 60.0, _HALLWAY: 60.0},
        )
        zones = [_KITCHEN, _LIVING_ROOM, _HALLWAY]
        d = resolve_batch_mop(
            batch_of(*zones), ctx, job_map(zones, job), enabled_cfg(), _NOW
        )
        assert d.mop is True
        # Only the genuinely-due zone is credited as triggering; the rest ride along.
        assert d.triggering_zones == [_KITCHEN]

    def test_arm_precedence_reported_across_zones(self):
        """signal > schedule > score for *reporting*; any arm still causes the mop."""
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [
                make_meta(_KITCHEN, last_mopped_days_ago=9.0),  # schedule
                make_meta(
                    _LIVING_ROOM, last_mopped_days_ago=1.0, mop_requested=True
                ),  # signal
            ],
            scores={_KITCHEN: 60.0, _LIVING_ROOM: 60.0},
        )
        zones = [_KITCHEN, _LIVING_ROOM]
        d = resolve_batch_mop(
            batch_of(*zones), ctx, job_map(zones, job), enabled_cfg(), _NOW
        )
        assert d.mop is True
        assert d.reason.startswith("signal")
        assert set(d.triggering_zones) == {_KITCHEN, _LIVING_ROOM}

    def test_zone_without_a_job_mapping_is_skipped_not_fatal(self):
        job = Saros1FRoomsJob()
        ctx = ctx_with([make_meta(_KITCHEN, never_mopped=True)])
        # _LIVING_ROOM deliberately absent from the job map
        d = resolve_batch_mop(
            batch_of(_KITCHEN, _LIVING_ROOM),
            ctx,
            job_map([_KITCHEN], job),
            enabled_cfg(),
            _NOW,
        )
        assert d.mop is True


# ── §4: intensity mapping + floor-type safety cap ────────────────────────────


class TestIntensityMapping:
    def test_light_maps_to_low(self):
        job = Saros1FRoomsJob()
        ctx = ctx_with([make_meta(_KITCHEN, last_mopped_days_ago=8.0)])  # due, not deep
        d = resolve_batch_mop(
            batch_of(_KITCHEN), ctx, job_map([_KITCHEN], job), enabled_cfg(), _NOW
        )
        assert d.intensity == "low"

    def test_deep_maps_to_high(self):
        job = Saros1FRoomsJob()
        ctx = ctx_with([make_meta(_KITCHEN, last_mopped_days_ago=20.0)])  # deep
        d = resolve_batch_mop(
            batch_of(_KITCHEN), ctx, job_map([_KITCHEN], job), enabled_cfg(), _NOW
        )
        assert d.intensity == "high"

    def test_extremes_of_ha_scale_are_never_used(self):
        cfg = VacuumOpsConfig()
        assert cfg.mop_intensity_light not in ("slight", "extreme")
        assert cfg.mop_intensity_deep not in ("slight", "extreme")

    def test_hardwood_in_batch_caps_deep_to_light(self):
        """Intensity is unit-level: the wettest setting must suit the most
        water-sensitive surface in the run, not the dirtiest zone."""
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [
                make_meta(
                    _KITCHEN, last_mopped_days_ago=30.0, floor_type="tile"
                ),  # deep
                make_meta(
                    _LIVING_ROOM, last_mopped_days_ago=1.0, floor_type="hardwood"
                ),
            ],
            scores={_KITCHEN: 60.0, _LIVING_ROOM: 60.0},
        )
        zones = [_KITCHEN, _LIVING_ROOM]
        d = resolve_batch_mop(
            batch_of(*zones), ctx, job_map(zones, job), enabled_cfg(), _NOW
        )
        assert d.mop is True
        assert d.intensity == "low"
        assert "capped_light" in d.reason

    def test_cap_considers_whole_batch_not_just_triggering_zones(self):
        """The hardwood zone here is a bundled rider, not a trigger — still caps."""
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [
                make_meta(_KITCHEN, never_mopped=True, floor_type="tile"),
                make_meta(_HALLWAY, last_mopped_days_ago=0.5, floor_type="hardwood"),
            ],
            scores={_KITCHEN: 60.0, _HALLWAY: 60.0},
        )
        zones = [_KITCHEN, _HALLWAY]
        d = resolve_batch_mop(
            batch_of(*zones), ctx, job_map(zones, job), enabled_cfg(), _NOW
        )
        assert d.triggering_zones == [_KITCHEN]
        assert d.intensity == "low"

    def test_null_floor_type_caps_to_light(self):
        """floor_type is nullable with no enforced backfill.

        An unknown surface cannot be shown to tolerate a deep pass, so it must
        fail CLOSED — consistent with the module's stance everywhere else.
        """
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [make_meta(_KITCHEN, last_mopped_days_ago=30.0, floor_type=None)]
        )
        d = resolve_batch_mop(
            batch_of(_KITCHEN), ctx, job_map([_KITCHEN], job), enabled_cfg(), _NOW
        )
        assert d.mop is True
        assert d.intensity == "low"
        assert "capped_light" in d.reason

    def test_null_floor_type_on_a_rider_zone_still_caps(self):
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [
                make_meta(_KITCHEN, last_mopped_days_ago=30.0, floor_type="tile"),
                make_meta(_HALLWAY, last_mopped_days_ago=0.5, floor_type=None),
            ],
            scores={_KITCHEN: 60.0, _HALLWAY: 60.0},
        )
        zones = [_KITCHEN, _HALLWAY]
        d = resolve_batch_mop(
            batch_of(*zones), ctx, job_map(zones, job), enabled_cfg(), _NOW
        )
        assert d.intensity == "low"

    def test_zone_missing_from_metadata_caps_to_light(self):
        """Degraded context inside an otherwise-deep batch: unknown surface."""
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [make_meta(_KITCHEN, last_mopped_days_ago=30.0, floor_type="tile")],
            scores={_KITCHEN: 60.0, _HALLWAY: 60.0},
        )
        # _HALLWAY is in the batch but absent from zone_metadata entirely.
        zones = [_KITCHEN, _HALLWAY]
        d = resolve_batch_mop(
            batch_of(*zones), ctx, job_map(zones, job), enabled_cfg(), _NOW
        )
        assert d.mop is True
        assert d.intensity == "low"

    def test_no_hardwood_leaves_deep_intact(self):
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [
                make_meta(_KITCHEN, last_mopped_days_ago=30.0, floor_type="tile"),
                make_meta(_HALLWAY, last_mopped_days_ago=1.0, floor_type="laminate"),
            ],
            scores={_KITCHEN: 60.0, _HALLWAY: 60.0},
        )
        zones = [_KITCHEN, _HALLWAY]
        d = resolve_batch_mop(
            batch_of(*zones), ctx, job_map(zones, job), enabled_cfg(), _NOW
        )
        assert d.intensity == "high"
        assert "capped_light" not in d.reason

    def test_mapping_is_configurable(self):
        job = Saros1FRoomsJob()
        cfg = enabled_cfg(mop_intensity_light="medium", mop_intensity_deep="extreme")
        ctx = ctx_with([make_meta(_KITCHEN, last_mopped_days_ago=8.0)])
        d = resolve_batch_mop(
            batch_of(_KITCHEN), ctx, job_map([_KITCHEN], job), cfg, _NOW
        )
        assert d.intensity == "medium"


# ── §5: job scoping — who is allowed to mop ──────────────────────────────────


class TestJobScoping:
    def test_saros_rooms_job_has_mop_enabled(self):
        assert Saros1FRoomsJob().mop_enabled is True

    def test_saros_litter_box_stays_vacuum_only(self):
        """Original design scoped the mop model to room zones."""
        assert Saros1FLitterBoxJob().mop_enabled is False

    def test_irobot_jobs_never_mop(self):
        """Braava is excluded from the CORTEX mop model; Ethan/Sam are dry units."""
        assert Ethan3FRoomsJob().mop_enabled is False

    def test_base_job_defaults_to_no_mop(self):
        @dataclass
        class Custom(VacuumJob):
            job_id: str = "custom"
            robot: str = "saros"
            zones: list[int] = field(default_factory=lambda: [99])

        assert Custom().mop_enabled is False

    def test_litter_box_in_batch_does_not_trigger_a_mop(self):
        """Even bone dry and never mopped, the litter box must not go wet.

        Gate explicitly ENABLED so this proves job scoping, not the kill switch.
        """
        ctx = ctx_with(
            [make_meta(_LITTER_BOX, never_mopped=True)], scores={_LITTER_BOX: 99.0}
        )
        d = resolve_batch_mop(
            batch_of(_LITTER_BOX),
            ctx,
            job_map([_LITTER_BOX], Saros1FLitterBoxJob()),
            enabled_cfg(),
            _NOW,
        )
        assert d.mop is False
        assert d.reason == "off:no_zone_due"


# ── §7: settings wiring — dry_run is still env-sourced ───────────────────────
#
# Regression guard for ARIIA finding 1: CORTEX_VACUUMOPS_MOP_ENABLED was
# documented and the dataclass field existed, but nothing connected them —
# loop.py constructed VacuumOpsConfig(dry_run=...) only, so the switch was dead.
# The original tests missed it because they built VacuumOpsConfig directly.
# mop_enabled is no longer env-sourced at all (see §7b below for its
# replacement coverage) — dry_run is the one field remaining here.


_REQUIRED_ENV = {
    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
    "REDIS_URL": "redis://localhost:6379/0",
    "CORTEX_SECRET_KEY": "test-secret",
}


def _settings_with(monkeypatch, **overrides):
    from cortex_python.config.settings import Settings

    for key, value in {**_REQUIRED_ENV, **overrides}.items():
        monkeypatch.setenv(key, value)
    # Ignore any repo-local .env so the test reflects the environment only.
    return Settings(_env_file=None)  # type: ignore[call-arg]


class TestSettingsWiring:
    def test_dry_run_still_wired(self, monkeypatch):
        """Guard the pre-existing field against the same class of regression."""
        from cortex_python.modules.vacuumops.config import build_vacuumops_config

        settings = _settings_with(monkeypatch, CORTEX_VACUUMOPS_DRY_RUN="true")
        assert build_vacuumops_config(settings).dry_run is True

    def test_build_vacuumops_config_does_not_read_the_retired_env_var(
        self, monkeypatch
    ):
        """mop_enabled must NOT come back from Settings, even if a stale
        CORTEX_VACUUMOPS_MOP_ENABLED=true lingers in an old .env — that env var
        is retired (see config.py's mop_enabled field docstring). Settings'
        `extra = "ignore"` means it doesn't error either; build_vacuumops_config
        simply never reads it into the dataclass.
        """
        from cortex_python.modules.vacuumops.config import build_vacuumops_config

        settings = _settings_with(monkeypatch, CORTEX_VACUUMOPS_MOP_ENABLED="true")
        # The dataclass default (False) wins — nothing wires the stale env var in.
        assert build_vacuumops_config(settings).mop_enabled is False


# ── §7b: live, DB-backed mop_enabled — HomeOps read path + per-tick wiring ───
#
# Replaces the env-var regression guard above (commit history: ARIIA finding 1
# on CORTEX_VACUUMOPS_MOP_ENABLED). The kill switch is now a live setting read
# from HomeOps every loop tick — HomeOpsAdapter.get_vacuumops_mop_enabled() —
# and threaded into the tick's VacuumOpsConfig via `dataclasses.replace()` in
# loop.vacuumops_loop() rather than being fixed at process start. Two things
# need guarding against regressing silently, same as the original env var bug:
#   1. The adapter's fail-closed contract (7a) — every unreachable/malformed/
#      missing-field case must resolve to False, never raise, never default True.
#   2. The value actually reaching resolve_batch_mop's decision (7b) — mirrors
#      the old test_env_var_reaches_actual_mop_behaviour, but through the real
#      per-tick mechanism (dataclasses.replace) instead of Settings/env vars.


class _RaisingClient:
    """Fake httpx.AsyncClient whose .get() raises — simulates HomeOps
    unreachable (connection refused, timeout, DNS failure, etc.)."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str):
        raise self._exc


class _StatusErrorClient:
    """Fake httpx.AsyncClient whose .get() returns a response that raises on
    raise_for_status() — simulates a non-2xx (e.g. HomeOps 500, or 404 on an
    old HomeOps build predating this endpoint)."""

    class _Resp:
        def raise_for_status(self):
            import httpx

            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]

        def json(self):
            raise AssertionError("json() must not be reached after raise_for_status()")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str):
        return self._Resp()


def _adapter_get_returning(payload):
    """Build a HomeOpsAdapter whose GET returns `payload` verbatim as JSON.

    Reuses the _ZonesResponse/_ZonesClient shape already defined below in this
    file for get_zone_metadata's tests — same fake-client pattern, different
    call site.
    """
    from cortex_python.adapters.homeops_adapter import HomeOpsAdapter

    adapter = HomeOpsAdapter.__new__(HomeOpsAdapter)
    adapter._base_url = "http://homeops.test"  # type: ignore[attr-defined]
    adapter._api_key = "test"  # type: ignore[attr-defined]
    adapter._headers = {}  # type: ignore[attr-defined]
    adapter._client = lambda: _ZonesClient(payload)  # type: ignore[method-assign]
    return adapter


def _adapter_get_raising(exc: Exception):
    from cortex_python.adapters.homeops_adapter import HomeOpsAdapter

    adapter = HomeOpsAdapter.__new__(HomeOpsAdapter)
    adapter._base_url = "http://homeops.test"  # type: ignore[attr-defined]
    adapter._api_key = "test"  # type: ignore[attr-defined]
    adapter._headers = {}  # type: ignore[attr-defined]
    adapter._client = lambda: _RaisingClient(exc)  # type: ignore[method-assign]
    return adapter


def _adapter_get_bad_status():
    from cortex_python.adapters.homeops_adapter import HomeOpsAdapter

    adapter = HomeOpsAdapter.__new__(HomeOpsAdapter)
    adapter._base_url = "http://homeops.test"  # type: ignore[attr-defined]
    adapter._api_key = "test"  # type: ignore[attr-defined]
    adapter._headers = {}  # type: ignore[attr-defined]
    adapter._client = lambda: _StatusErrorClient()  # type: ignore[method-assign]
    return adapter


class TestGetVacuumopsMopEnabledFailClosed:
    """HomeOpsAdapter.get_vacuumops_mop_enabled() — every ambiguity -> False."""

    @pytest.mark.asyncio
    async def test_confirmed_true(self):
        adapter = _adapter_get_returning({"data": {"mop_enabled": True}})
        assert await adapter.get_vacuumops_mop_enabled() is True

    @pytest.mark.asyncio
    async def test_confirmed_false(self):
        adapter = _adapter_get_returning({"data": {"mop_enabled": False}})
        assert await adapter.get_vacuumops_mop_enabled() is False

    @pytest.mark.asyncio
    async def test_homeops_unreachable_fails_closed(self):
        """Connection error mid-tick must not raise (would take down the tick)
        and must not default to True."""
        adapter = _adapter_get_raising(ConnectionError("connection refused"))
        assert await adapter.get_vacuumops_mop_enabled() is False

    @pytest.mark.asyncio
    async def test_non_2xx_status_fails_closed(self):
        adapter = _adapter_get_bad_status()
        assert await adapter.get_vacuumops_mop_enabled() is False

    @pytest.mark.asyncio
    async def test_missing_data_key_fails_closed(self):
        adapter = _adapter_get_returning({"unexpected": "shape"})
        assert await adapter.get_vacuumops_mop_enabled() is False

    @pytest.mark.asyncio
    async def test_data_not_a_dict_fails_closed(self):
        adapter = _adapter_get_returning({"data": None})
        assert await adapter.get_vacuumops_mop_enabled() is False

    @pytest.mark.asyncio
    async def test_missing_mop_enabled_field_fails_closed(self):
        """Pre-migration HomeOps (endpoint exists but row/column doesn't) must
        not be misread as an implicit True."""
        adapter = _adapter_get_returning({"data": {}})
        assert await adapter.get_vacuumops_mop_enabled() is False

    @pytest.mark.asyncio
    async def test_non_bool_mop_enabled_fails_closed(self):
        """A malformed response (e.g. the string "true", or 1) must not be
        truthy-coerced — only a literal bool True is accepted."""
        adapter = _adapter_get_returning({"data": {"mop_enabled": "true"}})
        assert await adapter.get_vacuumops_mop_enabled() is False

    @pytest.mark.asyncio
    async def test_null_mop_enabled_fails_closed(self):
        adapter = _adapter_get_returning({"data": {"mop_enabled": None}})
        assert await adapter.get_vacuumops_mop_enabled() is False


class TestLivePerTickWiring:
    """The live value actually reaching resolve_batch_mop's decision.

    Mirrors loop.vacuumops_loop()'s real mechanism: a base VacuumOpsConfig
    (mop_enabled always False, per build_vacuumops_config()'s fallback) is
    replaced per-tick with the value read from HomeOps —
    `dataclasses.replace(vacuumops_cfg, mop_enabled=live_mop_enabled)` — not
    mutated, and not sourced from Settings/env at all.
    """

    def test_live_false_keeps_gate_off(self):
        base = VacuumOpsConfig()  # process-start default: mop_enabled=False
        tick_cfg = dataclasses.replace(base, mop_enabled=False)

        job = Saros1FRoomsJob()
        ctx = ctx_with([make_meta(_KITCHEN, last_mopped_days_ago=9.0)])
        decision = resolve_batch_mop(
            batch_of(_KITCHEN), ctx, job_map([_KITCHEN], job), tick_cfg, _NOW
        )
        assert decision.mop is False

    def test_live_true_enables_the_gate_this_tick(self):
        base = VacuumOpsConfig()
        tick_cfg = dataclasses.replace(base, mop_enabled=True)

        job = Saros1FRoomsJob()
        ctx = ctx_with([make_meta(_KITCHEN, last_mopped_days_ago=9.0)])
        decision = resolve_batch_mop(
            batch_of(_KITCHEN), ctx, job_map([_KITCHEN], job), tick_cfg, _NOW
        )
        assert decision.mop is True
        assert decision.intensity == "low"

    def test_toggling_between_ticks_is_not_sticky(self):
        """The mechanism is a fresh replace() every tick, not a cached/mutated
        object — flipping HomeOps's value must take effect on the very next
        resolve_batch_mop call, with no leftover state from the prior tick.
        This is the behavioural core of "quickly disable": there is no cache
        to invalidate, so there is nothing that can serve a stale value.
        """
        base = VacuumOpsConfig()
        job = Saros1FRoomsJob()
        ctx = ctx_with([make_meta(_KITCHEN, last_mopped_days_ago=9.0)])
        args = (batch_of(_KITCHEN), ctx, job_map([_KITCHEN], job))

        tick1 = dataclasses.replace(base, mop_enabled=True)
        assert resolve_batch_mop(*args, tick1, _NOW).mop is True

        tick2 = dataclasses.replace(base, mop_enabled=False)  # Carlos flipped it off
        assert resolve_batch_mop(*args, tick2, _NOW).mop is False

        tick3 = dataclasses.replace(base, mop_enabled=True)  # flipped back on
        assert resolve_batch_mop(*args, tick3, _NOW).mop is True

    def test_shadow_logging_survives_the_live_wiring_path_when_off(self):
        """Shadow mode (full reasoning, dispatch suppressed) must still work
        when mop_enabled arrives via the live/replace() path, not just when a
        VacuumOpsConfig is constructed directly with mop_enabled=False."""
        base = VacuumOpsConfig()
        tick_cfg = dataclasses.replace(base, mop_enabled=False)

        job = Saros1FRoomsJob()
        ctx = ctx_with([make_meta(_KITCHEN, last_mopped_days_ago=9.0)])
        decision = resolve_batch_mop(
            batch_of(_KITCHEN), ctx, job_map([_KITCHEN], job), tick_cfg, _NOW
        )
        assert decision.mop is False
        assert decision.reason.startswith("off:disabled(would:")
        assert "schedule:" in decision.reason  # real arm reasoning, not a stub

    def test_shadow_logging_survives_the_live_wiring_path_when_on(self):
        """Same reasoning content whether the gate is live or shadowed — only
        `mop` and whether the dispatch actually sends wet differ."""
        base = VacuumOpsConfig()

        job = Saros1FRoomsJob()
        ctx = ctx_with([make_meta(_KITCHEN, last_mopped_days_ago=9.0)])
        args = (batch_of(_KITCHEN), ctx, job_map([_KITCHEN], job))

        off_decision = resolve_batch_mop(
            *args, dataclasses.replace(base, mop_enabled=False), _NOW
        )
        on_decision = resolve_batch_mop(
            *args, dataclasses.replace(base, mop_enabled=True), _NOW
        )

        assert off_decision.mop is False
        assert on_decision.mop is True
        # Same triggering arm/zones either way — only the dispatch outcome differs.
        assert off_decision.triggering_zones == on_decision.triggering_zones
        assert "schedule:" in off_decision.reason
        assert "schedule:" in on_decision.reason


# ── §6: trigger_vacuum payload — the wiring the ticket was filed for ─────────


class _FakeResponse:
    def __init__(self) -> None:
        self.captured: dict = {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"ok": True}


class _FakeClient:
    def __init__(self, sink: dict) -> None:
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url: str, json: dict):
        self._sink["url"] = url
        self._sink["payload"] = json
        return _FakeResponse()


def _adapter_with_sink(sink: dict):
    from cortex_python.adapters.homeops_adapter import HomeOpsAdapter

    adapter = HomeOpsAdapter.__new__(HomeOpsAdapter)
    adapter._base_url = "http://homeops.test"  # type: ignore[attr-defined]
    adapter._api_key = "test"  # type: ignore[attr-defined]
    adapter._headers = {}  # type: ignore[attr-defined]
    adapter._client = lambda: _FakeClient(sink)  # type: ignore[method-assign]
    return adapter


class TestTriggerVacuumPayload:
    @pytest.mark.asyncio
    async def test_mop_fields_omitted_when_dry(self):
        """Regression guard for the original bug: the payload never carried mop.

        When dry we omit rather than send mop=false, so iRobot dispatches stay
        clean and HomeOps applies its own default.
        """
        sink: dict = {}
        adapter = _adapter_with_sink(sink)
        await adapter.trigger_vacuum(
            robot="saros", zones=[], trigger_metadata={}, dry_run=False
        )
        assert "mop" not in sink["payload"]
        assert "mop_intensity" not in sink["payload"]

    @pytest.mark.asyncio
    async def test_mop_fields_sent_when_wet(self):
        sink: dict = {}
        adapter = _adapter_with_sink(sink)
        await adapter.trigger_vacuum(
            robot="saros",
            zones=[],
            trigger_metadata={},
            dry_run=False,
            mop=True,
            mop_intensity="low",
        )
        assert sink["payload"]["mop"] is True
        assert sink["payload"]["mop_intensity"] == "low"

    @pytest.mark.asyncio
    async def test_mop_without_intensity_raises_before_http(self):
        """HomeOps 422s on mop=true with no intensity; fail with a clear message."""
        sink: dict = {}
        adapter = _adapter_with_sink(sink)
        with pytest.raises(ValueError, match="mop_intensity is required"):
            await adapter.trigger_vacuum(
                robot="saros", zones=[], trigger_metadata={}, dry_run=False, mop=True
            )
        assert sink == {}  # never reached the wire


# ── §8: adapter parsing of the feature-detection flag ────────────────────────
#
# The guard for ARIIA finding 2 is only as good as its default. An older HomeOps
# build omits mop_tracking_available entirely, and that absence must read as
# False — not as a truthy missing-key surprise.


class _ZonesResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _ZonesClient:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str):
        return _ZonesResponse(self._payload)


def _adapter_returning(payload: dict):
    from cortex_python.adapters.homeops_adapter import HomeOpsAdapter

    adapter = HomeOpsAdapter.__new__(HomeOpsAdapter)
    adapter._base_url = "http://homeops.test"  # type: ignore[attr-defined]
    adapter._api_key = "test"  # type: ignore[attr-defined]
    adapter._headers = {}  # type: ignore[attr-defined]
    adapter._client = lambda: _ZonesClient(payload)  # type: ignore[method-assign]
    return adapter


class TestZoneMetadataMopParsing:
    @pytest.mark.asyncio
    async def test_absent_flag_defaults_to_false(self):
        """Pre-migration HomeOps omits the field → must NOT be treated as tracked."""
        adapter = _adapter_returning(
            {"data": [{"id": _KITCHEN, "unit_id": 3, "floor_type": "tile"}]}
        )
        meta = await adapter.get_zone_metadata()
        assert meta[_KITCHEN].mop_tracking_available is False
        assert meta[_KITCHEN].last_mopped_at is None

    @pytest.mark.asyncio
    async def test_flag_and_timestamps_parsed_when_present(self):
        adapter = _adapter_returning(
            {
                "data": [
                    {
                        "id": _KITCHEN,
                        "unit_id": 3,
                        "floor_type": "tile",
                        "mop_tracking_available": True,
                        "last_mopped_at": "2026-08-01T12:00:00Z",
                        "mop_requested_at": None,
                    }
                ]
            }
        )
        meta = await adapter.get_zone_metadata()
        assert meta[_KITCHEN].mop_tracking_available is True
        assert meta[_KITCHEN].last_mopped_at is not None
        assert meta[_KITCHEN].last_mopped_at.year == 2026
        assert meta[_KITCHEN].mop_requested_at is None

    @pytest.mark.asyncio
    async def test_unparseable_timestamp_degrades_to_none(self):
        adapter = _adapter_returning(
            {
                "data": [
                    {
                        "id": _KITCHEN,
                        "unit_id": 3,
                        "mop_tracking_available": True,
                        "last_mopped_at": "not-a-timestamp",
                    }
                ]
            }
        )
        meta = await adapter.get_zone_metadata()
        assert meta[_KITCHEN].last_mopped_at is None
