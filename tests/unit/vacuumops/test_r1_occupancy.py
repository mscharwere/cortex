"""Unit tests for the R1 occupancy-gate hardening (2026-08-31 incident).

The Saros 10R dispatched into occupied 1F rooms three times on 2026-08-31
(10:07:19, 14:06:47, 18:34:34 PST). The occupancy gates were intact and had
been since the module's first commit; three independent gaps let the dispatches
through anyway:

  1. zone occupancy was resolved by naming convention off zone_info.room_key,
     which silently degraded to "treat as clear" for any zone with no matching
     HA entity  → precedence chain onto ZoneMeta.occupancy_sensor + floor fallback
  2. floor clearance was re-derived by OR-ing the per-room sensors in
     FLOOR_ROOM_MAP, several of which do not exist
                → dedicated area_occupancy floor rollup as the primary signal
  3. an instantaneous "off" reading was trusted with no confirmation window
                → one-directional occupancy_clear_grace_s

Entity existence in these tests mirrors live HA as verified 2026-08-31.
No live Redis/DB/HA calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cortex_python.modules.vacuumops.jobs import Sam2FJob, Saros1FRoomsJob
from cortex_python.modules.vacuumops.r1 import (
    floor_clearance_check,
    occupancy_state,
    run_r1,
    zone_active_use_check,
)
from cortex_python.modules.vacuumops.schemas import ZoneMeta
from tests.unit.vacuumops.conftest import make_occupancy, make_room, make_snapshot

_NOW = datetime(2026, 5, 24, 15, 0, 0, tzinfo=timezone.utc)

# Zone 25 = Dining Table. zone_info.room_key is "dining_room", but HA has no
# binary_sensor.dining_room_occupancy_status at all — verified live 2026-08-31.
# Its occupancy gate was therefore a permanent no-op before this change.
_DINING_ZONE = 25
# Zone 19 = Kitchen — backed by a real binary_sensor.kitchen_occupancy_status.
_KITCHEN_ZONE = 19
# Zone 22 = Hallway. Zone 6 = Daniel's Room (Sam, 2F).
_HALLWAY_ZONE = 22
_DANIEL_ZONE = 6

# Designated occupancy_sensor entities, as stored in HomeOps vac_zone_cleanliness.
# Both are live entities that no naming convention recovers from the room key.
_DINING_SENSOR = "binary_sensor.emotion_kitchen_dining_table_presence"
_KITCHEN_SENSOR = "binary_sensor.emotion_kitchen_prep_sink_area_presence"


def _meta(zone_id: int, sensor: str | None) -> ZoneMeta:
    return ZoneMeta(zone_id=zone_id, unit_id=4, occupancy_sensor=sensor)


def _ago(seconds: float) -> datetime:
    return _NOW - timedelta(seconds=seconds)


def _saros_ctx(**kw: object):
    """A 1F snapshot whose sensor availability matches live HA.

    dining_room and prep_area are present in ctx.rooms (the synth always
    populates every tracked room so templates never hit StrictUndefined) but
    have no backing entity, so occupancy_available is False.
    """
    ctx = make_snapshot(timestamp=_NOW, **kw)  # type: ignore[arg-type]
    ctx.rooms["dining_room"] = make_room("unknown", 0.0, occupancy_available=False)
    ctx.rooms["prep_area"] = make_room("unknown", 0.0, occupancy_available=False)
    return ctx


def _floor_1f(occupied: bool, clear_for_s: float = 3600.0):
    return make_occupancy(
        "binary_sensor.first_floor_occupancy_status",
        occupied=occupied,
        last_changed=_ago(clear_for_s),
    )


# ── occupancy_state — the confirmation-window primitive ───────────────────────


def test_occupancy_state_occupied_is_immediate():
    """Flipping TO occupied blocks instantly — the window is one-directional."""
    state, elapsed = occupancy_state(True, _ago(0), _NOW, grace_s=120)
    assert state == "occupied"
    assert elapsed is None


def test_occupancy_state_fresh_clear_is_unconfirmed():
    state, elapsed = occupancy_state(False, _ago(30), _NOW, grace_s=120)
    assert state == "clearing"
    assert elapsed == pytest.approx(30.0)


def test_occupancy_state_settled_clear_is_clear():
    state, elapsed = occupancy_state(False, _ago(300), _NOW, grace_s=120)
    assert state == "clear"
    assert elapsed == pytest.approx(300.0)


def test_occupancy_state_boundary_is_inclusive():
    """Exactly grace_s of dwell counts as confirmed clear."""
    assert occupancy_state(False, _ago(120), _NOW, grace_s=120)[0] == "clear"
    assert occupancy_state(False, _ago(119), _NOW, grace_s=120)[0] == "clearing"


def test_occupancy_state_unknown_dwell_degrades_to_clear():
    """No timestamp → dwell unknown → never MORE restrictive than pre-grace."""
    assert occupancy_state(False, None, _NOW, grace_s=120)[0] == "clear"


def test_occupancy_state_zero_grace_disables_window():
    assert occupancy_state(False, _ago(1), _NOW, grace_s=0)[0] == "clear"


def test_occupancy_state_naive_timestamp_treated_as_utc():
    """A naive last_changed must not raise; it is assumed UTC."""
    naive = _NOW.replace(tzinfo=None) - timedelta(seconds=10)
    assert occupancy_state(False, naive, _NOW, grace_s=120)[0] == "clearing"


# ── Fix 1: occupancy resolved via ZoneMeta.occupancy_sensor ───────────────────


def test_zone_active_use_reads_designated_zone_sensor():
    """Tier 1: the entity HomeOps designated for the zone is read directly.

    Dining Table's designated sensor is
    binary_sensor.emotion_kitchen_dining_table_presence — a live entity that no
    naming convention recovers, and one the old room_key round-trip never read.
    """
    ctx = _saros_ctx()
    ctx.occupancy_readings[_DINING_SENSOR] = make_occupancy(
        _DINING_SENSOR, occupied=True, last_changed=_ago(60)
    )
    result, gate, reason = zone_active_use_check(
        Saros1FRoomsJob(), _DINING_ZONE, ctx, _meta(_DINING_ZONE, _DINING_SENSOR)
    )
    assert result == "FAIL"
    assert gate == "effectiveness"
    assert "emotion_kitchen_dining_table_presence" in reason


def test_zone_sensor_takes_precedence_over_room_sensor():
    """Tier 1 beats tier 2: the designated sensor wins over a clear room sensor."""
    ctx = _saros_ctx()
    ctx.rooms["kitchen"] = make_room("idle", raw_occupancy=False)
    ctx.occupancy_readings[_KITCHEN_SENSOR] = make_occupancy(
        _KITCHEN_SENSOR, occupied=True, last_changed=_ago(300)
    )
    result, _, reason = zone_active_use_check(
        Saros1FRoomsJob(), _KITCHEN_ZONE, ctx, _meta(_KITCHEN_ZONE, _KITCHEN_SENSOR)
    )
    assert result == "FAIL"
    assert "prep_sink_area_presence" in reason


def test_zone_falls_back_to_floor_when_designated_sensor_unset():
    """Tier 3: no zone sensor AND no working room sensor → floor rollup blocks.

    This is the Dining Table regression itself. Saros zones carry a NULL
    occupancy_sensor in HomeOps and dining_room has no HA entity, so before this
    change the gate returned "treat clear" unconditionally.
    """
    ctx = _saros_ctx()
    ctx.floor_occupancy["1F"] = _floor_1f(occupied=True)
    result, gate, reason = zone_active_use_check(Saros1FRoomsJob(), _DINING_ZONE, ctx)
    assert result == "FAIL"
    assert gate == "effectiveness"
    assert "floor_fallback_occupied" in reason
    assert "1F" in reason


def test_zone_floor_fallback_passes_when_floor_clear():
    ctx = _saros_ctx()
    ctx.floor_occupancy["1F"] = _floor_1f(occupied=False)
    result, _, reason = zone_active_use_check(Saros1FRoomsJob(), _DINING_ZONE, ctx)
    assert result == "PASS"
    assert "floor:1F" in reason


def test_zone_room_tier_retained_for_rooms_with_real_sensors():
    """Tier 2 survives: Sam's per-room model keeps room-level precision.

    Consolidating every zone straight onto the floor rollup would silently
    collapse effectiveness_scope="room_only" into a floor-wide gate. A room with
    a working sensor is still resolved at room level.
    """
    ctx = make_snapshot(timestamp=_NOW)
    ctx.rooms["daniel_room"] = make_room("idle", raw_occupancy=True)
    ctx.floor_occupancy["2F"] = make_occupancy(
        "binary_sensor.second_floor_occupancy_status",
        occupied=False,
        last_changed=_ago(3600),
    )
    result, _, reason = zone_active_use_check(Sam2FJob(), _DANIEL_ZONE, ctx)
    assert result == "FAIL"
    assert f"zone_occupied:{_DANIEL_ZONE}" in reason


def test_zone_unavailable_room_does_not_read_as_clear():
    """A room whose sensor does not exist must not satisfy the gate by itself."""
    ctx = _saros_ctx()
    ctx.floor_occupancy["1F"] = _floor_1f(occupied=True)
    # dining_room is present with raw_occupancy=False, but that is a placeholder
    # default rather than evidence of an empty room.
    assert ctx.rooms["dining_room"].raw_occupancy is False
    assert ctx.rooms["dining_room"].occupancy_available is False
    result, _, _ = zone_active_use_check(Saros1FRoomsJob(), _DINING_ZONE, ctx)
    assert result == "FAIL"


def test_zone_no_signal_anywhere_still_degrades_to_clear():
    """Tier 4: genuinely no zone, room or floor signal → PASS (spec §8.5)."""
    ctx = _saros_ctx()
    result, gate, reason = zone_active_use_check(Saros1FRoomsJob(), _DINING_ZONE, ctx)
    assert result == "PASS"
    assert gate == "none"
    assert "unavailable" in reason


def test_zone_detected_activity_still_blocks_via_floor_tier():
    """detected_activity remains an independent block regardless of which tier ran."""
    ctx = _saros_ctx()
    ctx.floor_occupancy["1F"] = _floor_1f(occupied=False)
    ctx.rooms["dining_room"] = make_room(
        "eating", 0.9, raw_occupancy=False, occupancy_available=False
    )
    result, _, reason = zone_active_use_check(Saros1FRoomsJob(), _DINING_ZONE, ctx)
    assert result == "FAIL"
    assert "activity=eating" in reason


# ── Fix 2: floor_clearance_check reads the dedicated floor entity ─────────────


def test_floor_clearance_reads_dedicated_floor_entity():
    """The rollup blocks even when every per-room sensor reads clear.

    Proves the dedicated entity is the signal rather than a re-derivation of the
    rooms: under the old FLOOR_ROOM_MAP OR this snapshot would have passed.
    """
    ctx = _saros_ctx()
    ctx.floor_occupancy["1F"] = _floor_1f(occupied=True, clear_for_s=600)
    result, gate, reason = floor_clearance_check(Saros1FRoomsJob(), _KITCHEN_ZONE, ctx)
    assert result == "FAIL"
    assert gate == "effectiveness"
    assert "first_floor_occupancy_status" in reason


def test_floor_clearance_passes_when_rollup_and_rooms_clear():
    ctx = _saros_ctx()
    ctx.floor_occupancy["1F"] = _floor_1f(occupied=False)
    result, _, _ = floor_clearance_check(Saros1FRoomsJob(), _KITCHEN_ZONE, ctx)
    assert result == "PASS"


def test_floor_clearance_room_sweep_is_a_secondary_net():
    """A positively-occupied room still blocks even if the rollup reads clear.

    Positive room occupancy is real evidence, never a silent default, so the
    sweep is kept alongside the rollup — it can only add a block, never suppress
    one. It is the *absence* of a room sensor that the rollup now covers.
    """
    ctx = _saros_ctx()
    ctx.floor_occupancy["1F"] = _floor_1f(occupied=False)
    ctx.rooms["living_room"] = make_room("active", raw_occupancy=True)
    result, _, reason = floor_clearance_check(Saros1FRoomsJob(), _KITCHEN_ZONE, ctx)
    assert result == "FAIL"
    assert "living_room" in reason


def test_floor_clearance_degrades_to_room_sweep_when_rollup_unavailable():
    """A dead area_occupancy integration falls back to the old behaviour."""
    ctx = _saros_ctx()
    ctx.floor_occupancy["1F"] = make_occupancy(
        "binary_sensor.first_floor_occupancy_status", available=False
    )
    ctx.rooms["kitchen"] = make_room("active", raw_occupancy=True)
    result, _, reason = floor_clearance_check(Saros1FRoomsJob(), _KITCHEN_ZONE, ctx)
    assert result == "FAIL"
    assert "kitchen" in reason


def test_floor_clearance_uses_the_jobs_own_floor():
    """A 2F job reads the 2F rollup, not the 1F one."""
    ctx = make_snapshot(timestamp=_NOW)
    ctx.floor_occupancy["1F"] = _floor_1f(occupied=True)
    ctx.floor_occupancy["2F"] = make_occupancy(
        "binary_sensor.second_floor_occupancy_status",
        occupied=False,
        last_changed=_ago(3600),
    )
    # Sam2FJob is room_only, but floor_clearance_check is floor-addressed either way.
    result, _, _ = floor_clearance_check(Sam2FJob(), _DANIEL_ZONE, ctx)
    assert result == "PASS"


# ── Fix 3: the confirmation window, end to end through the gates ─────────────


def test_floor_just_flipped_off_is_gated():
    """The 18:34:34 dispatch: 1F read off only 56s before the tick → defer."""
    ctx = _saros_ctx()
    ctx.floor_occupancy["1F"] = _floor_1f(occupied=False, clear_for_s=56)
    result, gate, reason = floor_clearance_check(Saros1FRoomsJob(), _KITCHEN_ZONE, ctx)
    assert result == "FAIL"
    assert gate == "effectiveness"
    assert "unconfirmed" in reason
    assert "clear_for=56s<120s" in reason


def test_floor_clear_long_enough_passes():
    ctx = _saros_ctx()
    ctx.floor_occupancy["1F"] = _floor_1f(occupied=False, clear_for_s=121)
    result, _, _ = floor_clearance_check(Saros1FRoomsJob(), _KITCHEN_ZONE, ctx)
    assert result == "PASS"


def test_floor_flip_to_occupied_has_zero_added_latency():
    """No grace in the occupying direction — 1s of occupancy blocks immediately."""
    ctx = _saros_ctx()
    ctx.floor_occupancy["1F"] = _floor_1f(occupied=True, clear_for_s=1)
    result, _, reason = floor_clearance_check(Saros1FRoomsJob(), _KITCHEN_ZONE, ctx)
    assert result == "FAIL"
    assert "unconfirmed" not in reason
    assert "floor_not_clear" in reason


def test_zone_just_flipped_off_is_gated():
    ctx = _saros_ctx()
    ctx.occupancy_readings[_KITCHEN_SENSOR] = make_occupancy(
        _KITCHEN_SENSOR, occupied=False, last_changed=_ago(10)
    )
    result, gate, reason = zone_active_use_check(
        Saros1FRoomsJob(), _KITCHEN_ZONE, ctx, _meta(_KITCHEN_ZONE, _KITCHEN_SENSOR)
    )
    assert result == "FAIL"
    assert gate == "effectiveness"
    assert "zone_occupancy_unconfirmed" in reason


def test_zone_room_tier_just_flipped_off_is_gated():
    ctx = make_snapshot(timestamp=_NOW)
    ctx.rooms["daniel_room"] = make_room(
        "idle", raw_occupancy=False, occupancy_last_changed=_ago(5)
    )
    result, _, reason = zone_active_use_check(Sam2FJob(), _DANIEL_ZONE, ctx)
    assert result == "FAIL"
    assert "zone_occupancy_unconfirmed" in reason


def test_zone_flip_to_occupied_has_zero_added_latency():
    ctx = _saros_ctx()
    ctx.occupancy_readings[_KITCHEN_SENSOR] = make_occupancy(
        _KITCHEN_SENSOR, occupied=True, last_changed=_ago(1)
    )
    result, _, reason = zone_active_use_check(
        Saros1FRoomsJob(), _KITCHEN_ZONE, ctx, _meta(_KITCHEN_ZONE, _KITCHEN_SENSOR)
    )
    assert result == "FAIL"
    assert "unconfirmed" not in reason


def test_grace_disabled_restores_instantaneous_trust():
    """occupancy_clear_grace_s=0 is the documented escape hatch."""
    ctx = _saros_ctx()
    ctx.floor_occupancy["1F"] = _floor_1f(occupied=False, clear_for_s=1)
    result, _, _ = floor_clearance_check(
        Saros1FRoomsJob(occupancy_clear_grace_s=0), _KITCHEN_ZONE, ctx
    )
    assert result == "PASS"


def test_all_occupancy_jobs_default_to_120s():
    """Every job that runs occupancy gates carries the window explicitly."""
    from cortex_python.modules.vacuumops.jobs import Ethan3FRoomsJob, Saros1FLitterBoxJob

    for job in (Saros1FRoomsJob(), Saros1FLitterBoxJob(), Ethan3FRoomsJob(), Sam2FJob()):
        assert job.occupancy_clear_grace_s == 120, job.job_id
        assert job.effectiveness_scope in ("floor", "room_only"), job.job_id


# ── Regression: the three 2026-08-31 dispatches, end to end through run_r1 ────


@pytest.mark.parametrize(
    ("label", "clear_for_s"),
    [
        ("10:07:19", 2),  # sensor read off 2s before the tick
        ("14:06:47", 90),  # off 90s before the tick
        ("18:34:34", 56),  # living_room off 18:33:38, dispatch 18:34:34, back on 18:35:01
    ],
)
@pytest.mark.asyncio
async def test_2026_08_31_dispatches_now_defer(mock_redis, label, clear_for_s):
    """All three real dispatches would have deferred under the 120s window."""
    ctx = _saros_ctx()
    ctx.floor_occupancy["1F"] = _floor_1f(occupied=False, clear_for_s=clear_for_s)
    result, gate, reason = await run_r1(
        Saros1FRoomsJob(), _KITCHEN_ZONE, ctx, mock_redis
    )
    assert result == "FAIL", f"{label} should defer, got {reason}"
    assert gate == "effectiveness"
    assert "unconfirmed" in reason


@pytest.mark.asyncio
async def test_settled_clear_floor_still_dispatches(mock_redis):
    """The window must not wedge the robot shut: a genuinely empty floor passes."""
    ctx = _saros_ctx()
    ctx.floor_occupancy["1F"] = _floor_1f(occupied=False, clear_for_s=1800)
    result, _, reason = await run_r1(Saros1FRoomsJob(), _KITCHEN_ZONE, ctx, mock_redis)
    assert result == "PASS", reason


@pytest.mark.asyncio
async def test_room_scoped_override_also_honours_the_window(mock_redis):
    """Override 2's relaxed room check is still a gate — same confirmation window."""
    meta = ZoneMeta(
        zone_id=_HALLWAY_ZONE,
        unit_id=4,
        occupancy_sensor="binary_sensor.hallway_sensor_group",
        low_disruption=True,
    )
    ctx = _saros_ctx(home_count=1, who_home=["Carlos"])
    ctx.rooms["hallway"] = make_room(
        "idle", raw_occupancy=False, occupancy_last_changed=_ago(15)
    )
    result, gate, reason = await run_r1(
        Saros1FRoomsJob(),
        _HALLWAY_ZONE,
        ctx,
        mock_redis,
        zone_meta=meta,
        bypass_mode="room_scoped",
        bypass_reason_str="single_person_low_disruption",
    )
    assert result == "FAIL"
    assert gate == "effectiveness"
    assert "target_room_occupancy_unconfirmed" in reason


