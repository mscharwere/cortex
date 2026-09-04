"""PR A3 — `r1.opportunity_check`, the predictive-patience comfort rule.

These tests exist because A3 is the PR that puts the patience mechanism on the
LIVE DISPATCH PATH. A1 and A2 could be wrong and cost nothing; this one runs
inside `run_r1` on every Saros 1F tick. "It compiles" and "the happy path
returns better_window" are not evidence that it is safe, so the suite is
organised around the three structural invariants and the fail-open matrix from
spec §4.4 rather than around the functions.

The load-bearing tests, in the order they matter:

  1. `TestInvariantCannotForceDispatch` — the rule can withhold, never compel.
  2. `TestInvariantEffectivenessRunsFirst` — MEASURED occupancy short-circuits
     before PREDICTED occupancy is even consulted. This is the 2026-08-31
     incident class, and the test proves the prior source is never TOUCHED on a
     zone the occupancy gate rejected, rather than merely checking the verdict.
  3. `TestInvariantDegradationNamesItself` — every fail-open path returns PASS
     with a reason string that says what degraded. A silent no-op is the exact
     shape of the 2026-08-31 root cause.
  4. `TestFailOpenMatrix` — the five specified degradations, one test each.
  5. `TestDeferStreak` — the chained-deferral instrument the A4 go/no-go reads.
  6. `TestShadowMode` — the whole point of A3: a real verdict that cannot act.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cortex_python.modules.vacuumops.config import VacuumOpsConfig
from cortex_python.modules.vacuumops.jobs import (
    Ethan3FLitterBoxJob,
    Ethan3FRoomsJob,
    Sam2FJob,
    Saros1FLitterBoxJob,
    Saros1FRoomsJob,
)
from cortex_python.modules.vacuumops.opportunity import over_threshold_since_key
from cortex_python.modules.vacuumops.priors import (
    CONFIDENCE_GOOD,
    CONFIDENCE_UNAVAILABLE,
    PriorObservation,
    SlotPrior,
    confidence_for,
    summarize,
)
from cortex_python.modules.vacuumops.r1 import (
    _OPPORTUNITY_DEFER_STREAK_KEY,
    _OPPORTUNITY_FAIL_OPEN,
    VERDICT_BETTER_WINDOW,
    VERDICT_FIT_MARGINAL,
    VERDICT_FIT_OK,
    OpportunityContext,
    opportunity_check,
    run_r1,
)
from tests.unit.vacuumops.conftest import make_occupancy, make_room, make_snapshot

# The Saros 1F Kitchen. A real zone on the only job that runs this rule.
ZONE = 19
ENTITY = "binary_sensor.first_floor_occupancy_status"

# 8:00 AM PST, exactly on a 30-minute slot boundary so the fit arithmetic is
# predictable rather than straddling.
NOW = datetime(2026, 5, 24, 15, 0, 0, tzinfo=UTC)

# p75 active 25 min + 5 min return-leg allowance = a 30-minute reservation,
# which is exactly one slot. Chosen so a mission occupies whole slots and the
# expected values below can be reasoned about by hand.
GOOD_STATS: dict[str, Any] = {"p75_active_duration_min": 25.0, "avg_active_duration_min": 22.0}


# ── Fakes ─────────────────────────────────────────────────────────────────────


class FakeRedis:
    """A STATEFUL Redis fake. The shared `mock_redis` fixture is not usable here.

    `mock_redis` is a bare AsyncMock, so `set()` followed by `get()` returns
    None rather than the stored value. `opportunity_check` does a SETNX-then-GET
    round trip on the starvation clock in a single call and depends on reading
    back what it just wrote — against a stateless mock every zone would look
    like it had no clock, every call would return IMPATIENT, and the entire
    suite would pass while testing nothing. Modelled on `test_priors.FakeRedis`.
    """

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.data: dict[str, str] = dict(initial or {})
        self.ttls: dict[str, int] = {}
        self.fail_on: set[str] = set()

    def _guard(self, op: str) -> None:
        if op in self.fail_on:
            raise ConnectionError(f"redis {op} unavailable")

    async def get(self, key: str) -> str | None:
        self._guard("get")
        return self.data.get(key)

    async def set(self, key: str, value: Any, nx: bool = False, ex: int | None = None) -> bool:
        self._guard("set")
        if nx and key in self.data:
            return False
        self.data[key] = str(value)
        return True

    async def incr(self, key: str) -> int:
        self._guard("incr")
        value = int(self.data.get(key, "0")) + 1
        self.data[key] = str(value)
        return value

    async def expire(self, key: str, ttl: int) -> bool:
        self._guard("expire")
        self.ttls[key] = ttl
        return True

    async def delete(self, *keys: str) -> int:
        self._guard("delete")
        removed = 0
        for key in keys:
            if self.data.pop(key, None) is not None:
                removed += 1
        return removed

    async def exists(self, key: str) -> int:
        self._guard("exists")
        return 1 if key in self.data else 0

    async def ttl(self, key: str) -> int:
        return self.ttls.get(key, -1)


@dataclass
class FakePriorSource:
    """Serves a scripted forward curve and counts its own reads.

    `reads` is what proves invariant 2: a test can assert the store was never
    CONSULTED, which is a stronger and more refactor-proof claim than asserting
    the rule returned PASS (which it also does on every degraded path).
    """

    means: list[float]
    native_count: int = 4
    age_days: float = 30.0
    reads: list[tuple[int, int]] = field(default_factory=list)
    empty_after: int | None = None

    async def read_observations(
        self, entity_id: str, day_of_week: int, slot: int
    ) -> list[PriorObservation]:
        index = len(self.reads)
        self.reads.append((day_of_week, slot))
        if self.empty_after is not None and index >= self.empty_after:
            return []
        mean = self.means[index] if index < len(self.means) else self.means[-1]
        oldest = NOW - timedelta(days=self.age_days)
        return [
            PriorObservation(f=mean, at=oldest + timedelta(days=7 * i), src="native")
            for i in range(self.native_count)
        ]

    def build_prior(
        self,
        entity_id: str,
        day_of_week: int,
        slot: int,
        observations: Any,
    ) -> SlotPrior:
        obs = list(observations)
        if not obs:
            return SlotPrior.unavailable(entity_id, day_of_week, slot)
        mean, stddev, native = summarize(obs)
        return SlotPrior(
            entity_id=entity_id,
            day_of_week=day_of_week,
            slot=slot,
            mean_occupied=mean,
            stddev_occupied=stddev,
            native_count=native,
            sample_count=len(obs),
            confidence=confidence_for(native, len(obs), 3),
            last_sample_at=obs[-1].at,
        )


# ── Builders ──────────────────────────────────────────────────────────────────


def make_ctx(score: float = 70.0, degraded: bool = False, **kwargs: Any) -> Any:
    """Snapshot with the Saros Kitchen zone scored above its dispatch threshold."""
    ctx = make_snapshot(timestamp=NOW, **kwargs)
    ctx.zone_scores[ZONE] = score
    ctx.degraded = degraded
    return ctx


def make_opp_ctx(
    means: list[float] | None = None,
    *,
    stats: dict[str, Any] | None = GOOD_STATS,
    native_count: int = 4,
    age_days: float = 30.0,
    empty_after: int | None = None,
    cfg: VacuumOpsConfig | None = None,
) -> OpportunityContext:
    source = FakePriorSource(
        means=means if means is not None else [0.02] * 12,
        native_count=native_count,
        age_days=age_days,
        empty_after=empty_after,
    )
    return OpportunityContext(
        prior_source=source,
        cfg=cfg or VacuumOpsConfig(),
        prior_entity_id=ENTITY,
        zone_stats={ZONE: stats} if stats is not None else {},
        robot_stats={},
    )


# Slot-mean curves, forward from `now`. Index 0 is the CURRENT slot.
# Occupied now, clear from slot 2 on: a large gain against a poor current fit.
CURVE_BETTER_WINDOW = [0.97, 0.95, 0.02, 0.02, 0.02, 0.02, 0.02, 0.02, 0.02, 0.02, 0.02, 0.02]
# Clear throughout, with just enough variation to avoid the all-identical rule.
CURVE_FIT_OK = [0.02, 0.03, 0.02, 0.03, 0.02, 0.03, 0.02, 0.03, 0.02, 0.03, 0.02, 0.03]
# Mediocre now, and no materially better window ahead.
CURVE_FIT_MARGINAL = [0.55, 0.52, 0.53, 0.52, 0.53, 0.52, 0.53, 0.52, 0.53, 0.52, 0.53, 0.52]


async def verdict_for(curve: list[float] | None = None, **kwargs: Any) -> tuple[str, str, str]:
    job = Saros1FRoomsJob()
    return await opportunity_check(
        job, ZONE, make_ctx(), FakeRedis(), make_opp_ctx(curve, **kwargs)
    )


# ── Sanity: the curves really do drive the three verdicts ────────────────────


class TestVerdictFixtures:
    """If these drift, every behavioural test below silently stops testing."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("curve", "expected"),
        [
            (CURVE_BETTER_WINDOW, VERDICT_BETTER_WINDOW),
            (CURVE_FIT_OK, VERDICT_FIT_OK),
            (CURVE_FIT_MARGINAL, VERDICT_FIT_MARGINAL),
        ],
    )
    async def test_curve_produces_expected_verdict(self, curve: list[float], expected: str) -> None:
        _, _, reason = await verdict_for(curve)
        assert reason.startswith(f"opportunity_shadow:{expected}:"), reason


