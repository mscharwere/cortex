"""Every job's L1 prompt template must actually render.

The templates are Jinja2 with StrictUndefined, so a variable referenced in a
template but not passed by `_render_prompt` is a hard UndefinedError — at L1
call time, in production, on a real dispatch decision. Nothing catches it
earlier: the templates are data files, so neither ruff nor mypy sees them, and
until now no test rendered them.

That gap had teeth for this PR specifically. Removing `RoomActivity.door_open`
orphaned six `{{ ctx.rooms.<room>.door_open }}` references across two templates,
and adding `{{ entry_gate }}` created a new variable the renderer has to supply.
Both classes of break are invisible to every other gate in CI.

ARIIA finding (Low), PR #53.
"""

from __future__ import annotations

import pathlib

import pytest

from cortex_python.modules.vacuumops.jobs import (
    Ethan3FLitterBoxJob,
    Ethan3FRoomsJob,
    Sam2FJob,
    Saros1FLitterBoxJob,
    Saros1FRoomsJob,
)
from cortex_python.modules.vacuumops.l1 import _describe_entry_gate, _render_prompt
from cortex_python.modules.vacuumops.schemas import ZoneMeta
from tests.unit.vacuumops.conftest import (
    make_gate,
    make_gated_zone_meta,
    make_robot_state,
    make_snapshot,
)

_PROMPTS = pathlib.Path(__file__).parents[3] / "cortex_python" / "modules" / "vacuumops"

_MB_GATE = "binary_sensor.sam_master_bathroom_door_gate"

# (job, a zone id that job actually owns)
_JOBS = [
    (Ethan3FLitterBoxJob(), 14),
    (Ethan3FRoomsJob(), 15),
    (Saros1FLitterBoxJob(), 23),
    (Saros1FRoomsJob(), 20),
    (Sam2FJob(), 1),
    # Zone 28 — Iestaf's room, added to Sam 2026-09-15. Rendered explicitly
    # rather than trusted to zone 1's pass: the template gained a
    # ctx.rooms.iestaf_room block in the same change, and StrictUndefined only
    # bites on a render that actually reaches it.
    (Sam2FJob(), 28),
]


def _ctx_for_render():
    ctx = make_snapshot()
    # conftest's default robot_states covers ethan + sam only; the Saros
    # templates reference ctx.robot_states.saros, and StrictUndefined makes that
    # a render error rather than a blank.
    ctx.robot_states["saros"] = make_robot_state()
    return ctx


@pytest.mark.parametrize(
    ("job", "zone_id"), _JOBS, ids=lambda v: getattr(v, "job_id", v)
)
def test_prompt_template_renders(job, zone_id):
    """No StrictUndefined escapes. This is the whole point of the test."""
    ctx = _ctx_for_render()
    ctx.zone_metadata[zone_id] = make_gated_zone_meta(zone_id, _MB_GATE)
    ctx.gate_readings[_MB_GATE] = make_gate(_MB_GATE, proceed=True)

    template = (_PROMPTS / job.prompt_file).read_text(encoding="utf-8")
    rendered = _render_prompt(
        job,
        zone_id,
        ctx,
        ("AMBIGUOUS", "comfort", "noise_marginal"),
        template,
        "patterns",
    )
    assert rendered.strip()
    # An orphaned reference to the deleted field would render the literal.
    assert "door_open" not in rendered


@pytest.mark.parametrize(("job", "zone_id"), [(Sam2FJob(), 1), (Saros1FRoomsJob(), 20)])
def test_gated_job_prompt_names_the_resolved_gate(job, zone_id):
    """The two door_check jobs surface the gate they were actually evaluated on."""
    ctx = _ctx_for_render()
    ctx.zone_metadata[zone_id] = make_gated_zone_meta(zone_id, _MB_GATE)
    ctx.gate_readings[_MB_GATE] = make_gate(_MB_GATE, proceed=True)

    template = (_PROMPTS / job.prompt_file).read_text(encoding="utf-8")
    rendered = _render_prompt(
        job,
        zone_id,
        ctx,
        ("AMBIGUOUS", "comfort", "noise_marginal"),
        template,
        "patterns",
    )
    assert f"open ({_MB_GATE})" in rendered


# ── _describe_entry_gate mirrors entry_gate_check, state for state ────────────


