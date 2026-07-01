"""Evals harness smoke test — Phase 0 placeholder.

SHODAN replaces / extends this file with per-module evals in Phase 1+.
This test exists solely to confirm the harness loads and pytest can discover
eval tests. It must always pass.

See tests/evals/README.md for how to add real evals.
"""

from __future__ import annotations

from tests.evals.conftest import make_context


def test_harness_placeholder() -> None:
    """Placeholder — SHODAN populates per-module evals starting Phase 1."""
    assert True  # placeholder — SHODAN populates per-module evals


def test_make_context_defaults() -> None:
    """Verify the make_context factory produces a usable ModuleContext stub."""
    ctx = make_context()
    assert ctx.presence == {"carlos": "home"}
    assert ctx.module_config == {}
    assert ctx.raw == {}
    assert ctx.ts is not None


def test_make_context_with_overrides() -> None:
    """Verify make_context accepts caller-supplied values."""
    ctx = make_context(
        presence={"carlos": "away", "elena": "home"},
        module_config={"dirty_score_threshold": 0.7},
        raw={"vacuum": {"sam": "docked"}},
    )
    assert ctx.presence["carlos"] == "away"
    assert ctx.module_config["dirty_score_threshold"] == 0.7
    assert ctx.raw["vacuum"]["sam"] == "docked"


def test_mock_litellm_client_sync(mock_litellm_client) -> None:  # type: ignore[no-untyped-def]
    """Verify the mock client fixture is importable and set_response works."""
    mock_litellm_client.set_response({"action": "dispatch_vacuum", "confidence": 0.87})
    # _response_body is set; actual async call tested in async evals
    assert mock_litellm_client._response_body["action"] == "dispatch_vacuum"


def test_sample_context_fixture(sample_context) -> None:  # type: ignore[no-untyped-def]
    """Verify the sample_context fixture has expected structure."""
    assert sample_context.presence["carlos"] == "home"
    assert "kitchen" in sample_context.raw["rooms"]
    assert sample_context.module_config["dirty_score_threshold"] == 0.65
