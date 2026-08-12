"""Unit tests for VacuumOps noise model.

Tests noise_impact() and noise_budget() against spec §6.5 worked examples
(Scenarios A/B/C/D).
"""

from __future__ import annotations


import pytest

from cortex_python.modules.vacuumops.noise import noise_budget, noise_impact
from cortex_python.modules.vacuumops.schemas import PersonActivity
from tests.unit.vacuumops.conftest import make_room, make_snapshot


# ── noise_impact ──────────────────────────────────────────────────────────────


def test_noise_impact_local_radius(clean_ctx):
    """Local radius = base only, no room multiplier."""
    from cortex_python.modules.vacuumops.jobs import Ethan3FLitterBoxJob

    job = Ethan3FLitterBoxJob()
    job.noise_radius = "local"
    job.noise_level = 2
    result = noise_impact(job, clean_ctx)
    assert result == 2.0


def test_noise_impact_floor_no_occupied(clean_ctx):
    """Floor radius, no occupied rooms → base only.

    Uses a 1F job so living_room/kitchen are in scope.
    """
    from cortex_python.modules.vacuumops.jobs import Saros1FRoomsJob

    job = Saros1FRoomsJob()
    job.noise_level = 1
    result = noise_impact(job, clean_ctx)
    # No occupied rooms → 1.0 * (1 + 0.5 * 0) = 1.0
    assert result == pytest.approx(1.0)


def test_noise_impact_floor_one_occupied(clean_ctx):
    """Floor radius, one occupied 1F room → 1.5x multiplier."""
    from cortex_python.modules.vacuumops.jobs import Saros1FRoomsJob

    job = Saros1FRoomsJob()
    job.noise_level = 1
    clean_ctx.rooms["living_room"] = make_room(raw_occupancy=True)
    result = noise_impact(job, clean_ctx)
    # 1 occupied room on 1F → 1.0 * (1 + 0.5 * 1) = 1.5
    assert result == pytest.approx(1.5)


def test_noise_impact_floor_two_occupied(clean_ctx):
    """Floor radius, two occupied 1F rooms → 2x multiplier."""
    from cortex_python.modules.vacuumops.jobs import Saros1FRoomsJob

    job = Saros1FRoomsJob()
    job.noise_level = 1
    clean_ctx.rooms["living_room"] = make_room(raw_occupancy=True)
    clean_ctx.rooms["kitchen"] = make_room(raw_occupancy=True)
    result = noise_impact(job, clean_ctx)
    # 2 occupied rooms → 1.0 * (1 + 0.5 * 2) = 2.0
    assert result == pytest.approx(2.0)


def test_noise_impact_house_radius(clean_ctx):
    """House radius → 1.5x base."""
    from cortex_python.modules.vacuumops.jobs import Ethan3FLitterBoxJob

    job = Ethan3FLitterBoxJob()
    job.noise_radius = "house"
    job.noise_level = 3
    result = noise_impact(job, clean_ctx)
    assert result == pytest.approx(4.5)


# ── noise_budget ──────────────────────────────────────────────────────────────


def test_noise_budget_full_open(clean_ctx):
    """All clear → budget = 10.0."""
    result = noise_budget(clean_ctx, "1F")
    assert result == pytest.approx(10.0)


def test_noise_budget_piano_active(clean_ctx):
    """Elena piano → budget = 10 * 0.05 = 0.5 (floor-independent)."""
    clean_ctx.people["elena"] = PersonActivity(
        activity="home_idle", confidence=0.9, piano=True
    )
    result = noise_budget(clean_ctx, "1F")
    assert result == pytest.approx(0.5)


def test_noise_budget_quiet_hours_1f_applies_no_penalty(clean_ctx):
    """quiet_hours_1f alone must NOT reduce the budget → stays 10.0.

    Regression guard for the 2026-08-11 removal of the blanket 1F quiet-hours
    penalty. The floor-aware sleep model already holds that ground-floor sound
    doesn't reach the 2F bedrooms, and real 1F presence at night is handled by
    the occupancy gates, not by the noise budget.
    """
    clean_ctx.quiet_hours_1f = True
    result = noise_budget(clean_ctx, "1F")
    assert result == pytest.approx(10.0)


def test_noise_budget_quiet_hours_1f_does_not_affect_other_floors(clean_ctx):
    """The removed penalty was 1F-scoped; 2F/3F budgets were never its business."""
    clean_ctx.quiet_hours_1f = True
    assert noise_budget(clean_ctx, "2F") == pytest.approx(10.0)
    assert noise_budget(clean_ctx, "3F") == pytest.approx(10.0)


def test_noise_budget_sleep_active_2f_floor(clean_ctx):
    """2F sleep, 2F job → budget = 10 * 0.05 = 0.5 (blocked — in the bedrooms)."""
    clean_ctx.quiet_hours_2f = True
    result = noise_budget(clean_ctx, "2F")
    assert result == pytest.approx(0.5)


def test_noise_budget_sleep_active_3f_floor(clean_ctx):
    """2F sleep, 3F job → budget = 10 * 0.20 = 2.0 (audible through 2F ceiling — blocked same as before)."""
    clean_ctx.quiet_hours_2f = True
    result = noise_budget(clean_ctx, "3F")
    assert result == pytest.approx(2.0)


def test_noise_budget_sleep_active_1f_floor(clean_ctx):
    """2F sleep, 1F job → budget = 10 * 0.80 = 8.0 (sound doesn't reach 2F)."""
    clean_ctx.quiet_hours_2f = True
    result = noise_budget(clean_ctx, "1F")
    assert result == pytest.approx(8.0)


