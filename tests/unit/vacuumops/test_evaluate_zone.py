"""Unit tests for evaluate_zone L1 paths.

Covers:
  - Low confidence: l1_results populated even when decision deferred due to
    confidence below vacuumops_cfg.l1_overflow_confidence
  - Defer decision: l1_results populated when L1 returns decision="defer"
  - Dispatch decision: l1_results populated when L1 returns decision="dispatch"
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from cortex_python.modules.vacuumops.config import VacuumOpsConfig
from cortex_python.modules.vacuumops.l1 import L1Decision
from cortex_python.modules.vacuumops.loop import evaluate_zone
from tests.unit.vacuumops.conftest import make_snapshot

# Zone ID 14 = Ethan 3F Litter Box (wired in conftest make_snapshot)
_LITTER_BOX_ZONE_ID = 14


@pytest.fixture
def vacuumops_cfg() -> VacuumOpsConfig:
    """Default VacuumOpsConfig with l1_overflow_confidence=0.65."""
    return VacuumOpsConfig()


@pytest.fixture
def mock_settings() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_litellm_client() -> MagicMock:
    return MagicMock()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_l1_decision(decision: str, confidence: float) -> L1Decision:
    return L1Decision(
        decision=decision,
        confidence=confidence,
        reason="test reason",
        passes="auto",
        intensity="auto",
        params_reason=None,
    )


# ── L1 low-confidence path ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_zone_l1_low_confidence_populates_l1_results(
    litter_box_job,
    mock_redis,
    vacuumops_cfg,
    mock_settings,
    mock_litellm_client,
):
    """When L1 returns confidence below threshold, l1_results IS populated.

    The zone outcome is defer (low confidence) but the L1Decision object must
    still be recorded so persist_decision can surface l1_decision / l1_reason
    in the zone detail log.
    """
    ctx = make_snapshot(litter_box_score=75.0)
    l1_results: dict = {}

    low_conf_decision = _make_l1_decision("dispatch", confidence=0.50)
    # 0.50 < default l1_overflow_confidence (0.65) → should defer + populate l1_results

    with (
        patch(
            "cortex_python.modules.vacuumops.loop.run_r0",
            new=AsyncMock(return_value=(True, "r0_pass")),
        ),
        patch(
            "cortex_python.modules.vacuumops.loop.run_r1",
            new=AsyncMock(return_value=("AMBIGUOUS", None, "ambiguous")),
        ),
        patch(
            "cortex_python.modules.vacuumops.loop.run_l1",
            new=AsyncMock(return_value=low_conf_decision),
        ),
        patch(
            "cortex_python.modules.vacuumops.loop._load_prompt_template",
            return_value="template",
        ),
        patch(
            "cortex_python.modules.vacuumops.loop.render_patterns_for",
            return_value="",
        ),
    ):
        outcome = await evaluate_zone(
            job=litter_box_job,
            zone=_LITTER_BOX_ZONE_ID,
            ctx=ctx,
            redis_client=mock_redis,
            settings=mock_settings,
            litellm_client=mock_litellm_client,
            vacuumops_cfg=vacuumops_cfg,
            patterns=[],
            l1_results=l1_results,
        )

    # Outcome must be defer due to low confidence
    assert outcome.action == "defer"
    assert outcome.gate_failed == "l1"
    assert outcome.tier == "L1"

    # l1_results MUST be populated even though outcome is defer
    key = (litter_box_job.job_id, _LITTER_BOX_ZONE_ID)
    assert key in l1_results, "l1_results must be populated on low-confidence path"
    assert l1_results[key] is low_conf_decision


# ── L1 defer decision path ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_zone_l1_defer_decision_populates_l1_results(
    litter_box_job,
    mock_redis,
    vacuumops_cfg,
    mock_settings,
    mock_litellm_client,
):
    """When L1 returns decision='defer', l1_results IS populated.

    The zone outcome is defer, but the L1Decision must be recorded so
    the defer reason and params are surfaced in the decision log.
    """
    ctx = make_snapshot(litter_box_score=75.0)
    l1_results: dict = {}

    defer_decision = _make_l1_decision("defer", confidence=0.80)
    # confidence above threshold but decision is 'defer' → should defer + populate l1_results

    with (
        patch(
            "cortex_python.modules.vacuumops.loop.run_r0",
            new=AsyncMock(return_value=(True, "r0_pass")),
        ),
        patch(
            "cortex_python.modules.vacuumops.loop.run_r1",
            new=AsyncMock(return_value=("AMBIGUOUS", None, "ambiguous")),
        ),
        patch(
            "cortex_python.modules.vacuumops.loop.run_l1",
            new=AsyncMock(return_value=defer_decision),
        ),
        patch(
            "cortex_python.modules.vacuumops.loop._load_prompt_template",
            return_value="template",
        ),
        patch(
            "cortex_python.modules.vacuumops.loop.render_patterns_for",
            return_value="",
        ),
    ):
        outcome = await evaluate_zone(
            job=litter_box_job,
            zone=_LITTER_BOX_ZONE_ID,
            ctx=ctx,
            redis_client=mock_redis,
            settings=mock_settings,
            litellm_client=mock_litellm_client,
            vacuumops_cfg=vacuumops_cfg,
            patterns=[],
            l1_results=l1_results,
        )

    # Outcome must be defer — L1 said defer
    assert outcome.action == "defer"
    assert outcome.gate_failed == "comfort"
    assert outcome.tier == "L1"

    # l1_results MUST be populated
    key = (litter_box_job.job_id, _LITTER_BOX_ZONE_ID)
    assert key in l1_results, "l1_results must be populated on L1-defer path"
    assert l1_results[key] is defer_decision


# ── L1 dispatch decision path ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_zone_l1_dispatch_populates_l1_results(
    litter_box_job,
    mock_redis,
    vacuumops_cfg,
    mock_settings,
    mock_litellm_client,
):
    """When L1 returns decision='dispatch', l1_results IS populated.

    This is the happy path — the zone is cleared for dispatch by L1.
    l1_results must be populated so assemble_batch can resolve cleaning params.
    """
    ctx = make_snapshot(litter_box_score=75.0)
    l1_results: dict = {}

    dispatch_decision = _make_l1_decision("dispatch", confidence=0.90)

    with (
        patch(
            "cortex_python.modules.vacuumops.loop.run_r0",
            new=AsyncMock(return_value=(True, "r0_pass")),
        ),
        patch(
            "cortex_python.modules.vacuumops.loop.run_r1",
            new=AsyncMock(return_value=("AMBIGUOUS", None, "ambiguous")),
        ),
        patch(
            "cortex_python.modules.vacuumops.loop.run_l1",
            new=AsyncMock(return_value=dispatch_decision),
        ),
        patch(
            "cortex_python.modules.vacuumops.loop._load_prompt_template",
            return_value="template",
        ),
        patch(
            "cortex_python.modules.vacuumops.loop.render_patterns_for",
            return_value="",
        ),
    ):
        outcome = await evaluate_zone(
            job=litter_box_job,
            zone=_LITTER_BOX_ZONE_ID,
            ctx=ctx,
            redis_client=mock_redis,
            settings=mock_settings,
            litellm_client=mock_litellm_client,
            vacuumops_cfg=vacuumops_cfg,
            patterns=[],
            l1_results=l1_results,
        )

    # Outcome must be dispatch
    assert outcome.action == "dispatch"
    assert outcome.tier == "L1"
    assert outcome.l1_confidence == 0.90

    # l1_results MUST be populated
    key = (litter_box_job.job_id, _LITTER_BOX_ZONE_ID)
    assert key in l1_results, "l1_results must be populated on L1-dispatch path"
    assert l1_results[key] is dispatch_decision
