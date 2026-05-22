"""Shared pytest fixtures for the CORTEX evals harness.

SHODAN owns this file — extend fixtures here as per-module evals are added.
Do not duplicate fixtures across individual test_<module>.py files.

Phase 0 scaffold: stubs for the interfaces that will be built in Phase 1.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# ModuleContext stub
# ---------------------------------------------------------------------------
# The real ModuleContext lives in cortex_python/modules/base.py (Phase 1).
# This stub mirrors the contract described in schemas/CONTRACT.md so evals
# can be written now and swapped to the real import when it ships.


class _ModuleContext:
    """Lightweight stand-in for cortex_python.modules.base.ModuleContext."""

    def __init__(
        self,
        ts: datetime | None = None,
        presence: dict[str, str] | None = None,
        module_config: dict[str, Any] | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.ts = ts or datetime.now(tz=timezone.utc)
        self.presence = presence or {}
        self.module_config = module_config or {}
        self.raw = raw or {}


def make_context(
    presence: dict[str, str] | None = None,
    module_config: dict[str, Any] | None = None,
    raw: dict[str, Any] | None = None,
) -> _ModuleContext:
    """Factory for building a ModuleContext in eval tests.

    Usage::

        ctx = make_context(presence={"carlos": "home", "daniel": "school"})
    """
    return _ModuleContext(
        presence=presence or {"carlos": "home"},
        module_config=module_config or {},
        raw=raw or {},
    )


# ---------------------------------------------------------------------------
# Mock LiteLLM client
# ---------------------------------------------------------------------------


class _MockLiteLLMClient:
    """Fake async HTTP client that returns canned LiteLLM responses.

    Set the response payload before calling the code under test::

        mock_litellm_client.set_response({"action": "dispatch_vacuum", "confidence": 0.87})
    """

    def __init__(self) -> None:
        self._response_body: dict[str, Any] = {}

    def set_response(self, payload: dict[str, Any]) -> None:
        """Set the JSON payload the mock will return as the LLM message content."""
        self._response_body = payload

    async def post(self, url: str, **kwargs: Any) -> MagicMock:
        content = json.dumps(self._response_body)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": content, "role": "assistant"}}],
            "model": kwargs.get("json", {}).get("model", "gemma4:31b"),
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        return mock_resp

    async def get(self, url: str, **kwargs: Any) -> MagicMock:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"object": "list", "data": []}
        return mock_resp

    async def __aenter__(self) -> "_MockLiteLLMClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        pass


@pytest.fixture()
def mock_litellm_client() -> _MockLiteLLMClient:
    """Yield a mock LiteLLM async HTTP client.

    Patch ``cortex_python.adapters.litellm_client.get_litellm_client`` with
    this fixture in tests that exercise LLM skill code::

        @pytest.fixture(autouse=True)
        def _patch_client(mock_litellm_client, monkeypatch):
            monkeypatch.setattr(
                "cortex_python.adapters.litellm_client.get_litellm_client",
                lambda _: mock_litellm_client,
            )
    """
    return _MockLiteLLMClient()


# ---------------------------------------------------------------------------
# Mock DecisionLogger
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_decision_logger() -> AsyncMock:
    """Yield a mock DecisionLogger with an async `annotate` method.

    Usage::

        decision = await decide(ctx)
        mock_decision_logger.annotate.assert_awaited()
    """
    logger = AsyncMock()
    logger.annotate = AsyncMock()
    return logger


# ---------------------------------------------------------------------------
# Sample ModuleContext fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_context() -> _ModuleContext:
    """Return a realistic ModuleContext for generic eval tests."""
    return make_context(
        presence={
            "carlos": "home",
            "elena": "away",
            "carlitos": "home",
            "daniel": "school",
        },
        module_config={
            "dirty_score_threshold": 0.65,
            "quiet_hours_start": "22:00",
            "quiet_hours_end": "07:00",
        },
        raw={
            "vacuum": {"sam": "docked", "ethan": "docked"},
            "rooms": {
                "kitchen": {"dirty": 0.71, "last_clean_min_ago": 480},
                "living_room": {"dirty": 0.45, "last_clean_min_ago": 240},
            },
            "noise_budget": {"quiet_hours_active": False, "kids_homework": False},
            "health": {"carlos_bb": 72},
        },
    )
