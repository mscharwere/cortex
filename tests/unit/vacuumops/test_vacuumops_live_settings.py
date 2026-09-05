"""HomeOpsAdapter.get_vacuumops_settings() — the live kill-switch read.

Covers the adapter half of moving `opportunity_actuate` out of the source tree
and into HomeOps `cortex_vacuumops_settings`, alongside the `mop_enabled` switch
that made the same move earlier.

THREE THINGS ARE UNDER TEST HERE AND THEY ARE DIFFERENT THINGS:

  1. FAIL-CLOSED, PER FLAG. Every unreachable / non-2xx / malformed / missing /
     wrong-typed case resolves that flag to False. Never raises (a settings
     outage must not take down a dispatch tick), never truthy-coerces, never
     defaults on.

  2. ONE HTTP CALL FOR BOTH FLAGS. They live in one DB row and are needed on the
     same tick. A request per flag would double the per-tick call count for no
     added freshness and would let two flags that cannot disagree in the
     database arrive from two different instants. Pinned by counting requests,
     because "we meant to share the call" is not a property a reader can check.

  3. `read_ok` SEPARATES "OFF" FROM "COULDN'T ASK". Both produce False, so
     without this bit `r1.opportunity_check` could not honour its invariant 3
     ("every degraded path names its degradation") — an outage would log as an
     ordinary shadow tick and be invisible in exactly the place the §4.5 soak is
     read from.

The existing mop-side coverage (test_mop.py::TestGetVacuumopsMopEnabledFailClosed)
is deliberately left alone: it pins the single-flag wrapper's public contract,
which this refactor must not change.
"""

from __future__ import annotations

import httpx
import pytest

from cortex_python.adapters.homeops_adapter import HomeOpsAdapter, VacuumOpsLiveSettings

_SETTINGS_PATH = "/api/cortex/vacuumops-settings"


class _CountingClient:
    """Fake httpx.AsyncClient returning `payload`, recording every GET path."""

    def __init__(self, payload, calls: list[str]) -> None:
        self._payload = payload
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str, **kwargs):
        self._calls.append(url)
        return _Resp(self._payload)


class _Resp:
    def __init__(self, payload) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _RaisingClient:
    def __init__(self, exc: Exception, calls: list[str]) -> None:
        self._exc = exc
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str, **kwargs):
        self._calls.append(url)
        raise self._exc


class _StatusErrorClient:
    class _Resp:
        def raise_for_status(self):
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]

        def json(self):
            raise AssertionError("json() must not be reached after raise_for_status()")

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str, **kwargs):
        self._calls.append(url)
        return self._Resp()


def _adapter(client_factory) -> tuple[HomeOpsAdapter, list[str]]:
    """A HomeOpsAdapter wired to a fake client, plus the list of GET paths it made.

    Built via __new__ to skip Settings construction — this suite is about the
    parsing contract, not about configuration.
    """
    calls: list[str] = []
    adapter = HomeOpsAdapter.__new__(HomeOpsAdapter)
    adapter._base_url = "http://homeops.test"  # type: ignore[attr-defined]
    adapter._api_key = "test"  # type: ignore[attr-defined]
    adapter._headers = {}  # type: ignore[attr-defined]
    adapter._client = lambda: client_factory(calls)  # type: ignore[method-assign]
    return adapter, calls


def _returning(payload):
    return _adapter(lambda calls: _CountingClient(payload, calls))


# ── 1. Fail-closed, per flag ─────────────────────────────────────────────────