def test_noise_budget_cooking(clean_ctx):
    """Active cooking → budget = 10 * 0.3 = 3.0."""
    clean_ctx.rooms["kitchen"] = make_room("cooking", confidence=0.8)
    result = noise_budget(clean_ctx, "1F")
    assert result == pytest.approx(3.0)


def test_noise_budget_eating(clean_ctx):
    """Active eating → budget = 10 * 0.5 = 5.0."""
    clean_ctx.rooms["kitchen"] = make_room("eating", confidence=0.8)
    result = noise_budget(clean_ctx, "1F")
    assert result == pytest.approx(5.0)


def test_noise_budget_living_room_occupied(clean_ctx):
    """Living room occupied → budget = 10 * 0.7 = 7.0."""
    clean_ctx.rooms["living_room"] = make_room(
        "active", confidence=0.8, raw_occupancy=True
    )
    result = noise_budget(clean_ctx, "1F")
    assert result == pytest.approx(7.0)


def test_noise_budget_near_term_event(clean_ctx):
    """Event in <30 min → budget = 10 * 0.6 = 6.0."""
    from cortex_python.modules.vacuumops.schemas import CalendarEvent
    from datetime import timedelta

    now = clean_ctx.timestamp
    event = CalendarEvent(
        title="Family Dinner",
        start=now + timedelta(minutes=20),
        end=now + timedelta(minutes=90),
        calendar_id="calendar.perez_melgar_family",
    )
    clean_ctx.upcoming_events = [event]
    result = noise_budget(clean_ctx, "1F")
    assert result == pytest.approx(6.0)


def test_noise_budget_multiplicative(clean_ctx):
    """Multiple constraints multiply: piano + cooking = 10 * 0.05 * 0.30 = 0.15.

    Previously paired piano with quiet_hours_1f; that penalty was removed
    2026-08-11, so this now pairs piano with cooking to keep exercising the
    multiplicative stacking itself rather than any one reducer.
    """
    clean_ctx.people["elena"] = PersonActivity(
        activity="home_idle", confidence=0.9, piano=True
    )
    clean_ctx.rooms["kitchen"] = make_room("cooking", confidence=0.9)
    result = noise_budget(clean_ctx, "1F")
    assert result == pytest.approx(0.15)


# ── Spec §6.5 Worked Examples ─────────────────────────────────────────────────


def test_scenario_a_saturday_9pm_family_in_living_room():
    """Scenario A: Saturday 9pm, family in living room, Litter Box zone empty.

    Expected: floor_clearance FAIL (living_room occupied) → defer.
    This test validates the noise gate logic specifically.
    budget = 10 * 0.7 = 7.0 (living room occupied)
    impact = 1.0 * (1 + 0.5 * 1) = 1.5 → PASS (1.5 ≤ 7.0)
    So noise_acceptable would PASS, but floor_clearance FAILS — tested in r1 tests.
    Here we just verify budget and impact values.
    Uses Saros1FRoomsJob (1F, noise_level=1) so living_room is in scope.
    """
    from cortex_python.modules.vacuumops.jobs import Saros1FRoomsJob

    job = Saros1FRoomsJob()
    job.noise_level = 1
    ctx = make_snapshot()
    ctx.rooms["living_room"] = make_room("active", confidence=0.8, raw_occupancy=True)

    budget = noise_budget(ctx, job.floor)
    impact = noise_impact(job, ctx)

    assert budget == pytest.approx(7.0)
    assert impact == pytest.approx(1.5)
    assert impact <= budget  # noise would pass; floor clearance would fail


def test_scenario_b_tuesday_8am_elena_piano():
    """Scenario B: Tuesday 8am, Elena practicing piano.

    Expected: noise_impact=1.0, noise_budget=0.5 → FAIL (piano_active).
    """
    from cortex_python.modules.vacuumops.jobs import Ethan3FLitterBoxJob

    job = Ethan3FLitterBoxJob()
    ctx = make_snapshot()
    ctx.people["elena"] = PersonActivity(
        activity="home_idle", confidence=0.9, piano=True
    )

    budget = noise_budget(ctx, job.floor)
    impact = noise_impact(job, ctx)

    assert budget == pytest.approx(0.5)
    assert impact == pytest.approx(1.0)
    assert impact > budget  # FAIL


def test_scenario_c_wednesday_11am_everyone_away():
    """Scenario C: Wednesday 11am, everyone away.

    Expected: noise_impact=1.0, noise_budget=10.0 → PASS (dispatch).
    """
    from cortex_python.modules.vacuumops.jobs import Ethan3FLitterBoxJob

    job = Ethan3FLitterBoxJob()
    ctx = make_snapshot()
    for name in ctx.people:
        ctx.people[name] = PersonActivity(activity="away", confidence=0.95)

    budget = noise_budget(ctx, job.floor)
    impact = noise_impact(job, ctx)

    assert budget == pytest.approx(10.0)
    assert impact == pytest.approx(1.0)
    assert impact <= budget  # PASS


def test_scenario_d_weekday_7am_quiet():
    """Scenario D: Weekday 7:05 AM, house quiet, transit pattern look-ahead should block.

    Noise gate itself PASSES (all clear), but transit pattern blocks via R1.
    This test verifies the noise computation only.
    """
    from cortex_python.modules.vacuumops.jobs import Ethan3FLitterBoxJob

    job = Ethan3FLitterBoxJob()
    ctx = make_snapshot()
    budget = noise_budget(ctx, job.floor)
    impact = noise_impact(job, ctx)

    # House is quiet — noise gate passes
    assert budget == pytest.approx(10.0)
    assert impact == pytest.approx(1.0)
    assert impact <= budget  # noise PASS; transit pattern tested in test_r1.py
