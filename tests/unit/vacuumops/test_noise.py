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


def test_noise_budget_carlos_in_meeting_3f(clean_ctx):
    """Carlos in meeting, 3F job → budget = 10 * 0.05 = 0.5 (below dispatch bar)."""
    clean_ctx.home["carlos_in_meeting"] = True
    result = noise_budget(clean_ctx, "3F")
    assert result == pytest.approx(0.5)


def test_noise_budget_carlos_not_in_meeting_3f(clean_ctx):
    """Carlos NOT in meeting, 3F job → budget stays fully open (above dispatch bar)."""
    clean_ctx.home["carlos_in_meeting"] = False
    result = noise_budget(clean_ctx, "3F")
    assert result == pytest.approx(10.0)


def test_carlos_in_meeting_absent_key_is_a_correct_noop(clean_ctx):
    """The reducer must not need the key to exist — a pre-rollout ctx.home
    (no carlos_in_meeting attribute yet) must behave exactly like 'off'.
    This is what makes C2 correct-and-inert while Ethan is still dark (D5)."""
    assert "carlos_in_meeting" not in clean_ctx.home
    result = noise_budget(clean_ctx, "3F")
    assert result == pytest.approx(10.0)


def test_carlos_in_meeting_does_not_reduce_1f_budget(clean_ctx):
    """Regression guard: the cortex#46 floor-leak shape — a floor-scoped
    reducer must not leak onto other floors. 1F must be bit-for-bit unchanged."""
    clean_ctx.home["carlos_in_meeting"] = True
    assert noise_budget(clean_ctx, "1F") == pytest.approx(10.0)


def test_carlos_in_meeting_does_not_reduce_2f_budget(clean_ctx):
    """Same regression guard for 2F."""
    clean_ctx.home["carlos_in_meeting"] = True
    assert noise_budget(clean_ctx, "2F") == pytest.approx(10.0)


def test_noise_budget_quiet_hours_1f(clean_ctx):
    """Quiet hours 1F, 1F job → budget = 10 * 0.4 = 4.0."""
    clean_ctx.quiet_hours_1f = True
    result = noise_budget(clean_ctx, "1F")
    assert result == pytest.approx(4.0)


def test_noise_budget_sleep_active_2f_floor(clean_ctx):
    """2F sleep, 2F job → budget = 10 * 0.05 * 0.40 = 0.2 (blocked — in the bedrooms).

    Both the sleep tier (×0.05) and the household quiet-hours reducer (×0.40)
    fire, because quiet_hours_2f drives both.

    NOTE: this assertion was 0.5 before the quiet-hours split, when the ×0.40
    reducer keyed off quiet_hours_1f alone. That 0.5 was never a value
    production produced: the synth aliased quiet_hours_1f = quiet_hours_2f, so
    a real overnight snapshot always had BOTH flags set and 2F always landed on
    0.2. The split makes the ctx here (2f set, 1f clear) reachable for the first
    time, and 0.2 is what preserves the real behaviour.
    """
    clean_ctx.quiet_hours_2f = True
    result = noise_budget(clean_ctx, "2F")
    assert result == pytest.approx(0.2)


def test_noise_budget_sleep_active_3f_floor(clean_ctx):
    """2F sleep, 3F job → budget = 10 * 0.20 * 0.40 = 0.8 (audible through 2F ceiling).

    Same note as test_noise_budget_sleep_active_2f_floor: the pre-split
    assertion of 2.0 described a ctx production never built. 0.8 is what 3F
    actually got overnight before this change, and still gets after it.
    """
    clean_ctx.quiet_hours_2f = True
    result = noise_budget(clean_ctx, "3F")
    assert result == pytest.approx(0.8)


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
    """Multiple constraints multiply: piano + quiet_1f = 10 * 0.05 * 0.4 = 0.2."""
    clean_ctx.people["elena"] = PersonActivity(
        activity="home_idle", confidence=0.9, piano=True
    )
    clean_ctx.quiet_hours_1f = True
    result = noise_budget(clean_ctx, "1F")
    assert result == pytest.approx(0.2)


