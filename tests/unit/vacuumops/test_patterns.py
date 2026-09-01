"""Unit tests for patterns.yaml — the curated household pattern set (D7).

Two concerns:

  1. Memo finding R7. `kids_school_window_mon_thu` / `_friday` asserted that
     08:40-15:05 was the "Best window for all cleaning runs". Measured against 7
     days of live binary_sensor.first_floor_occupancy_status history that window
     runs ~85% occupied (hours 12-14 read 96-100%) because Elena is home — close
     to the WORST window of the weekday, not the best. Pattern descriptions are
     injected verbatim into the L1 prompt by loop.render_patterns_for(), so a
     wrong description actively misleads the one tier designed to reason about
     timing. These tests pin the corrected claims.

  2. File integrity. loop._load_patterns() swallows every parse failure in a
     try/except and logs a warning, so a malformed entry degrades silently to
     "no patterns" rather than failing loudly. Structural assertions here are
     the only thing standing between a typo and a silent loss of all curated
     household knowledge.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PATTERNS_PATH = (
    Path(__file__).resolve().parents[3]
    / "cortex_python"
    / "modules"
    / "vacuumops"
    / "patterns.yaml"
)

_VALID_RELEVANCE = {"transit", "noise", "window"}


@pytest.fixture(scope="module")
def patterns() -> list[dict]:
    data = yaml.safe_load(PATTERNS_PATH.read_text(encoding="utf-8"))
    return data["patterns"]


@pytest.fixture(scope="module")
def by_name(patterns) -> dict[str, dict]:
    return {p["name"]: p for p in patterns}


# ── R7: the school-window descriptions ────────────────────────────────────────

SCHOOL_WINDOWS = ["kids_school_window_mon_thu", "kids_school_window_friday"]


@pytest.mark.parametrize("name", SCHOOL_WINDOWS)
def test_school_window_pattern_is_retained(by_name, name):
    """R7 was fixed by CORRECTING these patterns, not deleting them.

    The causal fact they encode — the kids are gone, so kid-driven transit and
    noise are absent — is real and is not learnable from ~4 samples per prior
    slot. Only the inference drawn from it (kids away ⇒ house empty) was wrong.
    """
    assert name in by_name


@pytest.mark.parametrize("name", SCHOOL_WINDOWS)
def test_school_window_no_longer_claims_to_be_a_good_window(by_name, name):
    """The refuted claims must not come back, in any casing."""
    description = by_name[name]["description"].lower()
    for refuted in ("best window", "house quiet", "quiet with only"):
        assert refuted not in description, f"{name} still asserts {refuted!r}"


@pytest.mark.parametrize("name", SCHOOL_WINDOWS)
def test_school_window_warns_that_1f_stays_occupied(by_name, name):
    """L1 must be told the opposite of what the file used to say."""
    description = by_name[name]["description"].lower()
    assert "elena" in description
    assert "not a good dispatch window" in description


def test_mon_thu_description_carries_the_measured_numbers(by_name):
    """The correction is evidence-backed, so the evidence travels with it.

    L1 reasons from this text; a bare "not good" is weaker context than the
    measured occupancy rate that justifies it.
    """
    description = by_name["kids_school_window_mon_thu"]["description"]
    assert "85%" in description
    assert "96-100%" in description


@pytest.mark.parametrize("name", SCHOOL_WINDOWS)
def test_school_window_times_unchanged(by_name, name):
    """The correction is to the CLAIM, not the boundaries.

    08:40 and the 15:05 / 13:50 ends are real school-schedule facts and are what
    make the neighbouring arrival transit patterns line up.
    """
    expected_end = {"kids_school_window_mon_thu": "15:05", "kids_school_window_friday": "13:50"}
    assert by_name[name]["start"] == "08:40"
    assert by_name[name]["end"] == expected_end[name]


@pytest.mark.parametrize("name", SCHOOL_WINDOWS)
def test_school_window_relevance_still_window_not_transit(by_name, name):
    """These stay advisory (L1 prompt context), never a deterministic gate.

    Only "transit" relevance blocks in R1 (transit_pattern_lookahead). Promoting
    a 6-hour window to a hard R1 block would starve every 1F zone on weekdays —
    the correct fix for a bad window is better L1 context, not a new hard gate.
    """
    assert by_name[name]["relevance"] == ["window"]


# ── File integrity ────────────────────────────────────────────────────────────


def test_patterns_file_parses_and_is_non_empty(patterns):
    assert isinstance(patterns, list)
    assert len(patterns) >= 12


def test_pattern_names_are_unique(patterns):
    names = [p["name"] for p in patterns]
    assert len(names) == len(set(names))


def test_every_pattern_is_structurally_valid(patterns):
    """Mirrors what loop.render_patterns_for() requires of every entry."""
    from cortex_python.modules.vacuumops.utils import parse_pattern_time

    for p in patterns:
        name = p.get("name")
        assert name, f"pattern missing name: {p}"
        assert p.get("description", "").strip(), f"{name} has no description"
        assert p.get("jobs"), f"{name} has no jobs field"

        days = p.get("days")
        assert days and all(d in range(1, 8) for d in days), f"{name} has bad days"

        relevance = p.get("relevance")
        assert relevance, f"{name} has no relevance"
        assert set(relevance) <= _VALID_RELEVANCE, f"{name} has unknown relevance"

        from datetime import date

        start = parse_pattern_time(date(2026, 9, 1), p["start"])
        end = parse_pattern_time(date(2026, 9, 1), p["end"])
        assert start < end, f"{name} start is not before end"


def test_patterns_load_through_the_runtime_loader():
    """The file the tests read is the file the loop actually loads."""
    from cortex_python.modules.vacuumops import loop

    loaded = loop._load_patterns()
    assert {p["name"] for p in loaded} >= set(SCHOOL_WINDOWS)