# ══════════════════════════════════════════════════════════════════════════════
# Synth-layer parsing — the source of last_changed and availability.
#
# This is the layer that can silently neuter the whole confirmation window: if
# _parse_last_changed ever returns None for a real payload, occupancy_state
# degrades every reading to "clear" and Fix 3 becomes a no-op. Payload shapes
# below are copied from live HA responses captured 2026-08-31.
# ══════════════════════════════════════════════════════════════════════════════

from unittest.mock import AsyncMock  # noqa: E402

from cortex_python.synth.vacuumops_synth import (  # noqa: E402
    _fetch_occupancy_reading,
    _fetch_occupancy_readings,
    _parse_last_changed,
)

# Verbatim from GET /api/states/binary_sensor.first_floor_occupancy_status.
_LIVE_FLOOR_PAYLOAD = {
    "entity_id": "binary_sensor.first_floor_occupancy_status",
    "state": "on",
    "attributes": {"device_class": "occupancy", "friendly_name": "First floor Occupancy Status"},
    "last_changed": "2026-08-31T18:35:01.713237-07:00",
    "last_reported": "2026-08-31T18:35:01.713237-07:00",
    "last_updated": "2026-08-31T18:35:01.713237-07:00",
}


def test_parse_last_changed_on_live_payload():
    """The real HA shape must parse — a None here silently disables the window."""
    dt = _parse_last_changed(_LIVE_FLOOR_PAYLOAD)
    assert dt is not None
    assert dt.tzinfo is not None
    # 18:35:01 PDT (-07:00) == 01:35:01 UTC the next day
    assert dt.isoformat() == "2026-09-01T01:35:01.713237+00:00"