# ── Invariant 1 — the rule can never force a dispatch ────────────────────────


class TestInvariantCannotForceDispatch:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "curve", [CURVE_BETTER_WINDOW, CURVE_FIT_OK, CURVE_FIT_MARGINAL, [0.5] * 12]
    )
    async def test_result_is_always_a_legal_r1_triple(self, curve: list[float]) -> None:
        result, gate, reason = await verdict_for(curve)
        assert result in {"PASS", "FAIL", "AMBIGUOUS"}
        assert gate in {"none", "comfort"}
        assert isinstance(reason, str) and reason

    @pytest.mark.asyncio
    async def test_never_returns_a_non_comfort_gate(self) -> None:
        """It must not be able to claim authority over the effectiveness tier."""
        for curve in (CURVE_BETTER_WINDOW, CURVE_FIT_OK, CURVE_FIT_MARGINAL):
            _, gate, _ = await verdict_for(curve)
            assert gate != "effectiveness"

    @pytest.mark.asyncio
    async def test_cannot_rescue_a_zone_another_comfort_rule_failed(self) -> None:
        """A noise FAIL stays a FAIL even on the rule's most enthusiastic verdict.

        This is the concrete form of "can never force a dispatch": the rule's
        best possible answer must not be able to overturn another rule's veto.
        """
        ctx = make_ctx(rooms={**_sleeping_rooms()})
        job = Saros1FRoomsJob()
        result, gate, reason = await run_r1(
            job,
            ZONE,
            ctx,
            FakeRedis(),
            [],
            opp_ctx=make_opp_ctx(CURVE_FIT_OK),
        )
        assert result == "FAIL"
        assert gate == "comfort"
        assert "noise_radius_overlap" in reason

    @pytest.mark.asyncio
    async def test_actuating_verdicts_only_ever_withhold(self) -> None:
        """Even with A4's flag on, no verdict produces a dispatch-forcing result."""
        job = Saros1FRoomsJob(opportunity_actuate=True)
        outcomes = set()
        for curve in (CURVE_BETTER_WINDOW, CURVE_FIT_OK, CURVE_FIT_MARGINAL):
            result, gate, _ = await opportunity_check(
                job, ZONE, make_ctx(), FakeRedis(), make_opp_ctx(curve)
            )
            outcomes.add((result, gate))
        assert outcomes == {
            ("FAIL", "comfort"),  # better_window → defer
            ("AMBIGUOUS", "comfort"),  # fit_marginal → escalate to L1
            ("PASS", "none"),  # fit_ok → no opinion
        }