class TestOpportunityActuateFailsClosed:
    @pytest.mark.asyncio
    async def test_confirmed_true(self) -> None:
        adapter, _ = _returning({"data": {"opportunity_actuate": True}})
        assert await adapter.get_vacuumops_opportunity_actuate() is True

    @pytest.mark.asyncio
    async def test_confirmed_false(self) -> None:
        adapter, _ = _returning({"data": {"opportunity_actuate": False}})
        assert await adapter.get_vacuumops_opportunity_actuate() is False

    @pytest.mark.asyncio
    async def test_homeops_unreachable_fails_closed_without_raising(self) -> None:
        """A settings outage mid-tick must not propagate: zone scores are the
        only hard dependency, and this flag is not one."""
        adapter, _ = _adapter(
            lambda calls: _RaisingClient(ConnectionError("refused"), calls)
        )
        assert await adapter.get_vacuumops_opportunity_actuate() is False

    @pytest.mark.asyncio
    async def test_timeout_fails_closed(self) -> None:
        adapter, _ = _adapter(
            lambda calls: _RaisingClient(httpx.ReadTimeout("slow"), calls)
        )
        assert await adapter.get_vacuumops_opportunity_actuate() is False

    @pytest.mark.asyncio
    async def test_non_2xx_fails_closed(self) -> None:
        adapter, _ = _adapter(_StatusErrorClient)
        assert await adapter.get_vacuumops_opportunity_actuate() is False

    @pytest.mark.asyncio
    async def test_missing_data_key_fails_closed(self) -> None:
        adapter, _ = _returning({"unexpected": "shape"})
        assert await adapter.get_vacuumops_opportunity_actuate() is False

    @pytest.mark.asyncio
    async def test_data_not_a_dict_fails_closed(self) -> None:
        adapter, _ = _returning({"data": None})
        assert await adapter.get_vacuumops_opportunity_actuate() is False

    @pytest.mark.asyncio
    async def test_body_not_a_dict_fails_closed(self) -> None:
        adapter, _ = _returning(["not", "a", "dict"])
        assert await adapter.get_vacuumops_opportunity_actuate() is False

    @pytest.mark.asyncio
    async def test_missing_key_fails_closed(self) -> None:
        """A HomeOps that predates the column must not read as an implicit on."""
        adapter, _ = _returning({"data": {"mop_enabled": True}})
        assert await adapter.get_vacuumops_opportunity_actuate() is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["true", "True", 1, [1], {"v": True}, "yes"])
    async def test_truthy_non_bool_fails_closed(self, value) -> None:
        """⛔ THE ONE THAT MATTERS. A flag that can change live dispatch must
        not be switched on by a serialization accident — a JSON string "true",
        or an integer 1 out of a MySQL TINYINT read through a driver that does
        not map it to bool. Only a literal `True` counts."""
        adapter, _ = _returning({"data": {"opportunity_actuate": value}})
        assert await adapter.get_vacuumops_opportunity_actuate() is False

    @pytest.mark.asyncio
    async def test_null_fails_closed(self) -> None:
        adapter, _ = _returning({"data": {"opportunity_actuate": None}})
        assert await adapter.get_vacuumops_opportunity_actuate() is False


# ── 2. One call for both flags ───────────────────────────────────────────────


class TestFlagsShareOneRequest:
    @pytest.mark.asyncio
    async def test_both_flags_come_from_a_single_get(self) -> None:
        adapter, calls = _returning(
            {"data": {"mop_enabled": True, "opportunity_actuate": True}}
        )
        settings = await adapter.get_vacuumops_settings()
        assert (settings.mop_enabled, settings.opportunity_actuate) == (True, True)
        assert calls == [_SETTINGS_PATH], "both flags must share ONE round trip"

    @pytest.mark.asyncio
    async def test_the_two_flags_are_independent(self) -> None:
        """Sharing a request must not make them share a value."""
        adapter, _ = _returning(
            {"data": {"mop_enabled": True, "opportunity_actuate": False}}
        )
        settings = await adapter.get_vacuumops_settings()
        assert settings.mop_enabled is True
        assert settings.opportunity_actuate is False

    @pytest.mark.asyncio
    async def test_one_missing_flag_does_not_taint_the_other(self) -> None:
        """A HomeOps mid-migration (mop column present, opportunity column not)
        must still deliver the flag it does have."""
        adapter, _ = _returning({"data": {"mop_enabled": True}})
        settings = await adapter.get_vacuumops_settings()
        assert settings.mop_enabled is True
        assert settings.opportunity_actuate is False
        assert settings.read_ok is True

    @pytest.mark.asyncio
    async def test_the_single_flag_wrappers_agree_with_the_record(self) -> None:
        payload = {"data": {"mop_enabled": False, "opportunity_actuate": True}}
        a1, _ = _returning(payload)
        a2, _ = _returning(payload)
        a3, _ = _returning(payload)
        record = await a1.get_vacuumops_settings()
        assert await a2.get_vacuumops_mop_enabled() is record.mop_enabled
        assert (
            await a3.get_vacuumops_opportunity_actuate() is record.opportunity_actuate
        )

    @pytest.mark.asyncio
    async def test_wrappers_each_make_exactly_one_call(self) -> None:
        """The wrappers must delegate, not re-implement — one GET each, not two."""
        adapter, calls = _returning({"data": {"mop_enabled": True}})
        await adapter.get_vacuumops_mop_enabled()
        assert calls == [_SETTINGS_PATH]


# ── 3. read_ok — "switched off" vs. "couldn't ask" ───────────────────────────


