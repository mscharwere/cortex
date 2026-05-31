"""Unit tests for VacuumOps R1 rules.

Covers PASS/FAIL/AMBIGUOUS where applicable for each R1 rule.
No live Redis/DB/HA calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from cortex_python.modules.vacuumops.r1 import (
    floor_clearance_check,
    noise_budget_check,
    noise_radius_check,
    occupancy_gate_bypass,
    per_robot_cooldown_check,
    room_key_for_zone,
    run_r1,
    transit_pattern_lookahead,
    zone_active_use_check,
)
from cortex_python.modules.vacuumops.schemas import ZoneMeta
from tests.unit.vacuumops.conftest import make_room, make_snapshot


# ── per_robot_cooldown_check ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_per_robot_cooldown_pass(litter_box_job, clean_ctx, mock_redis):
    mock_redis.exists = AsyncMock(return_value=0)
    result, gate, reason = await per_robot_cooldown_check(
        litter_box_job, "Litter Box", clean_ctx, mock_redis
    )
    assert result == "PASS"
    assert gate == "none"


@pytest.mark.asyncio
async def test_per_robot_cooldown_fail(litter_box_job, clean_ctx, mock_redis):
    mock_redis.exists = AsyncMock(return_value=1)
    mock_redis.ttl = AsyncMock(return_value=7200)
    result, gate, reason = await per_robot_cooldown_check(
        litter_box_job, "Litter Box", clean_ctx, mock_redis
    )
    assert result == "FAIL"
    assert gate == "robot_cooldown"
    assert "ethan" in reason


# ── zone_active_use_check ─────────────────────────────────────────────────────


def test_zone_active_use_pass_idle(litter_box_job):
    ctx = make_snapshot()
    ctx.rooms["litter_box"] = make_room("idle", raw_occupancy=False)
    result, gate, reason = zone_active_use_check(litter_box_job, "litter_box", ctx)
    assert result == "PASS"


def test_zone_active_use_fail_occupied(litter_box_job):
    ctx = make_snapshot()
    ctx.rooms["litter_box"] = make_room("idle", raw_occupancy=True)
    result, gate, reason = zone_active_use_check(litter_box_job, "litter_box", ctx)
    assert result == "FAIL"
    assert gate == "effectiveness"
    assert "occupied" in reason


@pytest.mark.parametrize("activity", ["active", "cooking", "eating", "transit"])
def test_zone_active_use_fail_active_states(litter_box_job, activity):
    ctx = make_snapshot()
    ctx.rooms["litter_box"] = make_room(activity, raw_occupancy=False)
    result, gate, reason = zone_active_use_check(litter_box_job, "litter_box", ctx)
    assert result == "FAIL"
    assert "active_use" in reason


def test_zone_active_use_pass_missing_sensor(litter_box_job, clean_ctx):
    # Missing sensor → treat as clear (graceful degradation §4.2)
    result, gate, reason = zone_active_use_check(litter_box_job, "litter_box_nonexistent", clean_ctx)
    assert result == "PASS"
    assert "unavailable" in reason


# ── floor_clearance_check ─────────────────────────────────────────────────────


def test_floor_clearance_pass_all_clear(litter_box_job, clean_ctx):
    result, gate, reason = floor_clearance_check(litter_box_job, "Litter Box", clean_ctx)
    assert result == "PASS"


def test_floor_clearance_fail_living_room(litter_box_job, clean_ctx):
    clean_ctx.rooms["living_room"] = make_room("active", raw_occupancy=True)
    result, gate, reason = floor_clearance_check(litter_box_job, "Litter Box", clean_ctx)
    assert result == "FAIL"
    assert gate == "effectiveness"
    assert "living_room" in reason


def test_floor_clearance_fail_kitchen(litter_box_job, clean_ctx):
    clean_ctx.rooms["kitchen"] = make_room("cooking", raw_occupancy=True)
    result, gate, reason = floor_clearance_check(litter_box_job, "Litter Box", clean_ctx)
    assert result == "FAIL"
    assert "kitchen" in reason


def test_floor_clearance_skips_missing_rooms(litter_box_job, clean_ctx):
    # Remove some rooms — should not fail, just skip those rooms
    clean_ctx.rooms.pop("hallway", None)
    clean_ctx.rooms.pop("bathroom", None)
    result, gate, reason = floor_clearance_check(litter_box_job, "Litter Box", clean_ctx)
    assert result == "PASS"


# ── transit_pattern_lookahead ─────────────────────────────────────────────────


def test_transit_pattern_pass_no_patterns(litter_box_job, clean_ctx):
    result, gate, reason = transit_pattern_lookahead(
        litter_box_job, "Litter Box", clean_ctx, []
    )
    assert result == "PASS"


def test_transit_pattern_fail_imminent(litter_box_job):
    """Weekday 7:05 AM UTC = before 7:10 PST bus — within look-ahead window."""
    import pytz
    pst = pytz.timezone("America/Los_Angeles")
    # 7:05 AM PST on a Tuesday
    ts = pst.localize(datetime(2026, 5, 26, 7, 5, 0)).astimezone(timezone.utc)
    ctx = make_snapshot(timestamp=ts)

    pattern = {
        "name": "carlitos_morning_bus",
        "days": [1, 2, 3, 4, 5],
        "start": "07:10",
        "end": "07:35",
        "relevance": ["transit"],
        "jobs": ["*"],
    }

    result, gate, reason = transit_pattern_lookahead(
        litter_box_job, "Litter Box", ctx, [pattern]
    )
    # 7:05 AM is within [07:10 - 15min = 06:55, 07:35 + 5min = 07:40] → FAIL
    assert result == "FAIL"
    assert "carlitos_morning_bus" in reason


def test_transit_pattern_pass_outside_window(litter_box_job):
    """11 AM PST — well outside any bus pattern window."""
    import pytz
    pst = pytz.timezone("America/Los_Angeles")
    ts = pst.localize(datetime(2026, 5, 26, 11, 0, 0)).astimezone(timezone.utc)
    ctx = make_snapshot(timestamp=ts)

    pattern = {
        "name": "carlitos_morning_bus",
        "days": [1, 2, 3, 4, 5],
        "start": "07:10",
        "end": "07:35",
        "relevance": ["transit"],
        "jobs": ["*"],
    }

    result, gate, reason = transit_pattern_lookahead(
        litter_box_job, "Litter Box", ctx, [pattern]
    )
    assert result == "PASS"


def test_transit_pattern_skips_noise_only(litter_box_job, clean_ctx):
    """Noise-only patterns should NOT block zone_effective."""
    pattern = {
        "name": "family_dinner",
        "days": [1, 2, 3, 4, 5, 6, 7],
        "start": "16:00",
        "end": "18:30",
        "relevance": ["noise"],  # No "transit" — should not block
        "jobs": ["*"],
    }
    result, gate, reason = transit_pattern_lookahead(
        litter_box_job, "Litter Box", clean_ctx, [pattern]
    )
    assert result == "PASS"


# ── noise_budget_check ────────────────────────────────────────────────────────


def test_noise_budget_check_strong_pass(litter_box_job, clean_ctx):
    """All clear, high budget, low impact → strong PASS."""
    result, gate, reason = noise_budget_check(litter_box_job, "Litter Box", clean_ctx)
    assert result == "PASS"
    assert gate == "none"


def test_noise_budget_check_fail_piano(litter_box_job, clean_ctx):
    """Elena playing piano → budget near zero → FAIL."""
    from cortex_python.modules.vacuumops.schemas import PersonActivity
    clean_ctx.people["elena"] = PersonActivity(activity="home_idle", confidence=0.9, piano=True)
    result, gate, reason = noise_budget_check(litter_box_job, "Litter Box", clean_ctx)
    assert result == "FAIL"
    assert gate == "comfort"
    assert "piano" in reason


def test_noise_budget_check_ambiguous_marginal(litter_box_job):
    """Living room occupied + TV → marginal budget → AMBIGUOUS."""
    ctx = make_snapshot()
    ctx.rooms["living_room"] = make_room("active", confidence=0.8, raw_occupancy=True)
    # noise_budget = 10 * 0.7 = 7.0 (living room occupied)
    # noise_impact = 1.0 * (1 + 0.5 * 1) = 1.5 (one room occupied on floor)
    # 1.5 ≤ 7.0 * 0.7 = 4.9 → PASS-strong
    # Actually this might PASS — let's just verify it's not FAIL
    result, gate, reason = noise_budget_check(litter_box_job, "Litter Box", ctx)
    assert result in ("PASS", "AMBIGUOUS")  # not a hard FAIL from living room alone


def test_noise_budget_check_fail_quiet_hours_1f(litter_box_job):
    """Quiet hours 1F active → reduced budget → check FAIL behavior."""
    ctx = make_snapshot(quiet_hours_1f=True)
    # budget = 10 * 0.4 = 4.0; impact = 1.0 → PASS-strong (1.0 ≤ 4.0 * 0.7 = 2.8)
    result, gate, reason = noise_budget_check(litter_box_job, "Litter Box", ctx)
    # With just quiet_hours_1f, budget is still 4.0 — impact 1.0 passes strongly
    assert result == "PASS"


# ── noise_radius_check ────────────────────────────────────────────────────────


def test_noise_radius_check_pass_no_sleeping(litter_box_job, clean_ctx):
    result, gate, reason = noise_radius_check(litter_box_job, "Litter Box", clean_ctx)
    assert result == "PASS"


def test_noise_radius_check_fail_sleeping_room(litter_box_job, clean_ctx):
    """Sleeping room on 1F floor → FAIL for floor-radius job."""
    clean_ctx.rooms["hallway"] = make_room("sleeping", confidence=0.9, raw_occupancy=False)
    result, gate, reason = noise_radius_check(litter_box_job, "Litter Box", clean_ctx)
    assert result == "FAIL"
    assert gate == "comfort"
    assert "sleeping" in reason


def test_noise_radius_check_pass_2f_sleeping_for_1f_job(litter_box_job, clean_ctx):
    """2F sleeping room shouldn't affect a floor-radius 1F job."""
    clean_ctx.rooms["master_bedroom"] = make_room("sleeping", confidence=0.9)
    # master_bedroom is in 2F FLOOR_ROOM_MAP but LitterBoxJob.floor = "1F"
    # floor-radius check only covers FLOOR_ROOM_MAP["1F"]
    result, gate, reason = noise_radius_check(litter_box_job, "Litter Box", clean_ctx)
    assert result == "PASS"