# ── Invariant 2 — effectiveness rules run first, and short-circuit ───────────


def _sleeping_rooms() -> dict[str, Any]:
    """A SLEEPING 1F room. Saros's noise_radius is "floor", so a sleeping 2F
    room would not trip noise_radius_check and the test would prove nothing."""
    rooms = {k: make_room("idle") for k in ("kitchen", "hallway", "bathroom")}
    rooms["living_room"] = make_room("sleeping")
    return rooms


class TestInvariantEffectivenessRunsFirst:
    @pytest.mark.asyncio
    async def test_occupied_zone_never_consults_the_prior_store(self) -> None:
        """THE 2026-08-31 CLASS. Measured occupancy must short-circuit first.

        Asserted on the prior source's read log, not on the return value: a
        degraded rule ALSO returns PASS, so "the result was a FAIL" would not
        distinguish "the gate ran first" from "the gate ran second and the
        opportunity rule happened to have no opinion". Zero reads is the claim.
        """
        rooms = {k: make_room("idle") for k in ("kitchen", "living_room", "hallway")}
        rooms["kitchen"] = make_room("active")
        ctx = make_ctx(rooms=rooms)
        opp_ctx = make_opp_ctx(CURVE_BETTER_WINDOW)

        result, gate, _ = await run_r1(
            Saros1FRoomsJob(), ZONE, ctx, FakeRedis(), [], opp_ctx=opp_ctx
        )

        assert result == "FAIL"
        assert gate == "effectiveness"
        assert opp_ctx.prior_source.reads == [], (
            "opportunity_check consulted the prior store on a zone the "
            "effectiveness gate had already rejected — the comfort tier is "
            "running before or alongside the occupancy gate"
        )

    @pytest.mark.asyncio
    async def test_floor_occupied_never_consults_the_prior_store(self) -> None:
        ctx = make_ctx(
            floor_occupancy={
                "1F": make_occupancy(
                    "binary_sensor.first_floor_occupancy_status",
                    occupied=True,
                    last_changed=NOW - timedelta(minutes=30),
                )
            },
        )
        opp_ctx = make_opp_ctx(CURVE_BETTER_WINDOW)
        result, gate, _ = await run_r1(
            Saros1FRoomsJob(), ZONE, ctx, FakeRedis(), [], opp_ctx=opp_ctx
        )
        assert (result, gate) == ("FAIL", "effectiveness")
        assert opp_ctx.prior_source.reads == []

    def test_source_order_pins_comfort_after_effectiveness(self) -> None:
        """A source-level tripwire against a silent reordering refactor.

        The runtime tests above prove the behaviour for the paths they exercise.
        This one guards the ORDERING ITSELF: if someone moves the
        `opportunity_check` CALL above the occupancy gates in `run_r1`, this
        fails even if they also "fixed" the behavioural tests, because the
        invariant is positional rather than observational.
        """
        import inspect

        from cortex_python.modules.vacuumops import r1

        src = inspect.getsource(r1.run_r1)
        # The CALL, not the mention in the sequencing docstring.
        call = src.index("await opportunity_check(")
        assert src.index("zone_active_use_check(job") < call
        assert src.index("floor_clearance_check(job") < call

        # And the effectiveness gates must still EXIT the function, not merely
        # run earlier — collecting their results instead of returning would
        # preserve the order while destroying the property it protects.
        head = src[:call]
        assert head.count("return result, gate_failed, reason") >= 3, (
            "the effectiveness rules no longer short-circuit out of run_r1"
        )


# ── Invariant 3 + the fail-open matrix ───────────────────────────────────────


