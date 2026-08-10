"""Unit tests for VacuumOps L1 module.

Covers:
  - resolve_params: three resolution paths (l1, mixed, default)
  - assemble_batch: l1_results integration (params_source propagation)
"""

from __future__ import annotations


from cortex_python.modules.vacuumops.jobs import Ethan3FLitterBoxJob
from cortex_python.modules.vacuumops.l1 import L1Decision, resolve_params
from cortex_python.modules.vacuumops.loop import assemble_batch
from cortex_python.modules.vacuumops.schemas import ZoneOutcome
from tests.unit.vacuumops.conftest import make_snapshot


# ── resolve_params ─────────────────────────────────────────────────────────────


def test_resolve_params_all_l1():
    """L1 returns both passes + intensity → source='l1'."""
    job = Ethan3FLitterBoxJob()
    l1 = L1Decision(
        decision="dispatch",
        confidence=0.9,
        reason="zone clear, heavy litter load",
        passes="two",
        intensity="perf",
        params_reason="Two heavy Oliver visits — double pass + high suction warranted",
    )
    passes, intensity, src = resolve_params(job, l1)
    assert passes == "two"
    assert intensity == "perf"
    assert src == "l1"


def test_resolve_params_mixed():
    """L1 returns only passes (intensity=None) → source='mixed', intensity falls back to job default."""
    job = Ethan3FLitterBoxJob()
    l1 = L1Decision(
        decision="dispatch",
        confidence=0.75,
        reason="moderate dirtiness, floor type clear",
        passes="one",
        intensity=None,
        params_reason="Light load — single pass sufficient",
    )
    passes, intensity, src = resolve_params(job, l1)
    assert passes == "one"
    # intensity falls back to job.cleaning_params default ("auto")
    assert intensity == job.cleaning_params.get("intensity", "auto")
    assert src == "mixed"


def test_resolve_params_default():
    """l1=None → both fall back to job defaults, source='default'."""
    job = Ethan3FLitterBoxJob()
    passes, intensity, src = resolve_params(job, None)
    assert passes == job.cleaning_params.get("passes", "auto")
    assert intensity == job.cleaning_params.get("intensity", "auto")
    assert src == "default"


# ── assemble_batch with l1_results ────────────────────────────────────────────


_LITTER_BOX = 14  # Ethan 3F Litter Box (Ethan3FLitterBoxJob, robot="ethan")
_LOFT = 15  # Ethan 3F Loft (Ethan3FRoomsJob, robot="ethan") — used in bundle tests


def test_assemble_batch_uses_l1_results():
    """Candidate with l1_results entry → BatchEntry.params_source='l1', params_reason set."""
    job = Ethan3FLitterBoxJob()
    ctx = make_snapshot(litter_box_score=75.0)

    # L1-decided outcome — use integer zone_id
    outcome = ZoneOutcome(
        zone=_LITTER_BOX,
        action="dispatch",
        tier="L1",
        gate_failed="none",
        reason="zone clear, heavy litter",
        score=75.0,
        l1_confidence=0.88,
    )

    l1_decision = L1Decision(
        decision="dispatch",
        confidence=0.88,
        reason="zone clear, heavy litter",
        passes="two",
        intensity="perf",
        params_reason="Heavy Oliver deposit — double pass + high suction",
    )
    l1_results = {(job.job_id, _LITTER_BOX): l1_decision}

    batch = assemble_batch("ethan", [outcome], ctx, [job], l1_results=l1_results)

    assert len(batch) == 1
    entry = batch[0]
    assert entry.params_source == "l1"
    assert entry.passes == "two"
    assert entry.intensity == "perf"
    assert entry.params_reason == "Heavy Oliver deposit — double pass + high suction"
    assert entry.bundled is False


def test_assemble_batch_bundled_uses_default():
    """Bundled candidate → BatchEntry.params_source='default' regardless of l1_results.

    Uses zone 14 (Litter Box, primary) and zone 15 (Loft, bundled) — both owned
    by ethan-robot jobs so _job_for_zone resolves correctly without patching ACTIVE_JOBS.
    """
    from dataclasses import dataclass, field

    @dataclass
    class TwoZoneJob(Ethan3FLitterBoxJob):
        job_id: str = "two_zone_test"
        zones: list = field(default_factory=lambda: [_LITTER_BOX, _LOFT])

    job = TwoZoneJob()
    ctx = make_snapshot()
    ctx.zone_scores = {_LITTER_BOX: 75.0, _LOFT: 38.0}

    # Litter Box independently passed; Loft is below threshold but above bundle floor
    zone_outcomes = [
        ZoneOutcome(
            zone=_LITTER_BOX,
            action="dispatch",
            tier="R1",
            gate_failed="none",
            reason="all_rules_pass",
            score=75.0,
        ),
        ZoneOutcome(
            zone=_LOFT,
            action="defer",
            tier="R0",
            gate_failed="r0",
            reason="score_below_threshold",
            score=38.0,
        ),
    ]

    # Even if an L1 result existed for Loft, bundled zones always use defaults
    l1_decision = L1Decision(
        decision="dispatch",
        confidence=0.9,
        reason="clear loft",
        passes="two",
        intensity="perf",
        params_reason="Should be ignored for bundled zone",
    )
    l1_results = {(job.job_id, _LOFT): l1_decision}

    batch = assemble_batch("ethan", zone_outcomes, ctx, [job], l1_results=l1_results)

    loft_entries = [e for e in batch if e.zone == _LOFT]
    assert len(loft_entries) == 1
    loft = loft_entries[0]
    assert loft.bundled is True
    assert loft.params_source == "default"
    assert loft.passes == job.cleaning_params.get("passes", "auto")
    assert loft.intensity == job.cleaning_params.get("intensity", "auto")
