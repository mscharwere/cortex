"""Unit tests for the R1-E4 entry gate (entry_gate_check).

Supersedes test_door_gate.py. The gate used to resolve its HA entity through a
room key — ctx.zone_info[zone].room_key → ctx.rooms[key].door_open — with a
hardcoded room→entity map and an f"binary_sensor.{room}_door" naming fallback
underneath it in the synth. That shape shipped two live dispatch bugs:

  1. July 2026 (PR #40): the door fetch sat behind an early return in
     _fetch_room_activity, so any room without an occupancy sensor skipped the
     door read entirely and the gate silently no-op'd.
  2. Sep 2026: _ZONE_LABEL_TO_ROOM_KEY mapped "Master Bathroom" → "master_bath"
     while every other table used "master_bathroom". ctx.rooms.get("master_bath")
     returned None, door_open resolved to None, and the gate defaulted to
     "treat as open" — a silent dispatch into a possibly-shut room.

Both are the same defect: a key that does not match yields silence, and silence
was read as permission. The gate now reads ZoneMeta.entry_gate_entity —
populated straight off the HomeOps zones API, exactly like occupancy_sensor —
and looks the entity up in ctx.gate_readings BY ENTITY ID. No room key is
involved on this path at all, so a room-key mismatch cannot affect gating.

The four semantics pinned here, per the locked design:
  entity is None      → PASS immediately, no HA lookup ("doorless room")
  entity resolves on  → PASS  (gate does not block)
  entity resolves off → FAIL  gate_closed:<entity>
  entity unresolvable → FAIL  gate_entity_unresolved:<entity>  (never a silent pass)
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
from cortex_python.modules.vacuumops.r1 import entry_gate_check, run_r1
from cortex_python.modules.vacuumops.schemas import ZoneMeta
from cortex_python.synth.vacuumops_synth import (
    _fetch_gate_reading,
    _fetch_gate_readings,
)
from tests.unit.vacuumops.conftest import (
    make_gate,
    make_gated_zone_meta,
    make_snapshot,
)

# Sam 2F zone ids (Sam2FJob.zones / conftest zone_info)
_MASTER_BATHROOM = 1
_MASTER_BEDROOM = 2
_UPPER_HALLWAY = 3
_CARLITOS_ROOM = 4
_KIDS_TABLE_AREA = 5
_DANIEL_ROOM = 6

_MB_GATE = "binary_sensor.sam_master_bathroom_door_gate"
_BED_GATE = "binary_sensor.sam_master_bedroom_door_gate"

# The generic case Carlos's design decision exists for: not a door at all.
_MANUAL_GATE = "input_boolean.guest_mode_daniel_room_available"


@pytest.fixture
def sam_job() -> Sam2FJob:
    return Sam2FJob()


def _ctx_with_gate(
    zone_id: int,
    entity_id: str | None,
    *,
    proceed: bool = True,
    resolved: bool = True,
    raw_state: str | None = None,
    read: bool = True,
    supported: bool = True,
):
    """Snapshot where `zone_id` is gated by `entity_id` in the given state.

    read=False models a gate entity that HomeOps designates but the synth never
    landed in ctx.gate_readings (HA outage, fetch exception) — which must block,
    not pass.
    """
    ctx = make_snapshot()
    ctx.zone_metadata[zone_id] = make_gated_zone_meta(
        zone_id, entity_id, entry_gate_supported=supported
    )
    if entity_id and read:
        ctx.gate_readings[entity_id] = make_gate(
            entity_id, proceed=proceed, resolved=resolved, raw_state=raw_state
        )
    return ctx


# ── Semantic 1: no gate configured → pass, without touching HA ────────────────


def test_null_gate_entity_passes(sam_job):
    """The explicit doorless case: Upper Hallway, Kids Table Area, any 1F/3F zone."""
    ctx = _ctx_with_gate(_UPPER_HALLWAY, None)
    result, gate, reason = entry_gate_check(sam_job, _UPPER_HALLWAY, ctx)
    assert result == "PASS"
    assert gate == "none"
    assert reason == f"gate_none:{_UPPER_HALLWAY}"


def test_null_gate_entity_does_not_consult_gate_readings(sam_job):
    """No HA lookup is even attempted — a stray reading must not change the verdict."""
    ctx = _ctx_with_gate(_UPPER_HALLWAY, None)
    # A closed reading for some other entity is present; it must be irrelevant.
    ctx.gate_readings[_MB_GATE] = make_gate(_MB_GATE, proceed=False)
    result, _, reason = entry_gate_check(sam_job, _UPPER_HALLWAY, ctx)
    assert result == "PASS"
    assert reason == f"gate_none:{_UPPER_HALLWAY}"


# ── Semantic 2: gate "on" → pass ──────────────────────────────────────────────


def test_gate_on_passes(sam_job):
    ctx = _ctx_with_gate(_MASTER_BATHROOM, _MB_GATE, proceed=True)
    result, gate, reason = entry_gate_check(sam_job, _MASTER_BATHROOM, ctx)
    assert result == "PASS"
    assert gate == "none"
    assert reason == f"gate_open:{_MB_GATE}"


def test_manual_input_boolean_gate_on_passes(sam_job):
    """The gate is generic: an input_boolean gates identically to a door sensor."""
    ctx = _ctx_with_gate(_DANIEL_ROOM, _MANUAL_GATE, proceed=True)
    result, _, reason = entry_gate_check(sam_job, _DANIEL_ROOM, ctx)
    assert result == "PASS"
    assert reason == f"gate_open:{_MANUAL_GATE}"


# ── Semantic 3: gate "off" → block ────────────────────────────────────────────


def test_gate_off_blocks(sam_job):
    ctx = _ctx_with_gate(_MASTER_BATHROOM, _MB_GATE, proceed=False)
    result, gate, reason = entry_gate_check(sam_job, _MASTER_BATHROOM, ctx)
    assert result == "FAIL"
    assert gate == "effectiveness"
    assert reason == f"gate_closed:{_MB_GATE}"


def test_manual_input_boolean_gate_off_blocks(sam_job):
    """Guest Mode: a room switched off by hand blocks exactly like a shut door."""
    ctx = _ctx_with_gate(_DANIEL_ROOM, _MANUAL_GATE, proceed=False)
    result, gate, reason = entry_gate_check(sam_job, _DANIEL_ROOM, ctx)
    assert result == "FAIL"
    assert gate == "effectiveness"
    assert reason == f"gate_closed:{_MANUAL_GATE}"


# ── Semantic 4: unresolved → BLOCK LOUDLY (never a silent pass) ───────────────


@pytest.mark.parametrize("raw", ["unavailable", "unknown", "", "open", "Off "])
def test_gate_unresolvable_state_blocks(sam_job, raw):
    """Anything outside a clean on/off is unresolved — including "open".

    "open" is deliberately in this list. The old parser accepted it, which meant
    the gate's contract silently depended on the exact vocabulary of whichever
    entity happened to be wired. The contract is now on/off, full stop.
    """
    ctx = _ctx_with_gate(_MASTER_BATHROOM, _MB_GATE, resolved=False, raw_state=raw)
    result, gate, reason = entry_gate_check(sam_job, _MASTER_BATHROOM, ctx)
    assert result == "FAIL"
    assert gate == "effectiveness"
    assert reason.startswith(f"gate_entity_unresolved:{_MB_GATE}")


def test_gate_entity_missing_from_ha_blocks(sam_job):
    """Entity id points at nothing in HA (typo, renamed entity, deleted helper)."""
    ctx = _ctx_with_gate(_MASTER_BATHROOM, _MB_GATE, resolved=False, raw_state=None)
    result, _, reason = entry_gate_check(sam_job, _MASTER_BATHROOM, ctx)
    assert result == "FAIL"
    assert reason == f"gate_entity_unresolved:{_MB_GATE}:not_read"


def test_gate_never_read_blocks(sam_job):
    """HomeOps designates a gate the synth never managed to read → block."""
    ctx = _ctx_with_gate(_MASTER_BATHROOM, _MB_GATE, read=False)
    result, gate, reason = entry_gate_check(sam_job, _MASTER_BATHROOM, ctx)
    assert result == "FAIL"
    assert gate == "effectiveness"
    assert reason == f"gate_entity_unresolved:{_MB_GATE}:not_read"


def test_missing_zone_metadata_blocks(sam_job):
    """HomeOps metadata degraded → we do not know if this zone has a gate."""
    ctx = make_snapshot(zone_metadata={})
    result, gate, reason = entry_gate_check(sam_job, _MASTER_BATHROOM, ctx)
    assert result == "FAIL"
    assert gate == "effectiveness"
    assert reason == f"gate_zone_metadata_unavailable:{_MASTER_BATHROOM}"


def test_homeops_build_without_column_blocks(sam_job):
    """cortex-before-homeops deploy: absent column must NOT read as 'no gate'.

    This is the regression that would otherwise delete the entry gate across the
    whole fleet in one deploy, silently.
    """
    ctx = _ctx_with_gate(_MASTER_BATHROOM, None, supported=False)
    result, gate, reason = entry_gate_check(sam_job, _MASTER_BATHROOM, ctx)
    assert result == "FAIL"
    assert gate == "effectiveness"
    assert reason == f"gate_column_unavailable:{_MASTER_BATHROOM}"


def test_synthesized_fallback_zone_meta_blocks(sam_job):
    """l1.resolve_zone_meta's ZoneMeta(zone_id, unit_id=0) fallback must block.

    It carries entry_gate_entity=None, and without entry_gate_supported gating it
    would be indistinguishable from a genuinely gateless zone.
    """
    ctx = make_snapshot(zone_metadata={})
    fallback = ZoneMeta(zone_id=_MASTER_BATHROOM, unit_id=0)
    result, _, reason = entry_gate_check(sam_job, _MASTER_BATHROOM, ctx, fallback)
    assert result == "FAIL"
    assert reason == f"gate_column_unavailable:{_MASTER_BATHROOM}"


# ── The room-key bug class is structurally gone ───────────────────────────────


def test_gate_ignores_zone_info_room_key_entirely(sam_job):
    """The "master_bath" bug, reproduced at the source and shown to be inert.

    zone_info[1].room_key is deliberately set to a key that exists in no room
    table anywhere. Under the old resolution that yielded None → "treat as open".
    It must now have no bearing on the verdict at all.
    """
    ctx = _ctx_with_gate(_MASTER_BATHROOM, _MB_GATE, proceed=False)
    ctx.zone_info[_MASTER_BATHROOM].room_key = "master_bath_typo_that_matches_nothing"
    result, _, reason = entry_gate_check(sam_job, _MASTER_BATHROOM, ctx)
    assert result == "FAIL"
    assert reason == f"gate_closed:{_MB_GATE}"


def test_gate_works_for_zone_with_no_room_key_at_all(sam_job):
    """Kids Table Area has room_key=None and shares the master bedroom's gate.

    The old path bailed to "treat as open" on room_key=None before ever looking
    at a sensor; a shared gate now works for sub-zones like any other.
    """
    ctx = _ctx_with_gate(_KIDS_TABLE_AREA, _BED_GATE, proceed=False)
    assert ctx.zone_info[_KIDS_TABLE_AREA].room_key is None
    result, _, reason = entry_gate_check(sam_job, _KIDS_TABLE_AREA, ctx)
    assert result == "FAIL"
    assert reason == f"gate_closed:{_BED_GATE}"


def test_shared_gate_blocks_both_zones(sam_job):
    """Two zones behind one entity both block off a single reading."""
    ctx = make_snapshot()
    for zid in (_MASTER_BEDROOM, _KIDS_TABLE_AREA):
        ctx.zone_metadata[zid] = make_gated_zone_meta(zid, _BED_GATE)
    ctx.gate_readings[_BED_GATE] = make_gate(_BED_GATE, proceed=False)
    for zid in (_MASTER_BEDROOM, _KIDS_TABLE_AREA):
        result, _, reason = entry_gate_check(sam_job, zid, ctx)
        assert result == "FAIL"
        assert reason == f"gate_closed:{_BED_GATE}"


def test_gate_is_per_zone_not_per_floor(sam_job):
    """A shut gate on one zone must not leak into an ungated sibling."""
    ctx = _ctx_with_gate(_MASTER_BATHROOM, _MB_GATE, proceed=False)
    ctx.zone_metadata[_CARLITOS_ROOM] = make_gated_zone_meta(_CARLITOS_ROOM, None)
    result, _, reason = entry_gate_check(sam_job, _CARLITOS_ROOM, ctx)
    assert result == "PASS"
    assert reason == f"gate_none:{_CARLITOS_ROOM}"


# ── Job descriptors — which jobs run the gate is UNCHANGED ────────────────────


def test_door_check_flag_unchanged_across_the_fleet():
    """This PR changes how the gate resolves, never which jobs run it."""
    assert Sam2FJob().door_check is True
    assert Saros1FRoomsJob().door_check is True
    assert Saros1FLitterBoxJob().door_check is False
    assert Ethan3FLitterBoxJob().door_check is False
    assert Ethan3FRoomsJob().door_check is False


def test_gated_jobs_declare_the_renamed_rule():
    assert "entry_gate_check" in Sam2FJob().r1_rules
    assert "entry_gate_check" in Saros1FRoomsJob().r1_rules
    assert "door_open_check" not in Sam2FJob().r1_rules
    assert "door_open_check" not in Saros1FRoomsJob().r1_rules


# ── run_r1 integration — the gate fires inside the real rule chain ────────────


@pytest.mark.asyncio
async def test_run_r1_defers_when_gate_closed(sam_job, mock_redis):
    ctx = _ctx_with_gate(_MASTER_BATHROOM, _MB_GATE, proceed=False)
    result, gate, reason = await run_r1(
        sam_job,
        _MASTER_BATHROOM,
        ctx,
        mock_redis,
        zone_meta=ctx.zone_metadata[_MASTER_BATHROOM],
    )
    assert result == "FAIL"
    assert gate == "effectiveness"
    assert reason == f"gate_closed:{_MB_GATE}"


@pytest.mark.asyncio
async def test_run_r1_defers_when_gate_unresolved(sam_job, mock_redis):
    """The headline regression: an unreadable gate must defer, not dispatch."""
    ctx = _ctx_with_gate(_MASTER_BATHROOM, _MB_GATE, read=False)
    result, gate, reason = await run_r1(
        sam_job,
        _MASTER_BATHROOM,
        ctx,
        mock_redis,
        zone_meta=ctx.zone_metadata[_MASTER_BATHROOM],
    )
    assert result == "FAIL"
    assert gate == "effectiveness"
    assert reason.startswith("gate_entity_unresolved:")


@pytest.mark.asyncio
async def test_run_r1_not_gate_blocked_when_open(sam_job, mock_redis):
    """Gate open → the gate contributes no failure.

    Asserted on gate semantics rather than a bare PASS: comfort rules downstream
    may legitimately return AMBIGUOUS, which is not this test's business.
    """
    ctx = _ctx_with_gate(_MASTER_BATHROOM, _MB_GATE, proceed=True)
    _, _, reason = await run_r1(
        sam_job,
        _MASTER_BATHROOM,
        ctx,
        mock_redis,
        zone_meta=ctx.zone_metadata[_MASTER_BATHROOM],
    )
    assert "gate_closed" not in reason
    assert "gate_entity_unresolved" not in reason


@pytest.mark.asyncio
async def test_run_r1_ungated_job_never_gate_blocked(mock_redis):
    """A job with door_check=False must not acquire a gate verdict.

    The snapshot carries NO zone_metadata at all — under the gate's own rules
    that is a block, so this pins that the check is genuinely skipped for an
    ungated job rather than incidentally passing.
    """
    job = Ethan3FRoomsJob()
    ctx = make_snapshot(zone_metadata={})
    for zone_id in job.zones:
        _, _, reason = await run_r1(job, zone_id, ctx, mock_redis)
        assert "gate_" not in reason


# ── Synth wiring — entity read directly, by id, with strict polarity ──────────


def _adapter_returning(states: dict[str, str | None]) -> AsyncMock:
    async def get_entity_state(entity_id: str):
        if entity_id not in states:
            return None
        raw = states[entity_id]
        return None if raw is None else {"state": raw, "attributes": {}}

    adapter = AsyncMock()
    adapter.get_entity_state = AsyncMock(side_effect=get_entity_state)
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "proceed"), [("on", True), ("off", False), ("ON", True)]
)
async def test_synth_gate_polarity(raw, proceed):
    """on = proceed, off = block. Uniform across every entity domain."""
    reading = await _fetch_gate_reading(_adapter_returning({_MB_GATE: raw}), _MB_GATE)
    assert reading.resolved is True
    assert reading.proceed is proceed


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["unavailable", "unknown", "open", "closed", "true"])
async def test_synth_non_binary_state_is_unresolved(raw):
    """Strictness is the fix. A permissive parser is how a typo read as 'open'."""
    reading = await _fetch_gate_reading(_adapter_returning({_MB_GATE: raw}), _MB_GATE)
    assert reading.resolved is False
    assert reading.proceed is False
    assert reading.raw_state == raw


@pytest.mark.asyncio
async def test_synth_absent_entity_is_unresolved_not_open():
    """The exact shape of the old bug: absence must never resolve to 'open'."""
    reading = await _fetch_gate_reading(_adapter_returning({}), _MB_GATE)
    assert reading.resolved is False
    assert reading.proceed is False
    assert reading.raw_state is None


@pytest.mark.asyncio
async def test_synth_reads_designated_entity_and_nothing_else():
    """No naming convention, no derived entity id — only what HomeOps designated."""
    adapter = _adapter_returning({_MB_GATE: "on"})
    meta = {_MASTER_BATHROOM: make_gated_zone_meta(_MASTER_BATHROOM, _MB_GATE)}
    await _fetch_gate_readings(adapter, meta)
    queried = {call.args[0] for call in adapter.get_entity_state.call_args_list}
    assert queried == {_MB_GATE}
    # The convention the old synth would have guessed at.
    assert "binary_sensor.master_bathroom_door" not in queried


@pytest.mark.asyncio
async def test_synth_dedupes_shared_gate_entities():
    """Two zones behind one gate cost one HA call."""
    adapter = _adapter_returning({_BED_GATE: "on"})
    meta = {
        _MASTER_BEDROOM: make_gated_zone_meta(_MASTER_BEDROOM, _BED_GATE),
        _KIDS_TABLE_AREA: make_gated_zone_meta(_KIDS_TABLE_AREA, _BED_GATE),
    }
    out = await _fetch_gate_readings(adapter, meta)
    assert set(out) == {_BED_GATE}
    assert adapter.get_entity_state.await_count == 1


@pytest.mark.asyncio
async def test_synth_skips_zones_with_no_gate():
    """A gateless zone costs zero HA calls."""
    adapter = _adapter_returning({})
    meta = {_UPPER_HALLWAY: make_gated_zone_meta(_UPPER_HALLWAY, None)}
    out = await _fetch_gate_readings(adapter, meta)
    assert out == {}
    assert adapter.get_entity_state.await_count == 0


@pytest.mark.asyncio
async def test_synth_fetch_exception_lands_as_unresolved():
    """An HA failure parks the gated zones; it must not erase their gates."""
    adapter = AsyncMock()
    adapter.get_entity_state = AsyncMock(side_effect=RuntimeError("HA down"))
    meta = {_MASTER_BATHROOM: make_gated_zone_meta(_MASTER_BATHROOM, _MB_GATE)}
    out = await _fetch_gate_readings(adapter, meta)
    assert out[_MB_GATE].resolved is False
    assert out[_MB_GATE].proceed is False


# ── The deleted machinery must stay deleted ───────────────────────────────────


def test_no_hardcoded_door_map_or_name_convention_survives():
    """Guards against the map/fallback creeping back in under any name."""
    import cortex_python.synth.vacuumops_synth as synth

    assert not hasattr(synth, "_DOOR_ENTITY_MAP")
    source = open(synth.__file__, encoding="utf-8").read()
    assert '_door"' not in source
    assert "_door_gate" not in source


def test_room_activity_no_longer_carries_door_state():
    """Entry gating must not be reachable through a room-keyed structure."""
    from cortex_python.modules.vacuumops.schemas import RoomActivity

    assert "door_open" not in RoomActivity.__dataclass_fields__