class TestInvariantDegradationNamesItself:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("label", "kwargs", "cause"),
        [
            # Every entry of the spec's fail-open matrix, pinned to the exact
            # token that must reach the decision log. Asserting the SPECIFIC
            # cause rather than "some degradation happened" is the point: five
            # different failures collapsing into one indistinguishable string
            # would satisfy a looser test while leaving the A4 soak unable to
            # tell a cold learner from a dead HomeOps.
            ("prior_store_cold", {"native_count": 2}, "prior_thin"),
            ("learner_young", {"age_days": 3.0}, "learner_cold"),
            ("duration_unavailable", {"stats": None}, "no_active_duration"),
            ("all_slots_identical", {"curve": [0.4] * 12}, "priors_degenerate_uniform"),
            ("slot_missing", {"empty_after": 3}, "prior_slot_unavailable"),
        ],
    )
    async def test_degraded_path_passes_and_names_itself(
        self, label: str, kwargs: dict[str, Any], cause: str
    ) -> None:
        kwargs.setdefault("curve", CURVE_BETTER_WINDOW)
        result, gate, reason = await verdict_for(**kwargs)
        assert (result, gate) == ("PASS", "none"), f"{label} did not fail open"
        assert cause in reason, f"{label} degraded without naming '{cause}': {reason}"

    @pytest.mark.asyncio
    async def test_a_thin_read_can_never_defer_even_when_actuating(self) -> None:
        """A cold prior store is a PASS case in the spec, and PASS means PASS.

        A thin read cannot produce `better_window` (that requires "good"), so
        the only way it could act is by escalating a `fit_marginal` to L1. That
        would hand an LLM a forecast built on one or two observations and ask it
        to weigh it, which grants a thin prior more authority than its evidence
        supports. The rule declines outright instead.
        """
        job = Saros1FRoomsJob(opportunity_actuate=True)
        result, gate, reason = await opportunity_check(
            job,
            ZONE,
            make_ctx(),
            FakeRedis(),
            make_opp_ctx(CURVE_FIT_MARGINAL, native_count=2),
        )
        assert (result, gate) == ("PASS", "none")
        assert reason.startswith("opportunity_thin:")
        assert "prior_thin" in reason

    @pytest.mark.asyncio
    async def test_context_degraded_passes_and_names_itself(self) -> None:
        job = Saros1FRoomsJob()
        result, gate, reason = await opportunity_check(
            job, ZONE, make_ctx(degraded=True), FakeRedis(), make_opp_ctx(CURVE_BETTER_WINDOW)
        )
        assert (result, gate) == ("PASS", "none")
        assert reason == "opportunity_unavailable:ctx_degraded"

    @pytest.mark.asyncio
    async def test_no_wired_prior_source_names_itself(self) -> None:
        result, gate, reason = await opportunity_check(
            Saros1FRoomsJob(), ZONE, make_ctx(), FakeRedis(), None
        )
        assert (result, gate) == ("PASS", "none")
        assert reason == "opportunity_inert:no_prior_source"

    @pytest.mark.asyncio
    async def test_disabled_job_names_itself(self) -> None:
        result, gate, reason = await opportunity_check(
            Sam2FJob(), ZONE, make_ctx(), FakeRedis(), make_opp_ctx(CURVE_BETTER_WINDOW)
        )
        assert (result, gate) == ("PASS", "none")
        assert reason == "opportunity_disabled"

    @pytest.mark.asyncio
    async def test_an_exception_inside_the_rule_fails_open_and_names_itself(self) -> None:
        """A bug in a log-only rule must not become a suppressed dispatch."""

        class Exploding:
            async def read_observations(self, *_: Any) -> list[PriorObservation]:
                raise RuntimeError("prior store on fire")

            def build_prior(self, *_: Any) -> SlotPrior:  # pragma: no cover
                raise AssertionError("unreachable")

        opp_ctx = OpportunityContext(
            prior_source=Exploding(),
            cfg=VacuumOpsConfig(),
            prior_entity_id=ENTITY,
            zone_stats={ZONE: GOOD_STATS},
        )
        result, gate, reason = await opportunity_check(
            Saros1FRoomsJob(), ZONE, make_ctx(), FakeRedis(), opp_ctx
        )
        assert (result, gate) == ("PASS", "none")
        assert reason.startswith("opportunity_error:RuntimeError")

    @pytest.mark.asyncio
    async def test_every_reachable_reason_is_in_the_declared_fail_open_set(self) -> None:
        """Invariant 3, checked as a SET rather than case by case.

        `_OPPORTUNITY_FAIL_OPEN` is the rule's own declaration of every way it
        is allowed to decline. If a future change adds a new silent PASS path
        without declaring it, this catches it.
        """
        job = Saros1FRoomsJob()
        observed = set()
        cases: list[tuple[Any, Any, Any]] = [
            (job, make_ctx(), None),
            (Sam2FJob(), make_ctx(), make_opp_ctx()),
            (job, make_ctx(score=10.0), make_opp_ctx()),
            (job, make_ctx(degraded=True), make_opp_ctx()),
            (job, make_ctx(), make_opp_ctx(stats=None)),
            (job, make_ctx(), make_opp_ctx(native_count=1)),
            (job, make_ctx(), make_opp_ctx(age_days=1.0)),
            (job, make_ctx(), make_opp_ctx(CURVE_BETTER_WINDOW)),
            (job, make_ctx(), make_opp_ctx(CURVE_FIT_OK)),
            (job, make_ctx(score=99.0), make_opp_ctx(CURVE_FIT_OK)),
        ]
        for a_job, ctx, opp in cases:
            result, _, reason = await opportunity_check(a_job, ZONE, ctx, FakeRedis(), opp)
            assert result == "PASS", reason
            observed.add(reason.split(":", 1)[0])
        undeclared = observed - set(_OPPORTUNITY_FAIL_OPEN)
        assert not undeclared, f"undeclared fail-open reasons: {undeclared}"