# ── Quiet-hours split: quiet_hours_1f is floor-scoped and independent of 2F ───
#
# Regression coverage for P3 / memo risk R9. These two flags used to hold the
# same value, so the ×0.40 reducer fired on every floor and 1F could not be
# relaxed overnight without also relaxing 2F.


def test_quiet_hours_1f_does_not_reduce_2f_budget(clean_ctx):
    """quiet_hours_1f is 1F-scoped: a 2F job must not see the ×0.40 reducer.

    Applying a flag named _1f to 2F was a latent bug the aliasing hid.
    """
    clean_ctx.quiet_hours_1f = True
    clean_ctx.quiet_hours_2f = False
    assert noise_budget(clean_ctx, "2F") == pytest.approx(10.0)


def test_quiet_hours_1f_does_not_reduce_3f_budget(clean_ctx):
    """Same as above for 3F."""
    clean_ctx.quiet_hours_1f = True
    clean_ctx.quiet_hours_2f = False
    assert noise_budget(clean_ctx, "3F") == pytest.approx(10.0)


def test_quiet_hours_2f_does_not_reduce_1f_budget_beyond_sleep_tier(clean_ctx):
    """THE P3 CASE — 23:00-07:00 on 1F.

    Household quiet hours are active (2F asleep) but the short 1F courtesy
    window has already closed. 1F must get ONLY the floor-aware sleep tier
    (×0.80), not a second ×0.40 on top of it.

    Before the split this returned 3.2, which put Saros1FRoomsJob
    (noise_impact 3.0 on a clear floor) at AMBIGUOUS — an L1 call at 3am.
    At 8.0 it is a strong PASS, which is what makes the overnight window
    reachable at all.
    """
    clean_ctx.quiet_hours_2f = True
    clean_ctx.quiet_hours_1f = False
    assert noise_budget(clean_ctx, "1F") == pytest.approx(8.0)


def test_quiet_hours_both_active_1f_still_reduced(clean_ctx):
    """22:00-23:00 on 1F — the courtesy window is open, so ×0.80 × ×0.40 = 3.2.

    The split relaxes the late-night band, not the wind-down hour.
    """
    clean_ctx.quiet_hours_2f = True
    clean_ctx.quiet_hours_1f = True
    assert noise_budget(clean_ctx, "1F") == pytest.approx(3.2)


def test_saros_1f_rooms_overnight_is_strong_pass(clean_ctx):
    """End-to-end on the real job: Saros 1F rooms passes noise at 23:00-07:00.

    Ties the budget change to the dispatch decision it is meant to unblock.
    """
    from cortex_python.modules.vacuumops.jobs import Saros1FRoomsJob
    from cortex_python.modules.vacuumops.r1 import noise_budget_check

    job = Saros1FRoomsJob()
    clean_ctx.quiet_hours_2f = True  # household asleep
    clean_ctx.quiet_hours_1f = False  # past 23:00

    # impact = noise_level 3 * (1 + 0.5 * 0 occupied 1F rooms) = 3.0
    assert noise_impact(job, clean_ctx) == pytest.approx(3.0)
    # budget = 10 * 0.80 = 8.0 → 3.0 <= 8.0 * 0.7 = 5.6 → strong PASS
    result, gate, reason = noise_budget_check(job, 21, clean_ctx)
    assert result == "PASS"
    assert gate == "none"


def test_saros_1f_rooms_courtesy_window_is_not_strong_pass(clean_ctx):
    """The 22:00-23:00 hour still escalates rather than waving Saros through."""
    from cortex_python.modules.vacuumops.jobs import Saros1FRoomsJob
    from cortex_python.modules.vacuumops.r1 import noise_budget_check

    job = Saros1FRoomsJob()
    clean_ctx.quiet_hours_2f = True
    clean_ctx.quiet_hours_1f = True

    # budget = 10 * 0.80 * 0.40 = 3.2; impact 3.0 > 3.2 * 0.7 = 2.24 → AMBIGUOUS
    result, gate, reason = noise_budget_check(job, 21, clean_ctx)
    assert result == "AMBIGUOUS"


