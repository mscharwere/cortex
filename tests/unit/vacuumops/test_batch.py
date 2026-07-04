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


from cortex_python.modules.vacuumops.jobs import VacuumJob
from cortex_python.modules.vacuumops.loop import assemble_batch
from cortex_python.modules.vacuumops.schemas import ZoneOutcome
from tests.unit.vacuumops.conftest import make_snapshot

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