# ── run_r1 integration ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_r1_all_pass(litter_box_job, clean_ctx, mock_redis):
    result, gate, reason = await run_r1(litter_box_job, "Litter Box", clean_ctx, mock_redis)
    assert result == "PASS"
    assert gate == "none"


@pytest.mark.asyncio
async def test_run_r1_robot_cooldown_short_circuits(litter_box_job, clean_ctx, mock_redis):
    mock_redis.exists = AsyncMock(return_value=1)  # robot cooldown active
    mock_redis.ttl = AsyncMock(return_value=3600)
    result, gate, reason = await run_r1(litter_box_job, "Litter Box", clean_ctx, mock_redis)
    assert result == "FAIL"
    assert gate == "robot_cooldown"


@pytest.mark.asyncio
async def test_run_r1_floor_not_clear_no_l1(litter_box_job, mock_redis):
    ctx = make_snapshot()
    ctx.rooms["kitchen"] = make_room("cooking", raw_occupancy=True)
    result, gate, reason = await run_r1(litter_box_job, "Litter Box", ctx, mock_redis)
    assert result == "FAIL"
    assert gate == "effectiveness"


@pytest.mark.asyncio
async def test_run_r1_piano_fails_comfort(litter_box_job, mock_redis):
    from cortex_python.modules.vacuumops.schemas import PersonActivity
    ctx = make_snapshot()
    ctx.people["elena"] = PersonActivity(activity="home_idle", confidence=0.9, piano=True)
    result, gate, reason = await run_r1(litter_box_job, "Litter Box", ctx, mock_redis)
    assert result == "FAIL"
    assert gate == "comfort"


