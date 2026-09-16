"""Pins that a HomeOps zone is actually WIRED INTO CORTEX, not merely known to it.

The bug this file exists for (found by audit 2026-09-15, weeks after the fact):
HomeOps zone 28 — Iestaf's room, HomeOps label "Elena Room" — was added to the
HomeOps zone table and was absent from `Sam2FJob.zones` and from
`FLOOR_ROOM_MAP["2F"]`. CORTEX did not fail on it, did not defer it and did not
log it. `vacuumops_loop` iterates `job.zones`, so the zone was simply never
evaluated, silently, on every tick for weeks. There is no louder failure mode
available: an un-iterated zone produces no decision-log row to be missing from.

Registering one 2F zone touches four tables that live in four modules, and
getting three of four right still yields silence rather than an error:

  jobs.Sam2FJob.zones            the zone is dispatch-evaluated at all
  noise.FLOOR_ROOM_MAP["2F"]     the room counts toward floor clearance + noise
  synth._TRACKED_ROOMS           ctx.rooms has the key (and the template renders)
  adapters._ZONE_LABEL_TO_ROOM_KEY   the HomeOps label resolves to that room key

The fourth is where this room is most dangerous, because it is the one place the
two spellings meet. Carlos display-renamed the HA area to "Iestaf room" on
2026-09-14 but left the HA area_id (`elena_room`) and the HomeOps zone label
("Elena Room") alone, so the label key and the room-key value MUST differ. That
is the "master_bath" shape — the mismatch that made Master Bathroom's gate
resolve to "no sensor" and default to open — except here it is correct, so it
needs a pin saying so or someone will "fix" it into a real bug.
"""

from __future__ import annotations

import pathlib

from cortex_python.adapters.homeops_adapter import _ZONE_LABEL_TO_ROOM_KEY
from cortex_python.modules.vacuumops.jobs import Sam2FJob
from cortex_python.modules.vacuumops.noise import FLOOR_ROOM_MAP
from cortex_python.synth.vacuumops_synth import _TRACKED_ROOMS
from tests.unit.vacuumops.conftest import _default_zone_info

_PROMPTS = pathlib.Path(__file__).parents[3] / "cortex_python" / "modules" / "vacuumops"

# HomeOps zone 28. Verified live against GET /api/vacuum/zones 2026-09-15:
# unit 1 (vacuum.sam, 2F), label "Elena Room", region_id "8", dispatch_type "rid".
_IESTAF_ZONE = 28
_IESTAF_HOMEOPS_LABEL = "Elena Room"
_IESTAF_ROOM_KEY = "iestaf_room"


# ── The zone is dispatch-evaluated ────────────────────────────────────────────


def test_sam_evaluates_the_iestaf_room_zone():
    """The original defect, pinned directly: zone 28 must be in Sam's zone list."""
    assert _IESTAF_ZONE in Sam2FJob().zones


def test_sam_owns_seven_zones():
    """Count pin. Six was the pre-2026-09-15 number and is now a regression."""
    zones = Sam2FJob().zones
    assert zones == [1, 2, 3, 4, 5, 6, 28]
    assert len(zones) == len(set(zones)), "duplicate zone id — it would be ticked twice"


def test_every_sam_zone_is_on_2f():
    """A zone id belonging to another robot's unit here would dispatch the wrong robot."""
    zone_table = _default_zone_info()
    for zone_id in Sam2FJob().zones:
        info = zone_table.get(zone_id)
        assert info is not None, (
            f"zone {zone_id} in Sam2FJob.zones has no ZoneInfo fixture"
        )
        assert info.floor == "2F", f"zone {zone_id} is on {info.floor}, not 2F"


# ── The room participates in the floor + noise model ──────────────────────────


def test_iestaf_room_is_in_the_2f_floor_room_map():
    """Present in FLOOR_ROOM_MAP is what makes the room count toward
    floor_clearance_check's secondary sweep and noise_impact's floor radius.
    Absent, the room is invisible to both — it can be occupied and still not
    block."""
    assert _IESTAF_ROOM_KEY in FLOOR_ROOM_MAP["2F"]


def test_every_floor_room_map_key_is_a_tracked_room():
    """Generalizes test_homeops_adapter_zone_meta's adapter-side pin to the
    noise map. A key here that the synth does not fetch yields ctx.rooms.get()
    → None, which every consumer reads as "no signal" in silence."""
    mapped = {room for rooms in FLOOR_ROOM_MAP.values() for room in rooms}
    unknown = sorted(mapped - set(_TRACKED_ROOMS))
    assert unknown == [], f"FLOOR_ROOM_MAP keys not in _TRACKED_ROOMS: {unknown}"


# ── The HomeOps label resolves ────────────────────────────────────────────────


def test_homeops_label_maps_to_the_ha_entity_stem():
    """The label/key split is deliberate and load-bearing in BOTH directions.

    Key side: HomeOps still serves label "Elena Room". Spelling it "Iestaf Room"
    makes _ZONE_LABEL_TO_ROOM_KEY.get(label) miss, room_key resolve to None, and
    the zone lose its tier-2 occupancy signal with only a warning log.

    Value side: the synth builds ctx.rooms from
    binary_sensor.{key}_occupancy_status, so "elena_room" would point at an
    entity that does not exist and never will.
    """
    assert _ZONE_LABEL_TO_ROOM_KEY[_IESTAF_HOMEOPS_LABEL] == _IESTAF_ROOM_KEY


def test_iestaf_room_zone_info_carries_the_split_spelling():
    """The fixture must model the real mismatch, or gate tests prove nothing."""
    info = _default_zone_info()[_IESTAF_ZONE]
    assert info.label == _IESTAF_HOMEOPS_LABEL
    assert info.room_key == _IESTAF_ROOM_KEY
    assert info.floor == "2F"


# ── The L1 prompt sees the room ───────────────────────────────────────────────


def test_sam_prompt_covers_every_2f_room():
    """test_prompt_render only proves the template renders for the zones it is
    parametrized over. This proves no 2F room is missing from the room block the
    L1 model actually reasons over — the same silent-omission shape one layer up.
    """
    text = (_PROMPTS / Sam2FJob().prompt_file).read_text(encoding="utf-8")
    missing = [r for r in FLOOR_ROOM_MAP["2F"] if f"ctx.rooms.{r}." not in text]
    assert missing == [], f"2F rooms absent from the Sam L1 prompt: {missing}"