def test_parse_last_changed_handles_z_suffix_and_naive():
    assert _parse_last_changed({"last_changed": "2026-08-31T18:35:01Z"}) is not None
    naive = _parse_last_changed({"last_changed": "2026-08-31T18:35:01"})
    assert naive is not None and naive.tzinfo is not None


@pytest.mark.parametrize("payload", [None, {}, {"last_changed": None}, {"last_changed": "junk"}])
def test_parse_last_changed_degrades_to_none(payload):
    assert _parse_last_changed(payload) is None


@pytest.mark.asyncio
async def test_fetch_occupancy_reading_live_payload():
    ha = AsyncMock()
    ha.get_entity_state = AsyncMock(return_value=_LIVE_FLOOR_PAYLOAD)
    reading = await _fetch_occupancy_reading(ha, "binary_sensor.first_floor_occupancy_status")
    assert reading.available is True
    assert reading.occupied is True
    assert reading.last_changed is not None


@pytest.mark.asyncio
async def test_fetch_occupancy_reading_missing_entity_is_unavailable_not_clear():
    """The whole point: a missing entity must NOT read as an empty room."""
    ha = AsyncMock()
    ha.get_entity_state = AsyncMock(return_value=None)
    reading = await _fetch_occupancy_reading(ha, "binary_sensor.dining_room_occupancy_status")
    assert reading.available is False
    assert reading.occupied is False  # value is meaningless while available is False


@pytest.mark.asyncio
async def test_fetch_zone_occupancy_dedupes_shared_sensors():
    """Zones legitimately share sensors — fetch each entity once, not once per zone."""
    ha = AsyncMock()
    ha.get_entity_state = AsyncMock(return_value={"state": "off", "last_changed": None})
    shared = "binary_sensor.emotion_kitchen_dining_table_presence"
    metadata = {
        19: ZoneMeta(zone_id=19, unit_id=4, occupancy_sensor=shared),
        25: ZoneMeta(zone_id=25, unit_id=4, occupancy_sensor=shared),
        24: ZoneMeta(zone_id=24, unit_id=4, occupancy_sensor=None),  # unset → skipped
    }
    out = await _fetch_occupancy_readings(ha, metadata)
    assert set(out) == {shared}
    assert ha.get_entity_state.await_count == 1


@pytest.mark.asyncio
async def test_fetch_zone_occupancy_empty_metadata():
    ha = AsyncMock()
    ha.get_entity_state = AsyncMock(return_value=None)
    assert await _fetch_occupancy_readings(ha, {}) == {}
    assert ha.get_entity_state.await_count == 0
