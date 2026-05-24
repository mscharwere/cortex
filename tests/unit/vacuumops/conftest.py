"""Shared fixtures for VacuumOps unit tests."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from cortex_python.modules.vacuumops.jobs import LitterBoxJob
from cortex_python.modules.vacuumops.schemas import (
    CalendarEvent,
    ContextSnapshot,
    PersonActivity,
    RobotState,
    RoomActivity,
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
    return RoomActivity(detected=detected, confidence=confidence, raw_occupancy=raw_occupancy)


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
    }

    ctx = ContextSnapshot(
        timestamp=timestamp,
        tick_id="test-tick-001",
        home={"mode": "home"},
        people=people if people is not None else default_people,
        rooms=rooms if rooms is not None else default_rooms,
        zone_scores={"Litter Box": litter_box_score},
        upcoming_events=upcoming_events or [],
        robot_states={
            "ethan": make_robot_state(robot_state, battery),
            "sam": make_robot_state("docked", 90),
        },
        quiet_hours_1f=quiet_hours_1f,
        quiet_hours_2f=quiet_hours_2f,
        noise_budget=None,
    )
    return ctx


@pytest.fixture
def litter_box_job() -> LitterBoxJob:
    return LitterBoxJob()


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
