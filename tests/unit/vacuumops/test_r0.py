"""Unit tests for VacuumOps R0 hard gates.

Covers PASS + FAIL for each of the 5 R0 checks.
No live Redis/DB/HA calls — all external deps are mocked.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from cortex_python.modules.vacuumops.r0 import (
    battery_above_30,
    not_in_active_mission,
    not_in_zone_cooldown,
    robot_docked,
    run_r0,
    score_above_threshold,
)
from tests.unit.vacuumops.conftest import make_snapshot


# ── robot_docked ──────────────────────────────────────────────────────────────


def test_robot_docked_pass(litter_box_job, clean_ctx):
    passed, reason = robot_docked(litter_box_job, "Litter Box", clean_ctx)
    assert passed is True
    assert reason == "r0_pass"


def test_robot_docked_fail_cleaning(litter_box_job):
    ctx = make_snapshot(robot_state="cleaning")
    passed, reason = robot_docked(litter_box_job, "Litter Box", ctx)
    assert passed is False
    assert "not_docked" in reason
    assert "cleaning" in reason


def test_robot_docked_fail_returning(litter_box_job):
    ctx = make_snapshot(robot_state="returning")
    passed, reason = robot_docked(litter_box_job, "Litter Box", ctx)
    assert passed is False


def test_robot_docked_missing_robot(litter_box_job, clean_ctx):
    clean_ctx.robot_states = {}
    passed, reason = robot_docked(litter_box_job, "Litter Box", clean_ctx)
    assert passed is False
    assert "missing" in reason


# ── battery_above_30 ──────────────────────────────────────────────────────────


def test_battery_above_30_pass(litter_box_job, clean_ctx):
    passed, reason = battery_above_30(litter_box_job, "Litter Box", clean_ctx)
    assert passed is True


def test_battery_above_30_fail_low(litter_box_job):
    ctx = make_snapshot(battery=25)
    passed, reason = battery_above_30(litter_box_job, "Litter Box", ctx)
    assert passed is False
    assert "battery_low" in reason
    assert "25" in reason


def test_battery_above_30_fail_exactly_30(litter_box_job):
    ctx = make_snapshot(battery=30)
    passed, reason = battery_above_30(litter_box_job, "Litter Box", ctx)
    assert passed is False  # must be ABOVE 30, not AT 30


def test_battery_above_30_pass_31(litter_box_job):
    ctx = make_snapshot(battery=31)
    passed, reason = battery_above_30(litter_box_job, "Litter Box", ctx)
    assert passed is True


# ── score_above_threshold ─────────────────────────────────────────────────────


def test_score_above_threshold_pass(litter_box_job, clean_ctx):
    # clean_ctx has score 75.0 at zone_id=14, threshold 50.0
    passed, reason = score_above_threshold(litter_box_job, 14, clean_ctx)
    assert passed is True


def test_score_above_threshold_fail_below(litter_box_job):
    ctx = make_snapshot(litter_box_score=30.0)
    passed, reason = score_above_threshold(litter_box_job, 14, ctx)
    assert passed is False
    assert "below_threshold" in reason


def test_score_above_threshold_fail_at_threshold(litter_box_job):
    ctx = make_snapshot(litter_box_score=50.0)
    passed, reason = score_above_threshold(litter_box_job, "Litter Box", ctx)
    assert passed is False  # must be ABOVE threshold


def test_score_above_threshold_fail_missing_zone(litter_box_job, clean_ctx):
    clean_ctx.zone_scores = {}
    passed, reason = score_above_threshold(litter_box_job, "Litter Box", clean_ctx)
    assert passed is False
    assert "missing" in reason


# ── not_in_active_mission ─────────────────────────────────────────────────────


def test_not_in_active_mission_pass_docked(litter_box_job, clean_ctx):
    passed, reason = not_in_active_mission(litter_box_job, "Litter Box", clean_ctx)
    assert passed is True


@pytest.mark.parametrize("state", ["cleaning", "returning", "error", "paused"])
def test_not_in_active_mission_fail_states(litter_box_job, state):
    ctx = make_snapshot(robot_state=state)
    passed, reason = not_in_active_mission(litter_box_job, "Litter Box", ctx)
    assert passed is False
    assert state in reason


# ── not_in_zone_cooldown ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_not_in_zone_cooldown_pass(litter_box_job, clean_ctx, mock_redis):
    mock_redis.exists = AsyncMock(return_value=0)
    passed, reason = await not_in_zone_cooldown(
        litter_box_job, "Litter Box", clean_ctx, mock_redis
    )
    assert passed is True


@pytest.mark.asyncio
async def test_not_in_zone_cooldown_fail_active(litter_box_job, clean_ctx, mock_redis):
    mock_redis.exists = AsyncMock(return_value=1)
    mock_redis.ttl = AsyncMock(return_value=14400)
    passed, reason = await not_in_zone_cooldown(
        litter_box_job, "Litter Box", clean_ctx, mock_redis
    )
    assert passed is False
    assert "cooldown_active" in reason
    assert "14400" in reason


# ── run_r0 integration ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_r0_all_pass(litter_box_job, clean_ctx, mock_redis):
    passed, reason = await run_r0(litter_box_job, 14, clean_ctx, mock_redis)
    assert passed is True
    assert reason == "r0_pass"


@pytest.mark.asyncio
async def test_run_r0_short_circuits_on_battery(litter_box_job, mock_redis):
    ctx = make_snapshot(battery=10)
    passed, reason = await run_r0(litter_box_job, "Litter Box", ctx, mock_redis)
    assert passed is False
    assert "battery_low" in reason
    # Redis should NOT be called — short-circuited before the async check
    mock_redis.exists.assert_not_called()


@pytest.mark.asyncio
async def test_run_r0_short_circuits_on_score(litter_box_job, mock_redis):
    ctx = make_snapshot(litter_box_score=10.0)
    passed, reason = await run_r0(litter_box_job, 14, ctx, mock_redis)
    assert passed is False
    assert "below_threshold" in reason
    mock_redis.exists.assert_not_called()
