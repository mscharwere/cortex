"""Unit tests for the R1-E4 door gate on the Saros 1F Rooms job.

Regression coverage for the "Saros dispatched into a closed Bathroom" bug
(Notion 3ba78067-86b1-81ac-898f-ce23fbd4afbe). Two independent defects had to
line up for the robot to be sent into a shut room:

  1. Saros1FRoomsJob never set door_check=True, so door_open_check never ran.
  2. Even with the flag on, "bathroom" had no entry in _DOOR_ENTITY_MAP, so the
     synth fell back to binary_sensor.bathroom_door (does not exist in HA) and
     door_open resolved to None → "treat as open" → gate silently no-op'd.

These tests pin both halves, plus the blast-radius claim that enabling the
job-wide flag leaves the other five Saros zones structurally unaffected.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from cortex_python.modules.vacuumops.jobs import (
    Ethan3FLitterBoxJob,
    Ethan3FRoomsJob,
    Sam2FJob,
    Saros1FLitterBoxJob,
    Saros1FRoomsJob,
)
from cortex_python.modules.vacuumops.r1 import door_open_check, run_r1
from cortex_python.synth.vacuumops_synth import _DOOR_ENTITY_MAP, _fetch_room_activity
from tests.unit.vacuumops.conftest import make_room, make_snapshot

# Saros 1F zone ids (see Saros1FRoomsJob.zones / conftest zone_info)
_BATHROOM = 20
_KITCHEN = 19
_LIVING_ROOM = 21
_HALLWAY = 22
_PREP_AREA = 24  # room_key=None (sub-zone)
_DINING_TABLE = 25

# Every Saros room zone that is NOT the Bathroom. These must be untouched by
# door_check=True — none of them has a door sensor wired.
_NON_BATHROOM_ZONES = [_KITCHEN, _LIVING_ROOM, _HALLWAY, _PREP_AREA, _DINING_TABLE]


@pytest.fixture
def saros_rooms_job() -> Saros1FRoomsJob:
    return Saros1FRoomsJob()


def _ctx_with_bathroom_door(door_open: bool | None):
    """Clean 1F snapshot whose bathroom carries the given door state."""
    ctx = make_snapshot()
    ctx.rooms["bathroom"] = make_room("idle", door_open=door_open)
    return ctx


# ── Job descriptor ────────────────────────────────────────────────────────────


def test_saros_rooms_job_enables_door_check(saros_rooms_job):
    """The flag that was missing. Without this the gate never runs at all."""
    assert saros_rooms_job.door_check is True


def test_saros_rooms_job_declares_door_rule(saros_rooms_job):
    assert "door_open_check" in saros_rooms_job.r1_rules


def test_door_check_not_enabled_on_unrelated_jobs():
    """door_check stays off everywhere it was off before — Sam is the only other user."""
    assert Saros1FLitterBoxJob().door_check is False
    assert Ethan3FLitterBoxJob().door_check is False
    assert Ethan3FRoomsJob().door_check is False
    # Sam's pre-existing gate must not regress.
    assert Sam2FJob().door_check is True


# ── door_open_check — Bathroom (zone 20) ──────────────────────────────────────


def test_bathroom_door_closed_fails_gate(saros_rooms_job):
    """The actual bug: closed door must defer, not dispatch."""
    ctx = _ctx_with_bathroom_door(False)
    result, gate, reason = door_open_check(saros_rooms_job, _BATHROOM, ctx)
    assert result == "FAIL"
    assert gate == "effectiveness"
    assert reason == f"door_closed:{_BATHROOM}"


def test_bathroom_door_open_passes_gate(saros_rooms_job):
    ctx = _ctx_with_bathroom_door(True)
    result, gate, reason = door_open_check(saros_rooms_job, _BATHROOM, ctx)
    assert result == "PASS"
    assert gate == "none"
    assert reason == f"door_open:{_BATHROOM}"


def test_bathroom_missing_sensor_treated_as_open(saros_rooms_job):
    """Graceful degradation — a dead sensor must not strand the zone forever."""
    ctx = _ctx_with_bathroom_door(None)
    result, _, reason = door_open_check(saros_rooms_job, _BATHROOM, ctx)
    assert result == "PASS"
    assert reason == f"door_sensor_unavailable_treat_open:{_BATHROOM}"


# ── door_open_check — blast radius on the other Saros zones ───────────────────


@pytest.mark.parametrize("zone_id", _NON_BATHROOM_ZONES)
def test_other_saros_zones_unaffected_by_door_check(saros_rooms_job, zone_id):
    """Enabling the job-wide flag must not gate zones with no door sensor.

    Prep Area has room_key=None; the rest have rooms whose door_open is None
    because no binary_sensor.{room}_door exists in HA. Both paths no-op to PASS.
    """
    ctx = _ctx_with_bathroom_door(False)  # bathroom shut — must not leak sideways
    result, gate, reason = door_open_check(saros_rooms_job, zone_id, ctx)
    assert result == "PASS"
    assert gate == "none"
    assert "door_closed" not in reason


# ── run_r1 integration — the gate actually fires in the real rule chain ───────


@pytest.mark.asyncio
async def test_run_r1_defers_bathroom_when_door_closed(saros_rooms_job, mock_redis):
    ctx = _ctx_with_bathroom_door(False)
    result, gate, reason = await run_r1(saros_rooms_job, _BATHROOM, ctx, mock_redis)
    assert result == "FAIL"
    assert gate == "effectiveness"
    assert reason == f"door_closed:{_BATHROOM}"


@pytest.mark.asyncio
async def test_run_r1_bathroom_not_door_blocked_when_open(saros_rooms_job, mock_redis):
    """Door open → the door gate contributes no failure.

    Asserted on the door semantics rather than a bare PASS: the comfort rules
    downstream may legitimately return AMBIGUOUS, and that is not this test's
    business.
    """
    ctx = _ctx_with_bathroom_door(True)
    _, _, reason = await run_r1(saros_rooms_job, _BATHROOM, ctx, mock_redis)
    assert "door_closed" not in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("zone_id", _NON_BATHROOM_ZONES)
async def test_run_r1_other_zones_never_door_blocked(
    saros_rooms_job, mock_redis, zone_id
):
    ctx = _ctx_with_bathroom_door(False)
    _, _, reason = await run_r1(saros_rooms_job, zone_id, ctx, mock_redis)
    assert "door_closed" not in reason


# ── Synth wiring — the entity actually resolves ───────────────────────────────


def test_bathroom_mapped_to_canonical_door_entity():
    """Pins the exact entity. The inverted '..._is_closed' siblings must never
    be wired here — they report on=CLOSED and would invert the whole gate."""
    assert (
        _DOOR_ENTITY_MAP["bathroom"] == "binary_sensor.first_level_bathroom_door_sensor"
    )
    assert not _DOOR_ENTITY_MAP["bathroom"].endswith("_closed")


def _ha_adapter_returning(door_state: str | None) -> AsyncMock:
    """HA adapter stub: bathroom idle/unoccupied, door reporting door_state."""

    async def get_entity_state(entity_id: str):
        if entity_id == "binary_sensor.bathroom_occupancy_status":
            return {"state": "off", "attributes": {}}
        if entity_id == "sensor.bathroom_detected_activity":
            return {"state": "unoccupied", "attributes": {"probability": 0.9}}
        if entity_id == _DOOR_ENTITY_MAP["bathroom"]:
            return (
                None if door_state is None else {"state": door_state, "attributes": {}}
            )
        return None

    adapter = AsyncMock()
    adapter.get_entity_state = AsyncMock(side_effect=get_entity_state)
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ha_state", "expected"),
    [("on", True), ("off", False), ("open", True)],
)
async def test_synth_resolves_bathroom_door_polarity(ha_state, expected):
    """on/open = door open. This is the polarity door_open_check contracts on."""
    adapter = _ha_adapter_returning(ha_state)
    room = await _fetch_room_activity(adapter, "bathroom")
    assert room is not None
    assert room.door_open is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("ha_state", ["unavailable", "unknown", None])
async def test_synth_bathroom_door_degrades_to_none(ha_state):
    """Unavailable sensor → None → door_open_check treats the zone as open."""
    adapter = _ha_adapter_returning(ha_state)
    room = await _fetch_room_activity(adapter, "bathroom")
    assert room is not None
    assert room.door_open is None


@pytest.mark.asyncio
async def test_synth_queries_the_mapped_entity_not_the_default():
    """Regression: the fallback binary_sensor.bathroom_door does not exist in HA.

    Querying it was defect #2 — door_open came back None and the gate no-op'd.
    """
    adapter = _ha_adapter_returning("off")
    await _fetch_room_activity(adapter, "bathroom")
    queried = {call.args[0] for call in adapter.get_entity_state.call_args_list}
    assert "binary_sensor.first_level_bathroom_door_sensor" in queried
    assert "binary_sensor.bathroom_door" not in queried