class TestFailOpenMatrix:
    """The five named degradations from §4.4, each pinned to its own cause."""

    @pytest.mark.asyncio
    async def test_prior_store_cold_below_min_slot_samples(self) -> None:
        """Under `opportunity_min_slot_samples` native reads, a slot is "thin".

        Thin is deliberately not the same as unavailable: the read is still
        computed and still logged (the soak wants to see it), it simply may
        never justify withholding a dispatch. `conf=thin` in the reason string
        is what a reviewer greps for.
        """
        _, _, reason = await verdict_for(CURVE_BETTER_WINDOW, native_count=2)
        assert "conf=thin" in reason
        assert "prior_thin" in reason
        # And crucially: it did NOT reach the one verdict that can defer.
        assert VERDICT_BETTER_WINDOW not in reason.split("degraded=")[0]

    @pytest.mark.asyncio
    async def test_learner_younger_than_min_learn_days(self) -> None:
        _, _, reason = await verdict_for(CURVE_BETTER_WINDOW, age_days=5.0)
        assert "learner_cold" in reason

    @pytest.mark.asyncio
    async def test_duration_estimate_unavailable(self) -> None:
        _, _, reason = await verdict_for(CURVE_BETTER_WINDOW, stats=None)
        assert "no_active_duration" in reason

    @pytest.mark.asyncio
    async def test_wall_clock_only_stats_are_not_a_usable_duration(self) -> None:
        """A payload with ONLY `avg_duration_min` must degrade, not be used.

        The A0 finding: wall-clock duration is ~70% larger than active duration,
        so accepting it here would over-reserve every window and turn the rule
        into a permanent deferral. A2 bans it structurally; this pins that the
        ban survives the trip through A3's adapter layer.
        """
        _, _, reason = await verdict_for(
            CURVE_BETTER_WINDOW, stats={"avg_duration_min": 44.8, "p75_duration_min": 60.0}
        )
        assert "no_active_duration" in reason

    @pytest.mark.asyncio
    async def test_all_consulted_slots_identical_is_no_information(self) -> None:
        _, _, reason = await verdict_for([0.5] * 12)
        assert "identical" in reason or "no_information" in reason or "unavailable" in reason

    @pytest.mark.asyncio
    async def test_an_empty_slot_is_not_a_clear_slot(self) -> None:
        """EMPTY IS NOT ZERO, at the A3 boundary.

        A slot with no observations carries `mean_occupied = 0.0`, which reads
        naively as p_clear = 1.0 — "always free, dispatch now". A missing slot
        anywhere in the consulted set must collapse the read, never improve it.
        """
        _, _, reason = await verdict_for(CURVE_FIT_MARGINAL, empty_after=4)
        assert reason.startswith("opportunity_unavailable:")
        assert VERDICT_FIT_OK not in reason


# ── patience() at the I/O boundary — judgment call 3 ─────────────────────────


class TestStarvationClock:
    @pytest.mark.asyncio
    async def test_first_crossing_seeds_the_clock_and_reads_it_back(self) -> None:
        redis = FakeRedis()
        await verdict_for_with(redis, CURVE_FIT_OK)
        assert over_threshold_since_key(ZONE) in redis.data

    @pytest.mark.asyncio
    async def test_clock_is_not_reseeded_on_later_ticks(self) -> None:
        """SETNX, not SET. Re-stamping it every tick would disable the hard cap."""
        redis = FakeRedis()
        await verdict_for_with(redis, CURVE_FIT_OK)
        first = redis.data[over_threshold_since_key(ZONE)]
        await verdict_for_with(redis, CURVE_FIT_OK)
        assert redis.data[over_threshold_since_key(ZONE)] == first

    @pytest.mark.asyncio
    async def test_unreadable_clock_resolves_to_impatient_not_patient(self) -> None:
        """⚠ THE DANGEROUS DIRECTION. An unknown clock must make the rule INERT.

        If a Redis failure resolved to "no time has passed" instead of "no
        clock", the 6-hour starvation cap could never fire and a zone could be
        deferred indefinitely. `patience()` returns IMPATIENT for None; this
        pins that A3's read path actually delivers None rather than inventing a
        timestamp on the way.
        """
        redis = FakeRedis()
        redis.fail_on = {"get", "set"}
        result, gate, reason = await verdict_for_with(redis, CURVE_BETTER_WINDOW)
        assert (result, gate) == ("PASS", "none")
        assert reason == "opportunity_impatient:impatient:no_threshold_clock"

    @pytest.mark.asyncio
    async def test_corrupt_clock_value_resolves_to_impatient(self) -> None:
        redis = FakeRedis({over_threshold_since_key(ZONE): "not-a-timestamp"})
        _, _, reason = await verdict_for_with(redis, CURVE_BETTER_WINDOW)
        assert reason == "opportunity_impatient:impatient:no_threshold_clock"

    @pytest.mark.asyncio
    async def test_hard_cap_makes_the_rule_inert(self) -> None:
        """Past `patience_hard_cap_h` the zone stops waiting, whatever the forecast."""
        stale = (NOW - timedelta(hours=7)).isoformat()
        redis = FakeRedis({over_threshold_since_key(ZONE): stale})
        result, _, reason = await verdict_for_with(redis, CURVE_BETTER_WINDOW)
        assert result == "PASS"
        assert reason == "opportunity_impatient:impatient:hard_cap"

    @pytest.mark.asyncio
    async def test_impatient_score_band_makes_the_rule_inert(self) -> None:
        job = Saros1FRoomsJob()
        result, _, reason = await opportunity_check(
            job, ZONE, make_ctx(score=90.0), FakeRedis(), make_opp_ctx(CURVE_BETTER_WINDOW)
        )
        assert result == "PASS"
        assert reason == "opportunity_impatient:impatient:score"

    @pytest.mark.asyncio
    async def test_impatient_zone_does_no_prior_reads(self) -> None:
        """Patience is evaluated first, so an inert zone costs no database I/O."""
        stale = (NOW - timedelta(hours=9)).isoformat()
        redis = FakeRedis({over_threshold_since_key(ZONE): stale})
        opp_ctx = make_opp_ctx(CURVE_BETTER_WINDOW)
        await opportunity_check(Saros1FRoomsJob(), ZONE, make_ctx(), redis, opp_ctx)
        assert opp_ctx.prior_source.reads == []


