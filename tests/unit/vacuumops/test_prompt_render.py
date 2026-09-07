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