def test_sam_2f_overnight_still_blocked_after_split(clean_ctx):
    """Guardrail: the 1F relaxation must not leak into Sam's 2F overnight gate."""
    from cortex_python.modules.vacuumops.jobs import Sam2FJob
    from cortex_python.modules.vacuumops.r1 import noise_budget_check

    job = Sam2FJob()
    clean_ctx.quiet_hours_2f = True
    clean_ctx.quiet_hours_1f = False

    # budget = 10 * 0.05 * 0.40 = 0.2; impact = 4 * 1.5 (house radius) = 6.0
    assert noise_budget(clean_ctx, "2F") == pytest.approx(0.2)
    result, gate, reason = noise_budget_check(job, 30, clean_ctx)
    assert result == "FAIL"


def test_ethan_3f_rooms_overnight_still_blocked_after_split(clean_ctx):
    """Guardrail: same for Ethan on 3F.

    3F is the floor most at risk of an accidental loosening — its sleep tier is
    ×0.20 and Ethan3FRoomsJob's impact on a clear floor is exactly 2.0, so
    dropping the ×0.40 would have flipped FAIL → AMBIGUOUS.
    """
    from cortex_python.modules.vacuumops.jobs import Ethan3FRoomsJob
    from cortex_python.modules.vacuumops.r1 import noise_budget_check

    job = Ethan3FRoomsJob()
    clean_ctx.quiet_hours_2f = True
    clean_ctx.quiet_hours_1f = False

    # budget = 10 * 0.20 * 0.40 = 0.8; impact = 2 * (1 + 0.5 * 0) = 2.0
    assert noise_budget(clean_ctx, "3F") == pytest.approx(0.8)
    result, gate, reason = noise_budget_check(job, 10, clean_ctx)
    assert result == "FAIL"


# ── C2 (D5): carlos_in_meeting reducer — dispatch-suppressing, not blocking ────


def test_carlos_in_meeting_suppresses_ethan_3f_rooms_dispatch(clean_ctx):
    """Meeting on, 3F job → comfort-tier FAIL naming the meeting as the cause.

    Ethan3FRoomsJob: noise_level=2, floor radius, no occupied 3F rooms →
    impact = 2 * (1 + 0.5 * 0) = 2.0. budget = 10 * 0.05 = 0.5. 2.0 > 0.5 → FAIL.
    Confirms the reducer actually changes dispatch behaviour, and that it is
    suppression on the comfort tier (gate == "comfort"), never a hard block of
    the effectiveness gate.
    """
    from cortex_python.modules.vacuumops.jobs import Ethan3FRoomsJob
    from cortex_python.modules.vacuumops.r1 import noise_budget_check

    job = Ethan3FRoomsJob()
    clean_ctx.home["carlos_in_meeting"] = True

    result, gate, reason = noise_budget_check(job, 15, clean_ctx)
    assert result == "FAIL"
    assert gate == "comfort"
    assert reason == "carlos_in_meeting_active"


def test_carlos_not_in_meeting_ethan_3f_rooms_dispatches_normally(clean_ctx):
    """Meeting off (default clean_ctx) → the same job passes noise normally.

    Establishes the "above the dispatch bar when off" half of the C2 test bar.
    """
    from cortex_python.modules.vacuumops.jobs import Ethan3FRoomsJob
    from cortex_python.modules.vacuumops.r1 import noise_budget_check

    job = Ethan3FRoomsJob()
    result, gate, reason = noise_budget_check(job, 15, clean_ctx)
    assert result == "PASS"
    assert gate == "none"


def test_carlos_in_meeting_does_not_touch_1f_dispatch(clean_ctx):
    """A meeting must not suppress a concurrent 1F dispatch — floor-scoped only."""
    from cortex_python.modules.vacuumops.jobs import Saros1FRoomsJob
    from cortex_python.modules.vacuumops.r1 import noise_budget_check

    job = Saros1FRoomsJob()
    clean_ctx.home["carlos_in_meeting"] = True

    result, gate, reason = noise_budget_check(job, 21, clean_ctx)
    assert result == "PASS"
    assert gate == "none"


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
