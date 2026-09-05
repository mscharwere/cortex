"""Unit tests for the quiet_hours_1f / quiet_hours_2f split.

Covers memo risk R9 / recommendation P3: quiet_hours_1f and quiet_hours_2f used
to be the SAME value — the synth assigned both from
sensor.home_context.attributes.quiet_hours — so 1F could not be relaxed
overnight without also relaxing 2F.

Two things need guarding against regressing silently:
  1. utils.is_quiet_hours_1f() computes the intended 1F-local window, in PST,
     across DST, with midnight wrap supported so retuning the bounds cannot
     produce an always-false window.
  2. build_snapshot() actually wires the two flags to DIFFERENT sources. This
     mirrors the repo's existing wiring-test precedent (test_mop.py's
     TestSettingsWiring / TestLiveMopEnabledWiring): the bug class here is a
     field that exists and is documented but is fed the wrong value, which no
     test that constructs ContextSnapshot directly can catch.

Budget-side consequences of the split live in test_noise.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from cortex_python.adapters.homeops_adapter import VacuumOpsLiveSettings
from cortex_python.modules.vacuumops.utils import (
    QUIET_HOURS_1F_END_HOUR,
    QUIET_HOURS_1F_START_HOUR,
    is_quiet_hours_1f,
)


def _pst(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A UTC datetime that lands on the given PST wall-clock time in 2026."""
    import pytz

    pst = pytz.timezone("America/Los_Angeles")
    return pst.localize(datetime(2026, month, day, hour, minute)).astimezone(UTC)


# ── is_quiet_hours_1f — window shape ──────────────────────────────────────────


def test_default_window_is_22_to_23():
    """The shipped window is one hour: 22:00-23:00 PST.

    It keeps the household quiet-hours START (22:00, per
    sensor.home_context.quiet_hours = `hour >= 22 or hour < 7`) and ends at the
    measured 23:00 occupancy cliff instead of running to 07:00.
    """
    assert (QUIET_HOURS_1F_START_HOUR, QUIET_HOURS_1F_END_HOUR) == (22, 23)


@pytest.mark.parametrize("hour", [22])
def test_inside_window(hour):
    assert is_quiet_hours_1f(_pst(9, 1, hour)) is True


@pytest.mark.parametrize("hour", [23, 0, 1, 3, 5, 6, 7, 9, 12, 15, 18, 21])
def test_outside_window(hour):
    """Everything except the 22:00 hour is clear — most importantly 23:00-07:00.

    That band is where essentially all of 1F's long clear windows are, and it
    was fully suppressed before the split.
    """
    assert is_quiet_hours_1f(_pst(9, 1, hour)) is False


def test_window_is_half_open_on_the_hour():
    """[22:00, 23:00) — 22:00 in, 21:59 out, 23:00 out."""
    assert is_quiet_hours_1f(_pst(9, 1, 21, 59)) is False
    assert is_quiet_hours_1f(_pst(9, 1, 22, 0)) is True
    assert is_quiet_hours_1f(_pst(9, 1, 22, 59)) is True
    assert is_quiet_hours_1f(_pst(9, 1, 23, 0)) is False


# ── is_quiet_hours_1f — timezone handling ─────────────────────────────────────


def test_uses_pst_not_utc():
    """22:00 PST is 05:00 UTC the next day — a naive UTC read would miss it."""
    ts = _pst(9, 1, 22)
    assert ts.astimezone(UTC).hour == 5
    assert is_quiet_hours_1f(ts) is True


def test_dst_summer_and_winter_both_correct():
    """The window is wall-clock PST year-round, across the DST boundary.

    Sep (PDT, UTC-7) and Jan (PST, UTC-8) resolve to different UTC hours for the
    same local 22:00; both must read as in-window.
    """
    summer = _pst(9, 1, 22)
    winter = _pst(1, 15, 22)
    assert summer.astimezone(UTC).hour != winter.astimezone(UTC).hour
    assert is_quiet_hours_1f(summer) is True
    assert is_quiet_hours_1f(winter) is True
    assert is_quiet_hours_1f(_pst(1, 15, 2)) is False


def test_accepts_naive_free_utc_input():
    """The loop hands in UTC-aware timestamps; that path must work directly."""
    assert is_quiet_hours_1f(datetime(2026, 9, 2, 5, 30, tzinfo=UTC)) is True
    assert is_quiet_hours_1f(datetime(2026, 9, 2, 10, 0, tzinfo=UTC)) is False


# ── is_quiet_hours_1f — retuning the bounds ───────────────────────────────────


def test_wrap_past_midnight_supported():
    """A wrapping window (22 → 7, the old household shape) must work.

    Guards against a future retune silently producing an always-false window.
    """
    assert is_quiet_hours_1f(_pst(9, 1, 23), start_hour=22, end_hour=7) is True
    assert is_quiet_hours_1f(_pst(9, 1, 3), start_hour=22, end_hour=7) is True
    assert is_quiet_hours_1f(_pst(9, 1, 6, 59), start_hour=22, end_hour=7) is True
    assert is_quiet_hours_1f(_pst(9, 1, 7), start_hour=22, end_hour=7) is False
    assert is_quiet_hours_1f(_pst(9, 1, 12), start_hour=22, end_hour=7) is False