class TestReadOkSeparatesOffFromUnreachable:
    @pytest.mark.asyncio
    async def test_a_confirmed_off_is_not_degraded(self) -> None:
        adapter, _ = _returning(
            {"data": {"mop_enabled": False, "opportunity_actuate": False}}
        )
        settings = await adapter.get_vacuumops_settings()
        assert settings.opportunity_actuate is False
        assert settings.read_ok is True, "we heard back — this is a real 'off'"

    @pytest.mark.asyncio
    async def test_an_absent_column_is_not_degraded(self) -> None:
        """Subtle but deliberate: HomeOps ANSWERED and the answer is "no such
        switch here". The fail-closed reading of that is a confirmed off, not an
        outage, so read_ok stays True and the decision log says `shadow` rather
        than `shadow_degraded`. Only silence clears read_ok."""
        adapter, _ = _returning({"data": {}})
        settings = await adapter.get_vacuumops_settings()
        assert settings.opportunity_actuate is False
        assert settings.read_ok is True

    @pytest.mark.asyncio
    async def test_unreachable_is_degraded(self) -> None:
        adapter, _ = _adapter(
            lambda calls: _RaisingClient(ConnectionError("refused"), calls)
        )
        settings = await adapter.get_vacuumops_settings()
        assert settings.opportunity_actuate is False
        assert settings.read_ok is False

    @pytest.mark.asyncio
    async def test_non_2xx_is_degraded(self) -> None:
        adapter, _ = _adapter(_StatusErrorClient)
        assert (await adapter.get_vacuumops_settings()).read_ok is False

    @pytest.mark.asyncio
    async def test_malformed_body_is_degraded(self) -> None:
        adapter, _ = _returning({"data": "not-a-dict"})
        assert (await adapter.get_vacuumops_settings()).read_ok is False

    def test_the_default_record_is_closed_and_degraded(self) -> None:
        """The zero value must be the safe one: nothing on, nothing confirmed."""
        blank = VacuumOpsLiveSettings()
        assert blank.mop_enabled is False
        assert blank.opportunity_actuate is False
        assert blank.read_ok is False


# ── 4. build_snapshot hands the record to the loop ───────────────────────────


class TestSnapshotThreadsTheRecord:
    """`build_snapshot` returns the live record as its third element.

    The loop reads its kill switches from nowhere else, so if the synth dropped
    or defaulted this the whole live path would be dead while every unit test of
    the rule and the adapter still passed.
    """

    @staticmethod
    def _adapters(settings: VacuumOpsLiveSettings):
        from unittest.mock import AsyncMock, MagicMock

        ha = MagicMock()
        ha.get_entity_state = AsyncMock(return_value=None)
        ha.list_calendar_entities = AsyncMock(return_value=[])
        ha.get_calendar_events = AsyncMock(return_value=[])

        homeops = MagicMock()
        homeops.get_zone_data = AsyncMock(
            return_value=({19: 50.0}, {}, {"saros": False})
        )
        homeops.get_zone_metadata = AsyncMock(return_value={})
        homeops.get_vacuumops_settings = AsyncMock(return_value=settings)
        return ha, homeops

    @pytest.mark.asyncio
    async def test_the_record_reaches_the_caller_intact(self) -> None:
        from unittest.mock import MagicMock

        from cortex_python.synth.vacuumops_synth import build_snapshot

        live = VacuumOpsLiveSettings(
            mop_enabled=False, opportunity_actuate=True, read_ok=True
        )
        ha, homeops = self._adapters(live)
        _, _, out = await build_snapshot("t-1", ha, homeops, MagicMock())
        assert out.opportunity_actuate is True
        assert out.mop_enabled is False
        assert out.read_ok is True

    @pytest.mark.asyncio
    async def test_the_synth_makes_one_settings_call_per_tick(self) -> None:
        """Not one per flag — the shared-call guarantee has to survive the synth."""
        from unittest.mock import MagicMock

        from cortex_python.synth.vacuumops_synth import build_snapshot

        ha, homeops = self._adapters(VacuumOpsLiveSettings(read_ok=True))
        await build_snapshot("t-1", ha, homeops, MagicMock())
        assert homeops.get_vacuumops_settings.await_count == 1

    @pytest.mark.asyncio
    async def test_a_degraded_read_does_not_skip_the_tick(self) -> None:
        """Zone scores are the only hard dependency; the switches are not."""
        from unittest.mock import MagicMock

        from cortex_python.synth.vacuumops_synth import build_snapshot

        ha, homeops = self._adapters(VacuumOpsLiveSettings(read_ok=False))
        ctx, _, out = await build_snapshot("t-1", ha, homeops, MagicMock())
        assert ctx is not None
        assert out.read_ok is False
        assert out.opportunity_actuate is False