async def verdict_for_with(
    redis: FakeRedis, curve: list[float], **kwargs: Any
) -> tuple[str, str, str]:
    return await opportunity_check(
        Saros1FRoomsJob(), ZONE, make_ctx(), redis, make_opp_ctx(curve, **kwargs)
    )


# ── The chained-deferral instrument (§4.4 — "do not merge A3 without it") ────


class TestDeferStreak:
    KEY = _OPPORTUNITY_DEFER_STREAK_KEY.format(zone_id=ZONE)

    @pytest.mark.asyncio
    async def test_better_window_increments_the_streak(self) -> None:
        redis = FakeRedis()
        for expected in (1, 2, 3):
            _, _, reason = await verdict_for_with(redis, CURVE_BETTER_WINDOW)
            assert redis.data[self.KEY] == str(expected)
            assert f"streak={expected}" in reason

    @pytest.mark.asyncio
    async def test_the_streak_is_counted_in_shadow_mode(self) -> None:
        """The instrument must work while `opportunity_actuate` is False.

        This is the whole reason the counter ships with A3 rather than A4 — the
        A4 go/no-go reads it out of a soak that runs entirely in shadow. A
        counter that only moved once actuation was on would be useless for
        deciding whether to turn actuation on.
        """
        redis = FakeRedis()
        job = Saros1FRoomsJob()
        assert job.opportunity_actuate is False
        for _ in range(4):
            await opportunity_check(job, ZONE, make_ctx(), redis, make_opp_ctx(CURVE_BETTER_WINDOW))
        assert redis.data[self.KEY] == "4"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("curve", [CURVE_FIT_OK, CURVE_FIT_MARGINAL])
    async def test_a_non_deferring_verdict_clears_the_streak(self, curve: list[float]) -> None:
        redis = FakeRedis()
        await verdict_for_with(redis, CURVE_BETTER_WINDOW)
        await verdict_for_with(redis, CURVE_BETTER_WINDOW)
        assert redis.data[self.KEY] == "2"
        await verdict_for_with(redis, curve)
        assert self.KEY not in redis.data

    @pytest.mark.asyncio
    async def test_a_degraded_read_clears_the_streak(self) -> None:
        """A streak is consecutive DEFERRALS, not consecutive non-dispatches."""
        redis = FakeRedis()
        await verdict_for_with(redis, CURVE_BETTER_WINDOW)
        await verdict_for_with(redis, CURVE_BETTER_WINDOW, stats=None)
        assert self.KEY not in redis.data

    @pytest.mark.asyncio
    async def test_streak_key_gets_a_ttl(self) -> None:
        redis = FakeRedis()
        await verdict_for_with(redis, CURVE_BETTER_WINDOW)
        assert redis.ttls.get(self.KEY, 0) > 0

    @pytest.mark.asyncio
    async def test_streak_failure_never_breaks_the_tick(self) -> None:
        redis = FakeRedis()
        redis.fail_on = {"incr"}
        result, _, reason = await verdict_for_with(redis, CURVE_BETTER_WINDOW)
        assert result == "PASS"
        assert "streak=0" in reason

    @pytest.mark.asyncio
    async def test_dispatch_clears_the_starvation_clock(self) -> None:
        from cortex_python.modules.vacuumops.loop import _clear_opportunity_zone_state
        from cortex_python.modules.vacuumops.schemas import BatchEntry

        redis = FakeRedis({over_threshold_since_key(ZONE): NOW.isoformat(), self.KEY: "3"})
        await _clear_opportunity_zone_state([BatchEntry(zone=ZONE, bundled=False, score=70.0)], redis)
        assert over_threshold_since_key(ZONE) not in redis.data
        # Shadow mode: the streak SURVIVES the dispatch, because in shadow the
        # dispatch happened despite the verdict. Clearing it here would pin the
        # counter at 1 and blind the A4 decision. See
        # `loop._clear_opportunity_zone_state`.
        assert redis.data[self.KEY] == "3"

    @pytest.mark.asyncio
    async def test_dispatch_clears_the_streak_once_actuating(self) -> None:
        """Under A4 the literal §4.4 semantics ("cleared on dispatch") apply."""
        import cortex_python.modules.vacuumops.loop as loop_mod
        from cortex_python.modules.vacuumops.schemas import BatchEntry

        redis = FakeRedis({over_threshold_since_key(ZONE): NOW.isoformat(), self.KEY: "3"})
        original = loop_mod.ACTIVE_JOBS
        loop_mod.ACTIVE_JOBS = [Saros1FRoomsJob(opportunity_actuate=True)]
        try:
            await loop_mod._clear_opportunity_zone_state(
                [BatchEntry(zone=ZONE, bundled=False, score=70.0)], redis
            )
        finally:
            loop_mod.ACTIVE_JOBS = original
        assert self.KEY not in redis.data