def test_describe_gate_none():
    ctx = _ctx_for_render()
    assert _describe_entry_gate(make_gated_zone_meta(3, None), ctx) == (
        "none (this zone has no entry gate)"
    )


def test_describe_gate_open():
    ctx = _ctx_for_render()
    ctx.gate_readings[_MB_GATE] = make_gate(_MB_GATE, proceed=True)
    assert (
        _describe_entry_gate(make_gated_zone_meta(1, _MB_GATE), ctx)
        == f"open ({_MB_GATE})"
    )


def test_describe_gate_closed_is_shouted():
    """Upper-cased so a CLOSED gate cannot be skimmed past in the prompt."""
    ctx = _ctx_for_render()
    ctx.gate_readings[_MB_GATE] = make_gate(_MB_GATE, proceed=False)
    assert (
        _describe_entry_gate(make_gated_zone_meta(1, _MB_GATE), ctx)
        == f"CLOSED ({_MB_GATE})"
    )


def test_describe_gate_unresolved():
    ctx = _ctx_for_render()
    described = _describe_entry_gate(make_gated_zone_meta(1, _MB_GATE), ctx)
    assert described == f"UNRESOLVED ({_MB_GATE} = not read)"


def test_describe_gate_unsupported_column():
    ctx = _ctx_for_render()
    described = _describe_entry_gate(ZoneMeta(zone_id=1, unit_id=0), ctx)
    assert "predates the entry_gate_entity column" in described


# ── 1F prompts must not re-grow a clock-based overnight hard-defer ────────────
#
# PR #39 wrote both 1F prompts off the 2F/3F templates, carrying a "quiet hours
# are 10 PM - 7 AM PST, hard defer, no exceptions" instruction. PR #46 then
# re-measured 1F occupancy and moved the rule engine the other way: 1F gets only
# the short quiet_hours_1f courtesy window (22:00-23:00) plus the mild x0.80
# sleep tier, because 23:00-07:00 is where essentially all of 1F's long clear
# windows are. Nobody updated the prompts, and because the litter-box job is
# l1_required=True its stale text blocked EVERY overnight tick for that zone
# while the l1_required=False rooms job dispatched fine on the corrected rules.
#
# The prompts are data files: ruff, mypy and every other gate are blind to them,
# so a text assertion is the only thing that can catch this drift recurring.

_ONEF_PROMPTS = ["saros_1f_litter_box.md", "saros_1f_rooms.md"]

# Lower-cased substrings that only appear in a blanket clock curfew.
_CURFEW_PHRASES = [
    "10 pm – 7 am",
    "10 pm - 7 am",
    "hard defer, no exceptions",
    "hard-defer during quiet hours",
]


@pytest.mark.parametrize("prompt_file", _ONEF_PROMPTS)
def test_1f_prompt_has_no_blanket_overnight_curfew(prompt_file):
    """No 1F prompt may instruct a score/occupancy-overriding overnight defer."""
    text = (_PROMPTS / "prompts" / prompt_file).read_text(encoding="utf-8").lower()
    for phrase in _CURFEW_PHRASES:
        assert phrase not in text, (
            f"{prompt_file} still carries a clock curfew: {phrase!r}"
        )


@pytest.mark.parametrize("prompt_file", _ONEF_PROMPTS)
def test_1f_prompt_states_the_corrected_overnight_model(prompt_file):
    """Removing the curfew is not enough — the prompt must say so positively.

    A silent deletion leaves the model free to re-derive a curfew from its own
    priors about vacuuming at night, which is the failure mode being fixed.
    """
    text = (_PROMPTS / "prompts" / prompt_file).read_text(encoding="utf-8").lower()
    assert "no blanket quiet-hours block" in text
    assert "22:00–23:00" in text  # the courtesy window it does still respect
    assert "floor_clearance_check" in text  # the real, presence-based protection


def test_2f_prompt_keeps_its_sleep_defer():
    """The 1F relaxation must not leak upstairs.

    Sam cleans the bedrooms themselves and is the one job where a sleep-window
    hard defer is correct. noise_budget() blocks 2F outright overnight (x0.05);
    the prompt says the same thing in words, and both must stay.
    """
    text = (
        (_PROMPTS / "prompts" / "sam_2f_rooms.md").read_text(encoding="utf-8").lower()
    )
    assert "hard reason to defer" in text
