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
) -> ZoneMeta:
    """Build a ZoneMeta with mop state relative to _NOW."""
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
    )


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
        ctx = ctx_with([make_meta(_KITCHEN, last_mopped_days_ago=1.0, mop_requested=True)])
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
        ctx = ctx_with([make_meta(_KITCHEN, last_mopped_days_ago=6.9)], scores={_KITCHEN: 10.0})
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
        ctx = ctx_with([make_meta(_KITCHEN, last_mopped_days_ago=2.0)], scores={_KITCHEN: 85.0})
        need = evaluate_mop_need(job, _KITCHEN, ctx, _NOW)
        assert need.needed is True
        assert need.arm == "score"
        assert need.reason == "score:85"

    def test_score_arm_ignores_merely_dispatch_eligible_zone(self):
        """dispatch_threshold is 50; a zone at 60 is vacuum-eligible but not mop-eligible."""
        job = Saros1FRoomsJob()
        ctx = ctx_with([make_meta(_KITCHEN, last_mopped_days_ago=1.0)], scores={_KITCHEN: 60.0})
        need = evaluate_mop_need(job, _KITCHEN, ctx, _NOW)
        assert need.needed is False

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
        d = resolve_batch_mop([], ctx, {}, VacuumOpsConfig(), _NOW)
        assert d.mop is False
        assert d.reason == "off:empty_batch"

    def test_module_kill_switch_forces_off(self):
        """Gate still evaluates, but resolves to dry so the trail can be reviewed."""
        job = Saros1FRoomsJob()
        ctx = ctx_with([make_meta(_KITCHEN, never_mopped=True)])
        cfg = VacuumOpsConfig(mop_enabled=False)
        d = resolve_batch_mop(batch_of(_KITCHEN), ctx, job_map([_KITCHEN], job), cfg, _NOW)
        assert d.mop is False
        assert d.reason == "off:module_disabled"

    def test_no_zone_due_is_off(self):
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [make_meta(_KITCHEN, last_mopped_days_ago=1.0)],
            scores={_KITCHEN: 20.0},
        )
        d = resolve_batch_mop(batch_of(_KITCHEN), ctx, job_map([_KITCHEN], job), VacuumOpsConfig(), _NOW)
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
        d = resolve_batch_mop(batch_of(*zones), ctx, job_map(zones, job), VacuumOpsConfig(), _NOW)
        assert d.mop is True
        # Only the genuinely-due zone is credited as triggering; the rest ride along.
        assert d.triggering_zones == [_KITCHEN]

    def test_arm_precedence_reported_across_zones(self):
        """signal > schedule > score for *reporting*; any arm still causes the mop."""
        job = Saros1FRoomsJob()
        ctx = ctx_with(
            [
                make_meta(_KITCHEN, last_mopped_days_ago=9.0),  # schedule
                make_meta(_LIVING_ROOM, last_mopped_days_ago=1.0, mop_requested=True),  # signal
            ],
            scores={_KITCHEN: 60.0, _LIVING_ROOM: 60.0},
        )
        zones = [_KITCHEN, _LIVING_ROOM]
        d = resolve_batch_mop(batch_of(*zones), ctx, job_map(zones, job), VacuumOpsConfig(), _NOW)
        assert d.mop is True
        assert d.reason.startswith("signal")
        assert set(d.triggering_zones) == {_KITCHEN, _LIVING_ROOM}

    def test_zone_without_a_job_mapping_is_skipped_not_fatal(self):
        job = Saros1FRoomsJob()
        ctx = ctx_with([make_meta(_KITCHEN, never_mopped=True)])
        # _LIVING_ROOM deliberately absent from the job map
        d = resolve_batch_mop(
            batch_of(_KITCHEN, _LIVING_ROOM), ctx, job_map([_KITCHEN], job), VacuumOpsConfig(), _NOW
        )
        assert d.mop is True


# ── §4: intensity mapping + floor-type safety cap ────────────────────────────


class TestIntensityMapping:
    def test_light_maps_to_low(self):
        job = Saros1FRoomsJob()
        ctx = ctx_with([make_meta(_KITCHEN, last_mopped_days_ago=8.0)])  # due, not deep
        d = resolve_batch_mop(batch_of(_KITCHEN), ctx, job_map([_KITCHEN], job), VacuumOpsConfig(), _NOW)
        assert d.intensity == "low"

    def test_deep_maps_to_high(self):
        job = Saros1FRoomsJob()
        ctx = ctx_with([make_meta(_KITCHEN, last_mopped_days_ago=20.0)])  # deep
        d = resolve_batch_mop(batch_of(_KITCHEN), ctx, job_map([_KITCHEN], job), VacuumOpsConfig(), _NOW)
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
                make_meta(_KITCHEN, last_mopped_days_ago=30.0, floor_type="tile"),  # deep
                make_meta(_LIVING_ROOM, last_mopped_days_ago=1.0, floor_type="hardwood"),
            ],
            scores={_KITCHEN: 60.0, _LIVING_ROOM: 60.0},
        )
        zones = [_KITCHEN, _LIVING_ROOM]
        d = resolve_batch_mop(batch_of(*zones), ctx, job_map(zones, job), VacuumOpsConfig(), _NOW)
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
        d = resolve_batch_mop(batch_of(*zones), ctx, job_map(zones, job), VacuumOpsConfig(), _NOW)
        assert d.triggering_zones == [_KITCHEN]
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
        d = resolve_batch_mop(batch_of(*zones), ctx, job_map(zones, job), VacuumOpsConfig(), _NOW)
        assert d.intensity == "high"
        assert "capped_light" not in d.reason

    def test_mapping_is_configurable(self):
        job = Saros1FRoomsJob()
        cfg = VacuumOpsConfig(mop_intensity_light="medium", mop_intensity_deep="extreme")
        ctx = ctx_with([make_meta(_KITCHEN, last_mopped_days_ago=8.0)])
        d = resolve_batch_mop(batch_of(_KITCHEN), ctx, job_map([_KITCHEN], job), cfg, _NOW)
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
        """Even bone dry and never mopped, the litter box must not go wet."""
        ctx = ctx_with([make_meta(_LITTER_BOX, never_mopped=True)], scores={_LITTER_BOX: 99.0})
        d = resolve_batch_mop(
            batch_of(_LITTER_BOX),
            ctx,
            job_map([_LITTER_BOX], Saros1FLitterBoxJob()),
            VacuumOpsConfig(),
            _NOW,
        )
        assert d.mop is False
        assert d.reason == "off:no_zone_due"


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
