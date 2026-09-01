"""Unit tests for VacuumOps batch assembly (assemble_batch).

Tests D10 + D11 logic:
  - Primary dispatch zones collected
  - Bundle threshold inclusion
  - Empty batch case

Zone IDs used:
  14 = Ethan 3F Litter Box (primary zone for litter_box_job)
  22 = Saros 1F Hallway (used in two-zone bundle tests)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cortex_python.modules.vacuumops.jobs import VacuumJob
from cortex_python.modules.vacuumops.loop import assemble_batch
from cortex_python.modules.vacuumops.schemas import ZoneMeta, ZoneOutcome
from tests.unit.vacuumops.conftest import make_occupancy, make_snapshot

_LITTER_BOX = 14  # Ethan 3F Litter Box (Ethan3FLitterBoxJob, robot="ethan")
_LOFT = 15        # Ethan 3F Loft (Ethan3FRoomsJob, robot="ethan") — used in bundle tests


def make_dispatch_outcome(
    zone: int, score: float = 75.0, tier: str = "R1"
) -> ZoneOutcome:
    return ZoneOutcome(
        zone=zone,
        action="dispatch",
        tier=tier,
        gate_failed="none",
        reason="all_rules_pass",
        score=score,
    )


def make_defer_outcome(
    zone: int,
    score: float = 30.0,
    gate_failed: str = "r0",
    reason: str = "score_below_threshold",
) -> ZoneOutcome:
    return ZoneOutcome(
        zone=zone,
        action="defer",
        tier="R0",
        gate_failed=gate_failed,
        reason=reason,
        score=score,
    )


# ── Empty batch ───────────────────────────────────────────────────────────────


def test_assemble_batch_empty_when_no_dispatch(litter_box_job):
    """No zones passed → empty batch → no dispatch."""
    ctx = make_snapshot()
    zone_outcomes = [make_defer_outcome(_LITTER_BOX)]
    batch = assemble_batch("ethan", zone_outcomes, ctx, [litter_box_job])
    assert batch == []


def test_assemble_batch_empty_when_all_fail(litter_box_job):
    """All zones fail hard → empty batch."""
    ctx = make_snapshot()
    zone_outcomes = [
        make_defer_outcome(_LITTER_BOX, score=10.0, gate_failed="r0"),
    ]
    batch = assemble_batch("ethan", zone_outcomes, ctx, [litter_box_job])
    assert batch == []


# ── Primary dispatch ──────────────────────────────────────────────────────────


def test_assemble_batch_single_primary_zone(litter_box_job):
    """Single zone independently PASSED → one non-bundled BatchEntry."""
    ctx = make_snapshot(litter_box_score=75.0)
    zone_outcomes = [make_dispatch_outcome(_LITTER_BOX, score=75.0)]
    batch = assemble_batch("ethan", zone_outcomes, ctx, [litter_box_job])

    assert len(batch) == 1
    entry = batch[0]
    assert entry.zone == _LITTER_BOX
    assert entry.bundled is False
    assert entry.score == 75.0


def test_assemble_batch_preserves_l1_confidence(litter_box_job):
    """L1-decided zone should carry l1_confidence through to batch."""
    ctx = make_snapshot(litter_box_score=75.0)
    outcome = ZoneOutcome(
        zone=_LITTER_BOX,
        action="dispatch",
        tier="L1",
        gate_failed="none",
        reason="l1_dispatch",
        score=75.0,
        l1_confidence=0.82,
    )
    batch = assemble_batch("ethan", [outcome], ctx, [litter_box_job])
    assert len(batch) == 1
    assert batch[0].l1_confidence == 0.82


# ── Bundle threshold (D11) ────────────────────────────────────────────────────


def test_assemble_batch_bundle_threshold_included():
    """Sub-threshold zone meeting bundle floor rides along with primary.

    Both zones belong to ethan-robot jobs so the bundle sweep's robot-match
    and _job_for_zone lookup succeed without patching ACTIVE_JOBS.
    Zone 14 = Litter Box (Ethan3FLitterBoxJob, primary dispatch).
    Zone 15 = Loft (Ethan3FRoomsJob, bundled at score=38 above bundle floor=35).
    """
    from dataclasses import dataclass, field

    @dataclass
    class TwoZoneJob(VacuumJob):
        job_id: str = "test_two_zone"
        robot: str = "ethan"
        zones: list = field(default_factory=lambda: [_LITTER_BOX, _LOFT])
        floor: str = "3F"
        noise_level: int = 1
        noise_radius: str = "floor"
        dispatch_threshold: float = 50.0
        bundle_threshold_pct: float = 0.70
        cleaning_params: dict = field(
            default_factory=lambda: {"passes": "auto", "intensity": "auto"}
        )

    job = TwoZoneJob()
    # Litter Box independently passes; Loft is at bundle floor (50 * 0.7 = 35)
    ctx = make_snapshot()
    ctx.zone_scores = {_LITTER_BOX: 75.0, _LOFT: 38.0}

    zone_outcomes = [
        make_dispatch_outcome(_LITTER_BOX, score=75.0),
        make_defer_outcome(
            _LOFT, score=38.0, gate_failed="r0", reason="score_below_threshold"
        ),
    ]

    batch = assemble_batch("ethan", zone_outcomes, ctx, [job])

    # Both zones should be in the batch
    zone_ids = {e.zone for e in batch}
    assert _LITTER_BOX in zone_ids
    assert _LOFT in zone_ids

    litter_entry = next(e for e in batch if e.zone == _LITTER_BOX)
    loft_entry = next(e for e in batch if e.zone == _LOFT)
    assert litter_entry.bundled is False
    assert loft_entry.bundled is True


def test_assemble_batch_bundle_below_floor_not_included():
    """Zone score below bundle floor → not bundled even if batch assembled."""
    from dataclasses import dataclass, field

    @dataclass
    class TwoZoneJob(VacuumJob):
        job_id: str = "test_bundle_floor"
        robot: str = "ethan"
        zones: list = field(default_factory=lambda: [_LITTER_BOX, _LOFT])
        floor: str = "3F"
        noise_level: int = 1
        noise_radius: str = "floor"
        dispatch_threshold: float = 50.0
        bundle_threshold_pct: float = 0.70
        cleaning_params: dict = field(
            default_factory=lambda: {"passes": "auto", "intensity": "auto"}
        )

    job = TwoZoneJob()
    ctx = make_snapshot()
    # Loft score = 20, bundle floor = 50 * 0.7 = 35 → below floor → NOT bundled
    ctx.zone_scores = {_LITTER_BOX: 75.0, _LOFT: 20.0}

    zone_outcomes = [
        make_dispatch_outcome(_LITTER_BOX, score=75.0),
        make_defer_outcome(
            _LOFT, score=20.0, gate_failed="r0", reason="score_below_threshold"
        ),
    ]

    batch = assemble_batch("ethan", zone_outcomes, ctx, [job])
    zone_ids = {e.zone for e in batch}
    assert _LITTER_BOX in zone_ids
    assert _LOFT not in zone_ids


def test_assemble_batch_hard_failed_zone_not_bundled():
    """Zone that hard-failed (effectiveness) is never bundled, even above bundle floor."""
    from dataclasses import dataclass, field

    @dataclass
    class TwoZoneJob(VacuumJob):
        job_id: str = "test_no_bundle_hard_fail"
        robot: str = "ethan"
        zones: list = field(default_factory=lambda: [_LITTER_BOX, _LOFT])
        floor: str = "3F"
        noise_level: int = 1
        noise_radius: str = "floor"
        dispatch_threshold: float = 50.0
        bundle_threshold_pct: float = 0.70
        cleaning_params: dict = field(
            default_factory=lambda: {"passes": "auto", "intensity": "auto"}
        )

    job = TwoZoneJob()
    ctx = make_snapshot()
    ctx.zone_scores = {_LITTER_BOX: 75.0, _LOFT: 40.0}

    zone_outcomes = [
        make_dispatch_outcome(_LITTER_BOX, score=75.0),
        make_defer_outcome(
            _LOFT,
            score=40.0,
            gate_failed="effectiveness",
            reason=f"zone_occupied:{_LOFT}",
        ),
    ]

    batch = assemble_batch("ethan", zone_outcomes, ctx, [job])
    zone_ids = {e.zone for e in batch}
    assert _LITTER_BOX in zone_ids
    # Loft hard-failed effectiveness — must NOT be bundled
    assert _LOFT not in zone_ids


# ── Bundle sweep honours the occupancy resolution chain (ARIIA finding 2) ─────
#
# _zone_effective_simple threads ctx.zone_metadata into zone_active_use_check so
# a bundled zone is not held to a weaker effectiveness standard than a primary —
# a bundled zone bypasses L1 entirely. Every other bundle test uses zones with no
# ctx.zone_metadata entry, so zone_meta resolves to None identically with or
# without that wiring and a regression would go undetected. These two tests are
# the same scenario with and without the metadata, so they can only both pass
# when tier 1 is genuinely being consulted.

_NOW = datetime(2026, 5, 24, 15, 0, 0, tzinfo=timezone.utc)
# Loft's designated entity: no suffix rule maps it back to the "loft" room key,
# so it is only ever read via ZoneMeta.occupancy_sensor → ctx.occupancy_readings.
_LOFT_SENSOR = "binary_sensor.emotion_loft_seating_presence"


def _bundle_job() -> VacuumJob:
    from dataclasses import dataclass, field

    @dataclass
    class TwoZoneJob(VacuumJob):
        job_id: str = "test_bundle_occupancy"
        robot: str = "ethan"
        zones: list = field(default_factory=lambda: [_LITTER_BOX, _LOFT])
        floor: str = "3F"
        noise_level: int = 1
        noise_radius: str = "floor"
        dispatch_threshold: float = 50.0
        bundle_threshold_pct: float = 0.70
        cleaning_params: dict = field(
            default_factory=lambda: {"passes": "auto", "intensity": "auto"}
        )

    return TwoZoneJob()


def _bundle_ctx():
    """Loft eligible for bundling; its room sensor and the 3F rollup both clear.

    Every coarser tier reads clear, so the designated sensor is the ONLY signal
    that can change the outcome.
    """
    ctx = make_snapshot(timestamp=_NOW)
    ctx.zone_scores = {_LITTER_BOX: 75.0, _LOFT: 38.0}
    ctx.floor_occupancy["3F"] = make_occupancy(
        "binary_sensor.third_floor_occupancy_status",
        occupied=False,
        last_changed=_NOW - timedelta(seconds=3600),
    )
    return ctx


def _bundle_outcomes():
    return [
        make_dispatch_outcome(_LITTER_BOX, score=75.0),
        make_defer_outcome(_LOFT, score=38.0, gate_failed="r0", reason="score_below_threshold"),
    ]


def test_bundle_sweep_excludes_zone_whose_designated_sensor_is_occupied():
    """Tier 1 must reach the bundle sweep: an occupied designated sensor excludes.

    Without zone_metadata threaded into _zone_effective_simple this zone bundles
    (room sensor and 3F rollup both read clear) and the robot is dispatched into
    an occupied Loft with no L1 call in between.
    """
    ctx = _bundle_ctx()
    ctx.zone_metadata[_LOFT] = ZoneMeta(zone_id=_LOFT, unit_id=2, occupancy_sensor=_LOFT_SENSOR)
    ctx.occupancy_readings[_LOFT_SENSOR] = make_occupancy(
        _LOFT_SENSOR, occupied=True, last_changed=_NOW - timedelta(seconds=60)
    )

    batch = assemble_batch("ethan", _bundle_outcomes(), ctx, [_bundle_job()])
    zone_ids = {e.zone for e in batch}
    assert _LITTER_BOX in zone_ids
    assert _LOFT not in zone_ids


def test_bundle_sweep_control_same_scenario_without_metadata_still_bundles():
    """The control half: with no zone_metadata entry the coarser tiers pass.

    This is what the occupied-sensor case above degraded to before the wiring
    fix, and it pins that the exclusion there comes from tier 1 specifically
    rather than from anything else in the snapshot.
    """
    ctx = _bundle_ctx()
    ctx.occupancy_readings[_LOFT_SENSOR] = make_occupancy(
        _LOFT_SENSOR, occupied=True, last_changed=_NOW - timedelta(seconds=60)
    )
    # No ctx.zone_metadata[_LOFT] → nothing points the gate at the sensor above.

    batch = assemble_batch("ethan", _bundle_outcomes(), ctx, [_bundle_job()])
    zone_ids = {e.zone for e in batch}
    assert _LITTER_BOX in zone_ids
    assert _LOFT in zone_ids


def test_bundle_sweep_includes_zone_whose_designated_sensor_is_settled_clear():
    """The gate must not wedge the sweep shut: a confirmed-clear sensor bundles."""
    ctx = _bundle_ctx()
    ctx.zone_metadata[_LOFT] = ZoneMeta(zone_id=_LOFT, unit_id=2, occupancy_sensor=_LOFT_SENSOR)
    ctx.occupancy_readings[_LOFT_SENSOR] = make_occupancy(
        _LOFT_SENSOR, occupied=False, last_changed=_NOW - timedelta(seconds=1800)
    )

    batch = assemble_batch("ethan", _bundle_outcomes(), ctx, [_bundle_job()])
    zone_ids = {e.zone for e in batch}
    assert _LITTER_BOX in zone_ids
    assert _LOFT in zone_ids


def test_bundle_sweep_excludes_zone_whose_designated_sensor_just_flipped_off():
    """The confirmation window reaches the bundle sweep too, not just the main gate."""
    ctx = _bundle_ctx()
    ctx.zone_metadata[_LOFT] = ZoneMeta(zone_id=_LOFT, unit_id=2, occupancy_sensor=_LOFT_SENSOR)
    ctx.occupancy_readings[_LOFT_SENSOR] = make_occupancy(
        _LOFT_SENSOR, occupied=False, last_changed=_NOW - timedelta(seconds=10)
    )

    batch = assemble_batch("ethan", _bundle_outcomes(), ctx, [_bundle_job()])
    zone_ids = {e.zone for e in batch}
    assert _LITTER_BOX in zone_ids
    assert _LOFT not in zone_ids