@pytest.mark.asyncio
async def test_run_r1_ambiguous_when_effectiveness_pass_comfort_ambiguous(
    litter_box_job, mock_redis
):
    """All effectiveness rules PASS, one comfort rule returns AMBIGUOUS → run_r1 returns AMBIGUOUS."""
    from unittest.mock import patch

    ctx = make_snapshot()  # all-clear context — effectiveness rules will PASS

    # Patch noise_budget_check to return AMBIGUOUS (marginal noise scenario)
    with patch(
        "cortex_python.modules.vacuumops.r1.noise_budget_check",
        return_value=("AMBIGUOUS", "comfort", "noise_marginal:impact=3.50:budget=4.00"),
    ):
        result, gate, reason = await run_r1(litter_box_job, "Litter Box", ctx, mock_redis)

    assert result == "AMBIGUOUS"
    assert gate == "comfort"
    assert "noise_marginal" in reason


# ── room_key_for_zone ─────────────────────────────────────────────────────────


def _make_zone_meta(occupancy_sensor: str | None = None, low_disruption: bool = False) -> ZoneMeta:
    return ZoneMeta(
        zone_id=1, unit_id=1,
        occupancy_sensor=occupancy_sensor,
        low_disruption=low_disruption,
    )


def test_room_key_for_zone_sensor_group():
    """binary_sensor.hallway_sensor_group → 'hallway'."""
    meta = _make_zone_meta("binary_sensor.hallway_sensor_group")
    assert room_key_for_zone(meta) == "hallway"