def test_equal_bounds_means_no_quiet_hours_not_all_day():
    """start == end is an EMPTY window, never a 24-hour one."""
    for hour in (0, 6, 12, 22, 23):
        assert is_quiet_hours_1f(_pst(9, 1, hour), start_hour=22, end_hour=22) is False


# ── build_snapshot wiring ─────────────────────────────────────────────────────


def _mock_adapters(*, home_quiet_hours: bool):
    """Minimal adapter doubles for build_snapshot.

    Only sensor.home_context resolves; every other entity read returns None, so
    the snapshot lands in its degraded/default path. That is fine here — the
    quiet-hours assignment is unconditional and does not depend on any of it.
    """
    ha = MagicMock()

    async def _get_entity_state(entity_id: str):
        if entity_id == "sensor.home_context":
            return {
                "state": "home",
                "attributes": {
                    "quiet_hours": home_quiet_hours,
                    "home_count": 4,
                    "who_home": ["Carlos", "Elena"],
                },
            }
        return None

    ha.get_entity_state = AsyncMock(side_effect=_get_entity_state)
    ha.list_calendar_entities = AsyncMock(return_value=[])
    ha.get_calendar_events = AsyncMock(return_value=[])

    homeops = MagicMock()
    homeops.get_zone_data = AsyncMock(return_value=({19: 50.0}, {}, {}))
    homeops.get_zone_metadata = AsyncMock(return_value={})
    homeops.get_vacuumops_settings = AsyncMock(return_value=VacuumOpsLiveSettings(read_ok=True))
    return ha, homeops


async def _build(monkeypatch, *, home_quiet_hours: bool, at: datetime):
    from cortex_python.synth import vacuumops_synth

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003 — signature must match
            return at

    monkeypatch.setattr(vacuumops_synth, "datetime", _FrozenDatetime)
    ha, homeops = _mock_adapters(home_quiet_hours=home_quiet_hours)
    ctx, _, _ = await vacuumops_synth.build_snapshot("t-1", ha, homeops, MagicMock())
    return ctx


@pytest.mark.asyncio
async def test_snapshot_flags_differ_overnight(monkeypatch):
    """THE R9 FIX. At 02:00 PST the two flags must NOT be equal.

    home_context.quiet_hours is True (its window runs to 07:00) while the 1F
    courtesy window closed at 23:00. Before the split both read True and 1F was
    suppressed for the entire band that holds its usable clear windows.
    """
    ctx = await _build(monkeypatch, home_quiet_hours=True, at=_pst(9, 2, 2))
    assert ctx.quiet_hours_2f is True
    assert ctx.quiet_hours_1f is False


@pytest.mark.asyncio
async def test_snapshot_flags_agree_in_the_courtesy_hour(monkeypatch):
    """At 22:00 PST both are True — the split relaxes late night, not wind-down."""
    ctx = await _build(monkeypatch, home_quiet_hours=True, at=_pst(9, 1, 22))
    assert ctx.quiet_hours_2f is True
    assert ctx.quiet_hours_1f is True


@pytest.mark.asyncio
async def test_snapshot_flags_both_clear_during_the_day(monkeypatch):
    ctx = await _build(monkeypatch, home_quiet_hours=False, at=_pst(9, 1, 14))
    assert ctx.quiet_hours_2f is False
    assert ctx.quiet_hours_1f is False


@pytest.mark.asyncio
async def test_quiet_hours_2f_still_reads_home_context(monkeypatch):
    """2F's source is unchanged: home_context remains canonical for it.

    Asserted at a daytime clock so a regression that re-aliased 1f→2f could not
    be masked by the two happening to agree.
    """
    ctx = await _build(monkeypatch, home_quiet_hours=True, at=_pst(9, 1, 14))
    assert ctx.quiet_hours_2f is True


@pytest.mark.asyncio
async def test_quiet_hours_1f_ignores_home_context(monkeypatch):
    """1F is clock-derived and must not follow home_context.

    This is what makes it survive a home_context outage: quiet_hours_2f
    fail-opens to False when the sensor is missing, but quiet_hours_1f keeps
    working off the tick clock.
    """
    ctx = await _build(monkeypatch, home_quiet_hours=False, at=_pst(9, 1, 22))
    assert ctx.quiet_hours_2f is False
    assert ctx.quiet_hours_1f is True


@pytest.mark.asyncio
async def test_quiet_hours_1f_comes_from_the_helper(monkeypatch):
    """Guard the wiring itself, independent of the window's current bounds.

    If someone re-points quiet_hours_1f at home_context (the exact regression
    this PR undoes), this fails even if the helper's bounds are later retuned.
    """
    from cortex_python.synth import vacuumops_synth

    sentinel_calls: list[datetime] = []

    def _fake(now: datetime) -> bool:
        sentinel_calls.append(now)
        return True

    monkeypatch.setattr(vacuumops_synth, "is_quiet_hours_1f", _fake)
    ctx = await _build(monkeypatch, home_quiet_hours=False, at=_pst(9, 1, 14))
    assert len(sentinel_calls) == 1
    assert ctx.quiet_hours_1f is True
    assert ctx.quiet_hours_2f is False
