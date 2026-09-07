"""Hydration tests for HomeOpsAdapter.get_zone_metadata() + the room-key table.

Two things are pinned here, both because a silent mismatch in either one is
invisible at runtime:

  1. entry_gate_entity / entry_gate_supported hydrate off GET /api/vacuum/zones
     the same way occupancy_sensor does, and feature detection keys on the
     PRESENCE of the field rather than its value. A HomeOps build predating the
     column omits the key; reading that as "no gate configured" would silently
     disable the entry gate fleet-wide on a cortex-before-homeops deploy.

  2. Every value in _ZONE_LABEL_TO_ROOM_KEY names a room the synth actually
     tracks. "Master Bathroom" pointed at "master_bath" while the tracked room
     was "master_bathroom"; ctx.rooms.get() returned None, and every consumer
     degraded quietly. Nothing anywhere raised.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cortex_python.adapters.homeops_adapter import (
    _ZONE_LABEL_TO_ROOM_KEY,
    HomeOpsAdapter,
)
from cortex_python.synth.vacuumops_synth import _TRACKED_ROOMS


# ── The room-key table ────────────────────────────────────────────────────────


def test_every_mapped_room_key_is_a_tracked_room():
    """The "master_bath" class of bug, caught at the table rather than in prod."""
    mapped = {v for v in _ZONE_LABEL_TO_ROOM_KEY.values() if v is not None}
    unknown = sorted(mapped - set(_TRACKED_ROOMS))
    assert unknown == [], f"room keys not in _TRACKED_ROOMS: {unknown}"


def test_master_bathroom_maps_to_the_tracked_room_key():
    """Explicit regression pin on the specific typo found 2026-09-07."""
    assert _ZONE_LABEL_TO_ROOM_KEY["Master Bathroom"] == "master_bathroom"


# ── entry_gate hydration ──────────────────────────────────────────────────────


def _adapter() -> HomeOpsAdapter:
    settings = MagicMock()
    settings.homeops_base_url = "http://homeops.test"
    settings.cortex_api_key = "test-key"
    return HomeOpsAdapter(settings)


def _zones_payload(*zones: dict) -> dict:
    return {"data": list(zones)}


async def _get_zone_metadata(payload: dict):
    adapter = _adapter()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=payload)

    client = MagicMock()

    async def _get(_path):
        return response

    client.get = _get

    class _Ctx:
        async def __aenter__(self):
            return client

        async def __aexit__(self, *a):
            return False

    with patch.object(HomeOpsAdapter, "_client", return_value=_Ctx()):
        return await adapter.get_zone_metadata()


@pytest.mark.asyncio
async def test_entry_gate_entity_hydrates_from_payload():
    zones = await _get_zone_metadata(
        _zones_payload(
            {
                "id": 1,
                "unit_id": 3,
                "entry_gate_entity": "binary_sensor.sam_master_bathroom_door_gate",
            }
        )
    )
    assert zones[1].entry_gate_entity == "binary_sensor.sam_master_bathroom_door_gate"
    assert zones[1].entry_gate_supported is True


@pytest.mark.asyncio
async def test_null_entry_gate_is_supported_but_gateless():
    """Present-and-null = "column exists, this zone has no gate" → gate passes."""
    zones = await _get_zone_metadata(
        _zones_payload({"id": 3, "unit_id": 3, "entry_gate_entity": None})
    )
    assert zones[3].entry_gate_entity is None
    assert zones[3].entry_gate_supported is True


@pytest.mark.asyncio
async def test_absent_entry_gate_key_is_unsupported():
    """Pre-migration HomeOps: absent key must NOT read as "no gate configured"."""
    zones = await _get_zone_metadata(_zones_payload({"id": 3, "unit_id": 3}))
    assert zones[3].entry_gate_entity is None
    assert zones[3].entry_gate_supported is False


@pytest.mark.asyncio
async def test_entry_gate_hydrates_alongside_occupancy_sensor():
    """Same call, same payload, same nullable-string contract as occupancy_sensor."""
    zones = await _get_zone_metadata(
        _zones_payload(
            {
                "id": 2,
                "unit_id": 3,
                "occupancy_sensor": "binary_sensor.master_bedroom_emotion_any_presence",
                "entry_gate_entity": "binary_sensor.sam_master_bedroom_door_gate",
            }
        )
    )
    meta = zones[2]
    assert meta.occupancy_sensor == "binary_sensor.master_bedroom_emotion_any_presence"
    assert meta.entry_gate_entity == "binary_sensor.sam_master_bedroom_door_gate"


@pytest.mark.asyncio
async def test_metadata_fetch_failure_yields_no_rows():
    """Degraded HomeOps → {} → entry_gate_check blocks rather than assuming."""
    adapter = _adapter()

    class _Ctx:
        async def __aenter__(self):
            raise RuntimeError("homeops down")

        async def __aexit__(self, *a):
            return False

    with patch.object(HomeOpsAdapter, "_client", return_value=_Ctx()):
        assert await adapter.get_zone_metadata() == {}