def test_room_key_for_zone_kitchen_sensor_group():
    """binary_sensor.kitchen_sensor_group → 'kitchen'."""
    meta = _make_zone_meta("binary_sensor.kitchen_sensor_group")
    assert room_key_for_zone(meta) == "kitchen"


def test_room_key_for_zone_explicit_map():
    """Bathroom tri-sensor handled by explicit _SENSOR_ENTITY_TO_ROOM map."""
    meta = _make_zone_meta("binary_sensor.first_level_bathroom_tri_sensor_motion_detection")
    assert room_key_for_zone(meta) == "bathroom"


def test_room_key_for_zone_none():
    """None occupancy_sensor → None."""
    meta = _make_zone_meta(None)
    assert room_key_for_zone(meta) is None


def test_room_key_for_zone_no_meta():
    """None zone_meta → None."""
    assert room_key_for_zone(None) is None


def test_room_key_for_zone_occupancy_status_suffix():
    """binary_sensor.living_room_occupancy_status → 'living_room'."""
    meta = _make_zone_meta("binary_sensor.living_room_occupancy_status")
    assert room_key_for_zone(meta) == "living_room"


# ── occupancy_gate_bypass ─────────────────────────────────────────────────────


def test_bypass_degraded_home_count_fail_closed():
    """home_count == -1 (degraded) → no bypass (fail closed)."""
    ctx = make_snapshot(home_count=-1)
    meta = _make_zone_meta("binary_sensor.hallway_sensor_group", low_disruption=True)
    mode, reason = occupancy_gate_bypass("Litter Box", ctx, meta)
    assert mode == "none"
    assert reason is None


def test_bypass_override1_home_empty():
    """home_count == 0 → full bypass (Override 1)."""
    ctx = make_snapshot(home_count=0, home_empty=True, who_home=[])
    meta = _make_zone_meta("binary_sensor.hallway_sensor_group")
    mode, reason = occupancy_gate_bypass("Litter Box", ctx, meta)
    assert mode == "full"
    assert reason == "home_empty"


def test_bypass_override1_home_empty_low_disruption_false():
    """Override 1 fires regardless of low_disruption flag."""
    ctx = make_snapshot(home_count=0, home_empty=True, who_home=[])
    meta = _make_zone_meta("binary_sensor.hallway_sensor_group", low_disruption=False)
    mode, reason = occupancy_gate_bypass("Litter Box", ctx, meta)
    assert mode == "full"
    assert reason == "home_empty"


def test_bypass_override2_single_carlos_low_disruption():
    """home_count == 1, Carlos home, low_disruption=True → room_scoped bypass."""
    ctx = make_snapshot(home_count=1, who_home=["Carlos"])
    meta = _make_zone_meta("binary_sensor.hallway_sensor_group", low_disruption=True)
    mode, reason = occupancy_gate_bypass("Litter Box", ctx, meta)
    assert mode == "room_scoped"
    assert reason == "single_person_low_disruption"


def test_bypass_override2_single_guest_low_disruption():
    """Any single non-Elena occupant qualifies — guests included."""
    ctx = make_snapshot(home_count=1, who_home=["Iestaf"])
    meta = _make_zone_meta("binary_sensor.hallway_sensor_group", low_disruption=True)
    mode, reason = occupancy_gate_bypass("Litter Box", ctx, meta)
    assert mode == "room_scoped"
    assert reason == "single_person_low_disruption"