# ── Shadow mode — the safety promise of this entire PR ───────────────────────


class TestShadowMode:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "curve", [CURVE_BETTER_WINDOW, CURVE_FIT_OK, CURVE_FIT_MARGINAL, [0.5] * 12]
    )
    async def test_shadow_mode_can_never_change_a_verdict(self, curve: list[float]) -> None:
        """Across the full verdict cross-product, A3 always returns PASS."""
        job = Saros1FRoomsJob()
        assert job.opportunity_actuate is False
        result, gate, _ = await opportunity_check(
            job, ZONE, make_ctx(), FakeRedis(), make_opp_ctx(curve)
        )
        assert (result, gate) == ("PASS", "none")

    @pytest.mark.asyncio
    async def test_run_r1_dispatches_a_zone_the_rule_would_have_deferred(self) -> None:
        """End-to-end: the strongest possible defer signal still PASSes R1."""
        result, gate, reason = await run_r1(
            Saros1FRoomsJob(),
            ZONE,
            make_ctx(),
            FakeRedis(),
            [],
            opp_ctx=make_opp_ctx(CURVE_BETTER_WINDOW),
        )
        assert (result, gate) == ("PASS", "none")
        assert "opportunity_shadow:better_window" in reason
        assert "all_rules_pass" in reason

    @pytest.mark.asyncio
    async def test_the_shadow_verdict_reaches_the_decision_log_reason(self) -> None:
        """The soak reads this string out of `get_vacuum_decisions()`."""
        _, _, reason = await run_r1(
            Saros1FRoomsJob(),
            ZONE,
            make_ctx(),
            FakeRedis(),
            [],
            opp_ctx=make_opp_ctx(CURVE_BETTER_WINDOW),
        )
        for token in ("opportunity_shadow", "fit_now=", "conf=", "streak="):
            assert token in reason, f"{token} missing from decision-log reason: {reason}"

    @pytest.mark.asyncio
    async def test_run_r1_is_unchanged_when_no_context_is_supplied(self) -> None:
        """Every pre-A3 caller keeps working, and says so in the log."""
        result, gate, reason = await run_r1(Saros1FRoomsJob(), ZONE, make_ctx(), FakeRedis(), [])
        assert (result, gate) == ("PASS", "none")
        assert "opportunity_inert:no_prior_source" in reason

    @pytest.mark.asyncio
    async def test_a_disabled_job_adds_no_noise_to_the_reason(self) -> None:
        ctx = make_ctx()
        ctx.zone_scores[14] = 75.0
        _, _, reason = await run_r1(
            Ethan3FLitterBoxJob(), 14, ctx, FakeRedis(), [], opp_ctx=make_opp_ctx()
        )
        assert reason == "all_rules_pass"


# ── Per-job flags (§4.4) ──────────────────────────────────────────────────────


class TestPerJobFlags:
    def test_only_saros_1f_rooms_is_enabled(self) -> None:
        assert Saros1FRoomsJob().opportunity_enabled is True
        for job in (
            Saros1FLitterBoxJob(),
            Ethan3FLitterBoxJob(),
            Ethan3FRoomsJob(),
            Sam2FJob(),
        ):
            assert job.opportunity_enabled is False, job.job_id

    def test_no_job_actuates(self) -> None:
        """⛔ A4's content. If this fails, a PR is changing live dispatch."""
        for job in (
            Saros1FRoomsJob(),
            Saros1FLitterBoxJob(),
            Ethan3FLitterBoxJob(),
            Ethan3FRoomsJob(),
            Sam2FJob(),
        ):
            assert job.opportunity_actuate is False, job.job_id

    def test_the_active_roster_matches(self) -> None:
        from cortex_python.modules.vacuumops.loop import ACTIVE_JOBS

        enabled = [j.job_id for j in ACTIVE_JOBS if j.opportunity_enabled]
        assert enabled == ["saros_1f_rooms"]
        assert not any(j.opportunity_actuate for j in ACTIVE_JOBS)


# ── Bundling (§5.5) — the guard is structural ────────────────────────────────


class TestBundlingIsStructurallyProtected:
    def test_bundle_sweep_never_consults_opportunity_check(self) -> None:
        """§5.5: the rule must never be able to shrink a batch.

        §4.4's pseudocode asked for an `_is_bundled(...)` runtime guard. That
        predicate is unwritable — bundling is decided in `assemble_batch`, after
        every zone outcome exists — and it is also unnecessary, because the
        bundle sweep re-checks eligibility through its own simple helpers and
        never calls `run_r1`. This pins the control-flow property the runtime
        guard was standing in for.
        """
        import inspect

        from cortex_python.modules.vacuumops import loop as loop_mod

        src = inspect.getsource(loop_mod.assemble_batch)
        assert "run_r1" not in src
        assert "opportunity_check" not in src
        assert "_zone_effective_simple" in src

    @pytest.mark.asyncio
    async def test_a_below_threshold_zone_is_skipped_by_the_guard(self) -> None:
        opp_ctx = make_opp_ctx(CURVE_BETTER_WINDOW)
        result, gate, reason = await opportunity_check(
            Saros1FRoomsJob(), ZONE, make_ctx(score=10.0), FakeRedis(), opp_ctx
        )
        assert (result, gate) == ("PASS", "none")
        assert reason == "opportunity_skipped:below_dispatch_threshold"
        assert opp_ctx.prior_source.reads == []


