"""Unit tests for VacuumOps noise model.

Tests noise_impact() and noise_budget() against spec §6.5 worked examples
(Scenarios A/B/C/D).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cortex_python.modules.vacuumops.noise import noise_budget, noise_impact
from cortex_python.modules.vacuumops.schemas import PersonActivity
from tests.unit.vacuumops.conftest import make_room, make_snapshot


# ── noise_impact ──────────────────────────────────────────────────────────────


def test_noise_impact_local_radius(litter_box_job, clean_ctx):
    """Local radius = base only, no room multiplier."""
    from cortex_python.modules.vacuumops.jobs import LitterBoxJob
    job = LitterBoxJob()
    job.noise_radius = "local"
    job.noise_level = 2
    result = noise_impact(job, clean_ctx)
    assert result == 2.0


def test_noise_impact_floor_no_occupied(litter_box_job, clean_ctx):
    """Floor radius, no occupied rooms → base only."""
    # LitterBoxJob: noise_level=1, noise_radius="floor"
    result = noise_impact(litter_box_job, clean_ctx)
    # No occupied rooms → 1.0 * (1 + 0.5 * 0) = 1.0
    assert result == pytest.approx(1.0)


def test_noise_impact_floor_one_occupied(litter_box_job, clean_ctx):
    """Floor radius, one occupied room → 1.5x multiplier."""
    clean_ctx.rooms["living_room"] = make_room(raw_occupancy=True)
    result = noise_impact(litter_box_job, clean_ctx)
    # 1 occupied room on 1F → 1.0 * (1 + 0.5 * 1) = 1.5
    assert result == pytest.approx(1.5)


def test_noise_impact_floor_two_occupied(litter_box_job, clean_ctx):
    """Floor radius, two occupied rooms → 2x multiplier."""
    clean_ctx.rooms["living_room"] = make_room(raw_occupancy=True)
    clean_ctx.rooms["kitchen"] = make_room(raw_occupancy=True)
    result = noise_impact(litter_box_job, clean_ctx)
    # 2 occupied rooms → 1.0 * (1 + 0.5 * 2) = 2.0
    assert result == pytest.approx(2.0)


def test_noise_impact_house_radius(clean_ctx):
    """House radius → 1.5x base."""
    from cortex_python.modules.vacuumops.jobs import LitterBoxJob
    job = LitterBoxJob()
    job.noise_radius = "house"
    job.noise_level = 3
    result = noise_impact(job, clean_ctx)
    assert result == pytest.approx(4.5)


# ── noise_budget ──────────────────────────────────────────────────────────────


def test_noise_budget_full_open(clean_ctx):
    """All clear → budget = 10.0."""
    result = noise_budget(clean_ctx)
    assert result == pytest.approx(10.0)


def test_noise_budget_piano_active(clean_ctx):
    """Elena piano → budget = 10 * 0.05 = 0.5."""
    clean_ctx.people["elena"] = PersonActivity(
        activity="home_idle", confidence=0.9, piano=True
    )
    result = noise_budget(clean_ctx)
    assert result == pytest.approx(0.5)


def test_noise_budget_quiet_hours_1f(clean_ctx):
    """Quiet hours 1F → budget = 10 * 0.4 = 4.0."""
    clean_ctx.quiet_hours_1f = True
    result = noise_budget(clean_ctx)
    assert result == pytest.approx(4.0)


def test_noise_budget_quiet_hours_2f(clean_ctx):
    """Quiet hours 2F → budget = 10 * 0.2 = 2.0."""
    clean_ctx.quiet_hours_2f = True
    result = noise_budget(clean_ctx)
    assert result == pytest.approx(2.0)


def test_noise_budget_cooking(clean_ctx):
    """Active cooking → budget = 10 * 0.3 = 3.0."""
    clean_ctx.rooms["kitchen"] = make_room("cooking", confidence=0.8)
    result = noise_budget(clean_ctx)
    assert result == pytest.approx(3.0)


def test_noise_budget_eating(clean_ctx):
    """Active eating → budget = 10 * 0.5 = 5.0."""
    clean_ctx.rooms["kitchen"] = make_room("eating", confidence=0.8)
    result = noise_budget(clean_ctx)
    assert result == pytest.approx(5.0)


def test_noise_budget_living_room_occupied(clean_ctx):
    """Living room occupied → budget = 10 * 0.7 = 7.0."""
    clean_ctx.rooms["living_room"] = make_room("active", confidence=0.8, raw_occupancy=True)
    result = noise_budget(clean_ctx)
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
    result = noise_budget(clean_ctx)
    assert result == pytest.approx(6.0)


def test_noise_budget_multiplicative(clean_ctx):
    """Multiple constraints multiply: piano + quiet_1f = 10 * 0.05 * 0.4 = 0.2."""
    clean_ctx.people["elena"] = PersonActivity(activity="home_idle", confidence=0.9, piano=True)
    clean_ctx.quiet_hours_1f = True
    result = noise_budget(clean_ctx)
    assert result == pytest.approx(0.2)


# ── Spec §6.5 Worked Examples ─────────────────────────────────────────────────


def test_scenario_a_saturday_9pm_family_in_living_room(litter_box_job):
    """Scenario A: Saturday 9pm, family in living room, Litter Box zone empty.

    Expected: floor_clearance FAIL (living_room occupied) → defer.
    This test validates the noise gate logic specifically.
    budget = 10 * 0.7 = 7.0 (living room occupied)
    impact = 1.0 * (1 + 0.5 * 1) = 1.5 → PASS (1.5 ≤ 7.0)
    So noise_acceptable would PASS, but floor_clearance FAILS — tested in r1 tests.
    Here we just verify budget and impact values.
    """
    ctx = make_snapshot()
    ctx.rooms["living_room"] = make_room("active", confidence=0.8, raw_occupancy=True)

    budget = noise_budget(ctx)
    impact = noise_impact(litter_box_job, ctx)

    assert budget == pytest.approx(7.0)
    assert impact == pytest.approx(1.5)
    assert impact <= budget  # noise would pass; floor clearance would fail


def test_scenario_b_tuesday_8am_elena_piano(litter_box_job):
    """Scenario B: Tuesday 8am, Elena practicing piano.

    Expected: noise_impact=1.0, noise_budget=0.5 → FAIL (piano_active).
    """
    ctx = make_snapshot()
    ctx.people["elena"] = PersonActivity(
        activity="home_idle", confidence=0.9, piano=True
    )

    budget = noise_budget(ctx)
    impact = noise_impact(litter_box_job, ctx)

    assert budget == pytest.approx(0.5)
    assert impact == pytest.approx(1.0)
    assert impact > budget  # FAIL


def test_scenario_c_wednesday_11am_everyone_away(litter_box_job):
    """Scenario C: Wednesday 11am, everyone away.

    Expected: noise_impact=1.0, noise_budget=10.0 → PASS (dispatch).
    """
    ctx = make_snapshot()
    for name in ctx.people:
        ctx.people[name] = PersonActivity(activity="away", confidence=0.95)

    budget = noise_budget(ctx)
    impact = noise_impact(litter_box_job, ctx)

    assert budget == pytest.approx(10.0)
    assert impact == pytest.approx(1.0)
    assert impact <= budget  # PASS


def test_scenario_d_weekday_7am_quiet(litter_box_job):
    """Scenario D: Weekday 7:05 AM, house quiet, transit pattern look-ahead should block.

    Noise gate itself PASSES (all clear), but transit pattern blocks via R1.
    This test verifies the noise computation only.
    """
    ctx = make_snapshot()
    budget = noise_budget(ctx)
    impact = noise_impact(litter_box_job, ctx)

    # House is quiet — noise gate passes
    assert budget == pytest.approx(10.0)
    assert impact == pytest.approx(1.0)
    assert impact <= budget  # noise PASS; transit pattern tested in test_r1.py
