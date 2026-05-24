"""Unit tests for VacuumOps batch assembly (assemble_batch).

Tests D10 + D11 logic:
  - Primary dispatch zones collected
  - Bundle threshold inclusion
  - Empty batch case
"""

from __future__ import annotations

import pytest

from cortex_python.modules.vacuumops.jobs import LitterBoxJob, VacuumJob
from cortex_python.modules.vacuumops.loop import assemble_batch
from cortex_python.modules.vacuumops.schemas import ZoneOutcome
from tests.unit.vacuumops.conftest import make_snapshot


def make_dispatch_outcome(zone: str, score: float = 75.0, tier: str = "R1") -> ZoneOutcome:
    return ZoneOutcome(
        zone=zone,
        action="dispatch",
        tier=tier,
        gate_failed="none",
        reason="all_rules_pass",
        score=score,
    )


def make_defer_outcome(
    zone: str,
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
    zone_outcomes = [make_defer_outcome("Litter Box")]
    batch = assemble_batch("ethan", zone_outcomes, ctx, [litter_box_job])
    assert batch == []


def test_assemble_batch_empty_when_all_fail(litter_box_job):
    """All zones fail hard → empty batch."""
    ctx = make_snapshot()
    zone_outcomes = [
        make_defer_outcome("Litter Box", score=10.0, gate_failed="r0"),
    ]
    batch = assemble_batch("ethan", zone_outcomes, ctx, [litter_box_job])
    assert batch == []


# ── Primary dispatch ──────────────────────────────────────────────────────────


def test_assemble_batch_single_primary_zone(litter_box_job):
    """Single zone independently PASSED → one non-bundled BatchEntry."""
    ctx = make_snapshot(litter_box_score=75.0)
    zone_outcomes = [make_dispatch_outcome("Litter Box", score=75.0)]
    batch = assemble_batch("ethan", zone_outcomes, ctx, [litter_box_job])

    assert len(batch) == 1
    entry = batch[0]
    assert entry.zone == "Litter Box"
    assert entry.bundled is False
    assert entry.score == 75.0


def test_assemble_batch_preserves_l1_confidence(litter_box_job):
    """L1-decided zone should carry l1_confidence through to batch."""
    ctx = make_snapshot(litter_box_score=75.0)
    outcome = ZoneOutcome(
        zone="Litter Box",
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

    Phase 1 only has LitterBoxJob with one zone, so bundle logic is
    tested with a synthetic second zone on the same robot.
    We create a custom job with two zones to exercise this path.
    """
    from dataclasses import dataclass, field

    @dataclass
    class TwoZoneJob(VacuumJob):
        job_id: str = "test_two_zone"
        robot: str = "ethan"
        zones: list = field(default_factory=lambda: ["Litter Box", "Hallway"])
        floor: str = "1F"
        noise_level: int = 1
        noise_radius: str = "floor"
        dispatch_threshold: float = 50.0
        bundle_threshold_pct: float = 0.70
        cleaning_params: dict = field(default_factory=lambda: {"passes": "auto", "intensity": "auto"})

    job = TwoZoneJob()
    # Litter Box independently passes; Hallway is at bundle floor (50 * 0.7 = 35)
    ctx = make_snapshot()
    ctx.zone_scores = {"Litter Box": 75.0, "Hallway": 38.0}

    zone_outcomes = [
        make_dispatch_outcome("Litter Box", score=75.0),
        make_defer_outcome("Hallway", score=38.0, gate_failed="r0", reason="score_below_threshold"),
    ]

    batch = assemble_batch("ethan", zone_outcomes, ctx, [job])

    # Both zones should be in the batch
    zone_labels = {e.zone for e in batch}
    assert "Litter Box" in zone_labels
    assert "Hallway" in zone_labels

    litter_entry = next(e for e in batch if e.zone == "Litter Box")
    hallway_entry = next(e for e in batch if e.zone == "Hallway")
    assert litter_entry.bundled is False
    assert hallway_entry.bundled is True


def test_assemble_batch_bundle_below_floor_not_included():
    """Zone score below bundle floor → not bundled even if batch assembled."""
    from dataclasses import dataclass, field

    @dataclass
    class TwoZoneJob(VacuumJob):
        job_id: str = "test_bundle_floor"
        robot: str = "ethan"
        zones: list = field(default_factory=lambda: ["Litter Box", "Hallway"])
        floor: str = "1F"
        noise_level: int = 1
        noise_radius: str = "floor"
        dispatch_threshold: float = 50.0
        bundle_threshold_pct: float = 0.70
        cleaning_params: dict = field(default_factory=lambda: {"passes": "auto", "intensity": "auto"})

    job = TwoZoneJob()
    ctx = make_snapshot()
    # Hallway score = 20, floor = 50 * 0.7 = 35 → below floor → NOT bundled
    ctx.zone_scores = {"Litter Box": 75.0, "Hallway": 20.0}

    zone_outcomes = [
        make_dispatch_outcome("Litter Box", score=75.0),
        make_defer_outcome("Hallway", score=20.0, gate_failed="r0", reason="score_below_threshold"),
    ]

    batch = assemble_batch("ethan", zone_outcomes, ctx, [job])
    zone_labels = {e.zone for e in batch}
    assert "Litter Box" in zone_labels
    assert "Hallway" not in zone_labels


def test_assemble_batch_hard_failed_zone_not_bundled():
    """Zone that hard-failed (effectiveness) is never bundled, even above bundle floor."""
    from dataclasses import dataclass, field

    @dataclass
    class TwoZoneJob(VacuumJob):
        job_id: str = "test_no_bundle_hard_fail"
        robot: str = "ethan"
        zones: list = field(default_factory=lambda: ["Litter Box", "Hallway"])
        floor: str = "1F"
        noise_level: int = 1
        noise_radius: str = "floor"
        dispatch_threshold: float = 50.0
        bundle_threshold_pct: float = 0.70
        cleaning_params: dict = field(default_factory=lambda: {"passes": "auto", "intensity": "auto"})

    job = TwoZoneJob()
    ctx = make_snapshot()
    ctx.zone_scores = {"Litter Box": 75.0, "Hallway": 40.0}

    zone_outcomes = [
        make_dispatch_outcome("Litter Box", score=75.0),
        make_defer_outcome(
            "Hallway", score=40.0, gate_failed="effectiveness", reason="zone_occupied:Hallway"
        ),
    ]

    batch = assemble_batch("ethan", zone_outcomes, ctx, [job])
    zone_labels = {e.zone for e in batch}
    assert "Litter Box" in zone_labels
    # Hallway hard-failed effectiveness — must NOT be bundled
    assert "Hallway" not in zone_labels