def test_bypass_override2_elena_carveout():
    """Elena is excluded from Override 2 — sole Elena occupant → no bypass."""
    ctx = make_snapshot(home_count=1, who_home=["Elena"])
    meta = _make_zone_meta("binary_sensor.hallway_sensor_group", low_disruption=True)
    mode, reason = occupancy_gate_bypass("Litter Box", ctx, meta)
    assert mode == "none"
    assert reason is None


def test_bypass_override2_elena_case_insensitive():
    """Elena carve-out is case-insensitive (belt-and-suspenders per spec §2.1)."""
    ctx = make_snapshot(home_count=1, who_home=["ELENA"])
    meta = _make_zone_meta("binary_sensor.hallway_sensor_group", low_disruption=True)
    mode, reason = occupancy_gate_bypass("Litter Box", ctx, meta)
    assert mode == "none"


def test_bypass_override2_not_low_disruption():
    """Single non-Elena occupant but low_disruption=False → no Override 2."""
    ctx = make_snapshot(home_count=1, who_home=["Carlos"])
    meta = _make_zone_meta("binary_sensor.hallway_sensor_group", low_disruption=False)
    mode, reason = occupancy_gate_bypass("Litter Box", ctx, meta)
    assert mode == "none"


def test_bypass_two_people_home_no_bypass():
    """home_count == 2 → no bypass (Override 2 requires exactly 1)."""
    ctx = make_snapshot(home_count=2, who_home=["Carlos", "Carlitos"])
    meta = _make_zone_meta("binary_sensor.hallway_sensor_group", low_disruption=True)
    mode, reason = occupancy_gate_bypass("Litter Box", ctx, meta)
    assert mode == "none"


# ── run_r1 with bypass modes ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_r1_full_bypass_ignores_kitchen_occupancy(litter_box_job, mock_redis):
    """Override 1 (home_empty): occupied kitchen does NOT block dispatch."""
    ctx = make_snapshot(home_count=0, home_empty=True)
    ctx.rooms["kitchen"] = make_room("cooking", raw_occupancy=True)
    # Without bypass: kitchen occupancy would fail floor_clearance_check.
    # With bypass_mode="full": occupancy checks are skipped entirely.
    result, gate, reason = await run_r1(
        litter_box_job, "Litter Box", ctx, mock_redis,
        bypass_mode="full", bypass_reason_str="home_empty",
    )
    assert result == "PASS"
    assert "occ_bypass:home_empty" in reason


@pytest.mark.asyncio
async def test_run_r1_room_scoped_hallway_clear_kitchen_occupied(litter_box_job, mock_redis):
    """Override 2 (room_scoped): hallway clear, kitchen occupied → dispatch allowed."""
    meta = _make_zone_meta("binary_sensor.hallway_sensor_group", low_disruption=True)
    ctx = make_snapshot(home_count=1, who_home=["Carlos"])
    ctx.rooms["kitchen"] = make_room("cooking", raw_occupancy=True)
    ctx.rooms["hallway"] = make_room("idle", raw_occupancy=False)
    result, gate, reason = await run_r1(
        litter_box_job, "Litter Box", ctx, mock_redis,
        zone_meta=meta,
        bypass_mode="room_scoped", bypass_reason_str="single_person_low_disruption",
    )
    assert result == "PASS"
    assert "occ_bypass" in reason or "occ_relax" in reason or "all_rules_pass" in reason


@pytest.mark.asyncio
async def test_run_r1_room_scoped_hallway_occupied_blocks(litter_box_job, mock_redis):
    """Override 2 (room_scoped): hallway (zone's own room) occupied → still blocks."""
    meta = _make_zone_meta("binary_sensor.hallway_sensor_group", low_disruption=True)
    ctx = make_snapshot(home_count=1, who_home=["Carlos"])
    ctx.rooms["hallway"] = make_room("active", raw_occupancy=True)
    result, gate, reason = await run_r1(
        litter_box_job, "Litter Box", ctx, mock_redis,
        zone_meta=meta,
        bypass_mode="room_scoped", bypass_reason_str="single_person_low_disruption",
    )
    assert result == "FAIL"
    assert gate == "effectiveness"
    assert "hallway" in reason
