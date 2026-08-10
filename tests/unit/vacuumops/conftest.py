"""Shared fixtures for VacuumOps unit tests."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from cortex_python.modules.vacuumops.jobs import Ethan3FLitterBoxJob
from cortex_python.modules.vacuumops.schemas import (
    ContextSnapshot,
    PersonActivity,
    RobotState,
    RoomActivity,
    ZoneInfo,
)


def make_robot_state(
    state: str = "docked",
    battery_pct: int = 80,
    current_zone: str | None = None,
) -> RobotState:
    return RobotState(state=state, battery_pct=battery_pct, current_zone=current_zone)


def make_person(
    activity: str = "home_idle",
    confidence: float = 0.9,
    piano: bool | None = None,
    sleep_confidence: float | None = None,
) -> PersonActivity:
    return PersonActivity(
        activity=activity,
        confidence=confidence,
        piano=piano,
        sleep_confidence=sleep_confidence,
    )


def make_room(
    detected: str = "idle",
    confidence: float = 0.8,
    raw_occupancy: bool = False,
) -> RoomActivity:
    return RoomActivity(
        detected=detected, confidence=confidence, raw_occupancy=raw_occupancy
    )


def make_snapshot(
    *,
    robot_state: str = "docked",
    battery: int = 80,
    litter_box_score: float = 75.0,
    people: dict | None = None,
    rooms: dict | None = None,
    quiet_hours_1f: bool = False,
    quiet_hours_2f: bool = False,
    upcoming_events: list | None = None,
    timestamp: datetime | None = None,
    home_count: int = -1,
    who_home: list | None = None,
    home_empty: bool = False,
) -> ContextSnapshot:
    if timestamp is None:
        timestamp = datetime(2026, 5, 24, 15, 0, 0, tzinfo=timezone.utc)  # 8 AM PST

    default_people = {
        "carlos": make_person("home_idle"),
        "elena": make_person("home_idle"),
        "carlitos": make_person("school"),
        "daniel": make_person("school"),
        "iestaf": make_person("away"),
    }
    default_rooms = {
        "kitchen": make_room("idle"),
        "living_room": make_room("idle"),
        "hallway": make_room("idle"),
        "dining_room": make_room("idle"),
        "prep_area": make_room("idle"),
        "bathroom": make_room("idle"),
        "master_bedroom": make_room("idle"),
        "carlitos_room": make_room("idle"),
        "upper_hallway": make_room("idle"),
        "master_bath": make_room("idle"),
        "kids_table_area": make_room("idle"),
        "loft": make_room("idle"),
        "office": make_room("idle"),
        "gym": make_room("idle"),
        "daniel_room": make_room("idle"),
    }

    ctx = ContextSnapshot(
        timestamp=timestamp,
        tick_id="test-tick-001",
        home={"mode": "home"},
        people=people if people is not None else default_people,
        rooms=rooms if rooms is not None else default_rooms,
        zone_scores={14: litter_box_score},
        zone_info={
            # Ethan 3F — Litter Box sub-zone (no room sensor)
            14: ZoneInfo(
                label="Litter Box",
                display="3F Litter Box",
                unit_id=2,
                floor="3F",
                room_key=None,
            ),
            # Ethan 3F — room zones
            15: ZoneInfo(
                label="Loft", display="3F Loft", unit_id=2, floor="3F", room_key="loft"
            ),
            16: ZoneInfo(
                label="Office",
                display="3F Office",
                unit_id=2,
                floor="3F",
                room_key="office",
            ),
            17: ZoneInfo(
                label="Gym", display="3F Gym", unit_id=2, floor="3F", room_key="gym"
            ),
            # Saros 1F — room zones
            19: ZoneInfo(
                label="Kitchen",
                display="1F Kitchen",
                unit_id=1,
                floor="1F",
                room_key="kitchen",
            ),
            20: ZoneInfo(
                label="Bathroom",
                display="1F Bathroom",
                unit_id=1,
                floor="1F",
                room_key="bathroom",
            ),
            21: ZoneInfo(
                label="Living Room",
                display="1F Living Room",
                unit_id=1,
                floor="1F",
                room_key="living_room",
            ),
            22: ZoneInfo(
                label="Hallway",
                display="1F Hallway",
                unit_id=1,
                floor="1F",
                room_key="hallway",
            ),
            # Saros 1F — sub-zones (no room sensor)
            23: ZoneInfo(
                label="Litter Box",
                display="1F Litter Box",
                unit_id=1,
                floor="1F",
                room_key=None,
            ),
            24: ZoneInfo(
                label="Prep Area",
                display="1F Prep Area",
                unit_id=1,
                floor="1F",
                room_key=None,
            ),
            25: ZoneInfo(
                label="Dining Table",
                display="1F Dining Table",
                unit_id=1,
                floor="1F",
                room_key="dining_room",
            ),
            # Sam 2F — room zones
            1: ZoneInfo(
                label="Master Bathroom",
                display="2F Master Bathroom",
                unit_id=3,
                floor="2F",
                room_key="master_bath",
            ),
            2: ZoneInfo(
                label="Master Bedroom",
                display="2F Master Bedroom",
                unit_id=3,
                floor="2F",
                room_key="master_bedroom",
            ),
            3: ZoneInfo(
                label="Upper Hallway",
                display="2F Upper Hallway",
                unit_id=3,
                floor="2F",
                room_key="upper_hallway",
            ),
            4: ZoneInfo(
                label="Carlitos Room",
                display="2F Carlitos Room",
                unit_id=3,
                floor="2F",
                room_key="carlitos_room",
            ),
            # Sam 2F — sub-zone (no room sensor)
            5: ZoneInfo(
                label="Kids Table Area",
                display="2F Kids Table Area",
                unit_id=3,
                floor="2F",
                room_key=None,
            ),
            6: ZoneInfo(
                label="Daniel's Room",
                display="2F Daniel's Room",
                unit_id=3,
                floor="2F",
                room_key="daniel_room",
            ),
        },
        upcoming_events=upcoming_events or [],
        robot_states={
            "ethan": make_robot_state(robot_state, battery),
            "sam": make_robot_state("docked", 90),
        },
        quiet_hours_1f=quiet_hours_1f,
        quiet_hours_2f=quiet_hours_2f,
        noise_budget=None,
        home_count=home_count,
        who_home=who_home if who_home is not None else [],
        home_empty=home_empty,
    )
    return ctx


@pytest.fixture
def litter_box_job() -> Ethan3FLitterBoxJob:
    return Ethan3FLitterBoxJob()


@pytest.fixture
def mock_redis() -> AsyncMock:
    """Redis mock — all EXISTS calls return 0 (no cooldown) by default."""
    r = AsyncMock()
    r.exists = AsyncMock(return_value=0)
    r.ttl = AsyncMock(return_value=-1)
    r.set = AsyncMock(return_value=True)
    r.get = AsyncMock(return_value=None)
    return r


@pytest.fixture
def clean_ctx() -> ContextSnapshot:
    """All-clear snapshot — everything passes by default."""
    return make_snapshot()