# ── The L1 hand-off ───────────────────────────────────────────────────────────


class TestOpportunityReadReachesL1:
    @pytest.mark.asyncio
    async def test_the_read_is_deposited_for_the_prompt(self) -> None:
        opp_ctx = make_opp_ctx(CURVE_BETTER_WINDOW)
        await opportunity_check(Saros1FRoomsJob(), ZONE, make_ctx(), FakeRedis(), opp_ctx)
        read = opp_ctx.reads.get(ZONE)
        assert read is not None
        assert read.confidence == CONFIDENCE_GOOD
        assert read.best_slot_offset is not None

    @pytest.mark.asyncio
    async def test_an_unavailable_read_is_still_deposited(self) -> None:
        """L1 most needs to know when the forecast is untrustworthy."""
        opp_ctx = make_opp_ctx(CURVE_BETTER_WINDOW, stats=None)
        await opportunity_check(Saros1FRoomsJob(), ZONE, make_ctx(), FakeRedis(), opp_ctx)
        read = opp_ctx.reads.get(ZONE)
        assert read is not None
        assert read.confidence == CONFIDENCE_UNAVAILABLE
        assert read.degraded_reason

    def test_every_prompt_template_renders_the_read(self) -> None:
        """StrictUndefined: the variable must be in ALL five templates or none."""
        from pathlib import Path

        from cortex_python.modules.vacuumops import loop as loop_mod

        prompts = Path(loop_mod.__file__).parent / "prompts"
        files = sorted(prompts.glob("*.md"))
        assert len(files) == 5
        for path in files:
            assert "{{ opportunity_read }}" in path.read_text(encoding="utf-8"), path.name

    def test_the_read_is_part_of_the_l1_cache_key(self) -> None:
        """A changed forecast must bust the 600s L1 cache, not be masked by it."""
        from cortex_python.modules.vacuumops.l1 import _build_context_hash
        from cortex_python.modules.vacuumops.schemas import OpportunityRead

        job = Saros1FRoomsJob()
        ctx = make_ctx()

        def read(fit: float) -> OpportunityRead:
            return OpportunityRead(
                p_clear_now=fit,
                p_clear_curve=(fit,),
                expected_fit_now=fit,
                best_slot_offset=2,
                best_slot_gain=0.5,
                duration_estimate_min=30.0,
                duration_basis="p75_active+return_leg",
                confidence=CONFIDENCE_GOOD,
            )

        base = _build_context_hash(job, ZONE, ctx)
        poor = _build_context_hash(job, ZONE, ctx, opportunity_read=read(0.10))
        good = _build_context_hash(job, ZONE, ctx, opportunity_read=read(0.95))
        assert len({base, poor, good}) == 3


# ── Mission-stats sourcing ────────────────────────────────────────────────────


class TestMissionStatsWiring:
    @pytest.mark.asyncio
    async def test_context_is_none_when_no_job_is_enabled(self) -> None:
        from cortex_python.modules.vacuumops.loop import build_opportunity_context

        out = await build_opportunity_context(
            ctx=make_ctx(),
            jobs=[Sam2FJob(), Ethan3FRoomsJob()],
            prior_source=object(),
            cfg=VacuumOpsConfig(),
            homeops_adapter=AsyncMock(),
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_context_points_at_the_gate_signal_entity(self) -> None:
        """The reader must consult the SAME entity A1 learns, not a member area."""
        from cortex_python.modules.vacuumops.loop import build_opportunity_context

        adapter = AsyncMock()
        adapter.get_mission_stats = AsyncMock(return_value=dict(GOOD_STATS))
        ctx = make_ctx()
        out = await build_opportunity_context(
            ctx=ctx,
            jobs=[Saros1FRoomsJob()],
            prior_source=object(),
            cfg=VacuumOpsConfig(),
            homeops_adapter=adapter,
            now_monotonic=1.0,
        )
        assert out is not None
        assert out.prior_entity_id == ENTITY
        assert out.prior_entity_id == VacuumOpsConfig().prior_learner_entities[0]

    @pytest.mark.asyncio
    async def test_a_failing_stats_read_degrades_rather_than_raising(self) -> None:
        from cortex_python.modules.vacuumops.loop import build_opportunity_context

        adapter = AsyncMock()
        adapter.get_mission_stats = AsyncMock(side_effect=RuntimeError("homeops down"))
        out = await build_opportunity_context(
            ctx=make_ctx(),
            jobs=[Saros1FRoomsJob()],
            prior_source=object(),
            cfg=VacuumOpsConfig(),
            homeops_adapter=adapter,
            now_monotonic=2.0,
        )
        assert out is not None
        assert out.zone_stats == {}
        assert out.robot_stats == {}
