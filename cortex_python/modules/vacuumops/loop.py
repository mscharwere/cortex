"""VacuumOps adaptive polling loop.

The main dispatch brain. Runs forever as an asyncio background task registered
via FastAPI's lifespan hook in api/main.py.

Per-tick sequence (§8.3):
  1. Build ContextSnapshot via synth
  2. Per (job, zone): R0 → R1 → L1
  3. Per-robot batch assembly (D10 + D11)
  4. If batch: single POST /api/vacuum/trigger
  5. Persist to decision_log + vac_decisions
  6. Publish loop status sensor to HA

Adaptive cadence (§8.2):
  Robot cleaning/returning → 300s
  Any robot cooldown active → 300s
  All people away + no events → 120s
  L1 timeout in last 2 ticks → 180s
  Default → 60s

Spec: C:/Jarvis/Team/TARS/cortex_vacuumops_module_spec.md §8
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import redis.asyncio as aioredis
import structlog
import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cortex_python.adapters.litellm_client import build_litellm_client
from cortex_python.config.settings import Settings
from cortex_python.modules.vacuumops.config import VacuumOpsConfig, build_vacuumops_config
from cortex_python.modules.vacuumops.jobs import (
    Ethan3FLitterBoxJob,
    Ethan3FRoomsJob,
    Sam2FJob,
    Saros1FLitterBoxJob,
    Saros1FRoomsJob,
    VacuumJob,
)
from cortex_python.modules.vacuumops.l1 import L1Decision, resolve_params, resolve_zone_meta, run_l1
from cortex_python.modules.vacuumops.mop import resolve_batch_mop
from cortex_python.modules.vacuumops.opportunity import over_threshold_since_key
from cortex_python.modules.vacuumops.priors import (
    BACKFILL_DONE_KEY,
    PriorLearner,
    PriorStore,
)
from cortex_python.modules.vacuumops.priors_backfill import backfill_priors
from cortex_python.modules.vacuumops.r0 import _ZONE_COOLDOWN_KEY as _R0_ZONE_COOLDOWN_KEY
from cortex_python.modules.vacuumops.r0 import run_r0
from cortex_python.modules.vacuumops.r1 import (
    _OPPORTUNITY_DEFER_STREAK_KEY,
    OpportunityContext,
    occupancy_gate_bypass,
    run_r1,
)
from cortex_python.modules.vacuumops.r1 import _ROBOT_COOLDOWN_KEY as _R1_ROBOT_COOLDOWN_KEY
from cortex_python.modules.vacuumops.schemas import (
    BatchEntry,
    ContextSnapshot,
    DecisionEntry,
    DropRecord,
    MopDecision,
    ZoneDecisionDetail,
    ZoneOutcome,
)
from cortex_python.modules.vacuumops.utils import parse_pattern_time as _parse_pattern_time_util

if TYPE_CHECKING:
    # Type-only: the adapters are imported lazily inside vacuumops_loop() to
    # avoid a circular import at module load, and this annotation must not
    # reintroduce one.
    from cortex_python.adapters.homeops_adapter import VacuumOpsLiveSettings

log = structlog.get_logger()

ACTIVE_JOBS: list[VacuumJob] = [
    Ethan3FLitterBoxJob(),
    Ethan3FRoomsJob(),
    Saros1FLitterBoxJob(),
    Saros1FRoomsJob(),
    Sam2FJob(),
]

# ── Pattern loading ────────────────────────────────────────────────────────────

_PATTERNS_FILE = Path(__file__).parent / "patterns.yaml"
_PROMPT_FILES: dict[str, str] = {}  # job_id → rendered template string

_patterns: list[dict] = []
_patterns_mtime: float = 0.0


def _load_patterns() -> list[dict]:
    """Load patterns.yaml, hot-reloading on mtime change."""
    global _patterns, _patterns_mtime
    try:
        mtime = _PATTERNS_FILE.stat().st_mtime
        if mtime != _patterns_mtime:
            with open(_PATTERNS_FILE) as f:
                data = yaml.safe_load(f)
            _patterns = data.get("patterns", [])
            _patterns_mtime = mtime
            log.info("patterns_loaded", count=len(_patterns))
    except Exception as exc:
        log.warning("patterns_load_failed", error=str(exc))
    return _patterns


def _load_prompt_template(job: VacuumJob) -> str:
    """Load the prompt template for a job. Cached after first load."""
    if job.job_id not in _PROMPT_FILES:
        prompt_path = Path(__file__).parent / job.prompt_file
        try:
            _PROMPT_FILES[job.job_id] = prompt_path.read_text(encoding="utf-8")
        except Exception as exc:
            log.error(
                "prompt_load_failed",
                job_id=job.job_id,
                path=str(prompt_path),
                error=str(exc),
            )
            _PROMPT_FILES[job.job_id] = ""
    return _PROMPT_FILES[job.job_id]


def _parse_pattern_time(current_date: date, time_str: str) -> datetime:
    """Parse HH:MM string into a UTC datetime on current_date.

    Thin wrapper around utils.parse_pattern_time — kept here so r1.py can
    import it from loop.py if needed, but the canonical implementation lives
    in utils.py to avoid circular imports.
    """
    return _parse_pattern_time_util(current_date, time_str)


def render_patterns_for(job: VacuumJob, current_time: datetime, patterns: list[dict]) -> str:
    """Two-dimensional filter: job relevance + temporal relevance.

    Returns a rendered string for injection into the L1 prompt's patterns_block
    placeholder.

    Temporal windows per relevance type:
      transit: [start - 15min, end + 5min]  — forward-biased
      noise:   [start - 20min, end + 5min]  — forward-biased
      window:  [start - 5min,  end + 5min]  — symmetric

    Spec: §9
    """
    rendered = []
    weekday = current_time.isoweekday()  # 1=Mon..7=Sun
    current_date = current_time.date()

    for p in patterns:
        # Job-relevance gate
        jobs_field = p.get("jobs", [])
        if not ("*" in jobs_field or job.job_id in jobs_field):
            continue

        # Day-of-week gate
        if weekday not in p.get("days", []):
            continue

        try:
            start = _parse_pattern_time(current_date, p["start"])
            end = _parse_pattern_time(current_date, p["end"])
        except Exception:
            continue

        rel = p.get("relevance", [])
        if "transit" in rel:
            lookback = timedelta(minutes=15)
            lookahead = timedelta(minutes=5)
        elif "noise" in rel:
            lookback = timedelta(minutes=20)
            lookahead = timedelta(minutes=5)
        elif "window" in rel:
            lookback = timedelta(minutes=5)
            lookahead = timedelta(minutes=5)
        else:
            continue

        if start - lookback <= current_time <= end + lookahead:
            rendered.append(f"- [{p['name']}] {p['description']}")

    return "\n".join(rendered) if rendered else "- (no patterns active for this window)"


# ── Per-zone evaluation ───────────────────────────────────────────────────────


async def evaluate_zone(
    job: VacuumJob,
    zone: int,
    ctx: ContextSnapshot,
    redis_client: aioredis.Redis,
    settings: Settings,
    litellm_client: Any,
    vacuumops_cfg: VacuumOpsConfig,
    patterns: list[dict],
    l1_results: dict[tuple[str, int], L1Decision] | None = None,
    opp_ctx: OpportunityContext | None = None,
) -> ZoneOutcome:
    """Evaluate one (job, zone_id) pair through R0 → R1 → L1.

    Returns a ZoneOutcome (never raises; all failures are captured as outcomes).
    Batching happens upstream in the main loop.

    Spec: §8.3 evaluate_zone pseudocode
    """
    zone_id = zone  # alias for clarity throughout
    score = ctx.zone_scores.get(zone_id, 0.0)

    # R0 — hard gates
    try:
        r0_passed, r0_reason = await run_r0(job, zone_id, ctx, redis_client)
    except Exception as exc:
        log.exception("r0_error", job_id=job.job_id, zone_id=zone_id)
        return ZoneOutcome(
            zone=zone_id,
            action="defer",
            tier="R0",
            gate_failed="r0",
            reason=f"r0_exception:{exc!s}",
            score=score,
        )

    if not r0_passed:
        return ZoneOutcome(
            zone=zone_id,
            action="defer",
            tier="R0",
            gate_failed="r0",
            reason=r0_reason,
            score=score,
        )

    # ── Occupancy-gate override — compute once per (zone, tick) ─────────────
    zone_meta_for_bypass = resolve_zone_meta(zone_id, ctx)
    bypass_mode, bypass_reason_str = occupancy_gate_bypass(zone_id, ctx, zone_meta_for_bypass)
    bypassed_for_zone = bypass_mode != "none"

    if bypassed_for_zone:
        log.info(
            "occupancy_gate_bypassed",
            job_id=job.job_id,
            zone_id=zone_id,
            mode=bypass_mode,
            reason=bypass_reason_str,
            tick_id=ctx.tick_id,
        )

    # R1 — two-gate rules
    try:
        r1_result, r1_gate_failed, r1_reason = await run_r1(
            job,
            zone_id,
            ctx,
            redis_client,
            patterns,
            zone_meta=zone_meta_for_bypass,
            bypass_mode=bypass_mode,
            bypass_reason_str=bypass_reason_str,
            opp_ctx=opp_ctx,
        )
    except Exception as exc:
        log.exception("r1_error", job_id=job.job_id, zone_id=zone_id)
        return ZoneOutcome(
            zone=zone_id,
            action="defer",
            tier="R1",
            gate_failed="effectiveness",
            reason=f"r1_exception:{exc!s}",
            score=score,
        )

    if r1_result == "FAIL":
        return ZoneOutcome(
            zone=zone_id,
            action="defer",
            tier="R1",
            gate_failed=r1_gate_failed,
            reason=r1_reason,
            score=score,
        )

    if r1_result == "PASS" and not job.l1_required:
        # All rules passed strongly — no L1 needed
        return ZoneOutcome(
            zone=zone_id,
            action="dispatch",
            tier="R1",
            gate_failed="none",
            reason=r1_reason,  # may include occ_bypass tag for observability
            score=score,
        )

    # R1 AMBIGUOUS or l1_required — escalate to L1 (D13)
    try:
        prompt_template = _load_prompt_template(job)
        patterns_block = render_patterns_for(job, ctx.timestamp, patterns)
        l1_decision = await run_l1(
            job=job,
            zone=zone_id,
            ctx=ctx,
            marginal_result=(r1_result, r1_gate_failed, r1_reason),
            settings=settings,
            litellm_client=litellm_client,
            redis_client=redis_client,
            prompt_template=prompt_template,
            patterns_block=patterns_block,
            bypassed_for_zone=bypassed_for_zone,
            reason_for_zone=bypass_reason_str,
            # Spec §4.4: "the OpportunityRead goes into the L1 prompt verbatim".
            # None whenever the rule did not form a read for this zone.
            opportunity_read=opp_ctx.reads.get(zone_id) if opp_ctx else None,
        )
    except Exception as exc:
        log.exception("l1_error", job_id=job.job_id, zone_id=zone_id)
        return ZoneOutcome(
            zone=zone_id,
            action="defer",
            tier="L1",
            gate_failed="l1",
            reason=f"l1_exception:{exc!s}",
            score=score,
        )

    # Record L1 decision for all outcomes — persist_decision uses this for zone details
    if l1_results is not None:
        l1_results[(job.job_id, zone_id)] = l1_decision

    # Low-confidence check (Phase 1: no AIT overflow queue — just defer and log)
    if l1_decision.confidence < vacuumops_cfg.l1_overflow_confidence:
        log.info(
            "l1_low_confidence",
            job_id=job.job_id,
            zone_id=zone_id,
            confidence=l1_decision.confidence,
            threshold=vacuumops_cfg.l1_overflow_confidence,
        )
        return ZoneOutcome(
            zone=zone_id,
            action="defer",
            tier="L1",
            gate_failed="l1",
            reason="l1_low_confidence",
            score=score,
            l1_confidence=l1_decision.confidence,
        )

    if l1_decision.decision == "defer":
        return ZoneOutcome(
            zone=zone_id,
            action="defer",
            tier="L1",
            gate_failed="comfort",
            reason=l1_decision.reason,
            score=score,
            l1_confidence=l1_decision.confidence,
        )

    return ZoneOutcome(
        zone=zone_id,
        action="dispatch",
        tier="L1",
        gate_failed="none",
        reason=l1_decision.reason,
        score=score,
        l1_confidence=l1_decision.confidence,
    )


# ── Containment dedup ─────────────────────────────────────────────────────────


def dedup_contained(
    candidates: list[ZoneOutcome], zone_meta: dict
) -> tuple[list[ZoneOutcome], list[DropRecord]]:
    """Drop child zones whose parent is also a candidate in the same tick (same job_id).

    A child zone is identified by zone_meta[zone_id].contained_by being non-None
    and pointing to a zone_id whose job is also in the candidate set.

    If the parent zone is not dispatchable (e.g. virtual/aggregate), the child
    is kept rather than dropped.

    Returns (kept, dropped) where dropped entries are logged as containment_dedup.
    """
    # Phase 2 TODO: match candidates by zone_id once ZoneMeta gains a label field

    if not zone_meta:
        return list(candidates), []

    return list(candidates), []


# ── Predictive patience — per-tick wiring (PR A3) ─────────────────────────────
#
# Everything here exists to hand `r1.opportunity_check` the three things a rule
# in the R1 tier cannot fetch for itself: the learned prior store, the module
# config, and mission-duration statistics. See `r1.OpportunityContext`.

# Mission stats are cached because they are NOT tick-scoped data. They change
# only when a mission completes — a handful of times a day — while the loop
# ticks every 60-300s. Fetching per tick would issue seven HTTP calls a minute
# to re-read numbers that last moved hours ago. This is the opposite case from
# `get_zone_data()`, which deliberately declines a TTL because zone SCORES do
# change every tick; the distinction is the data's own update rate, not a
# difference of opinion about caching.
_MISSION_STATS_TTL_S = 900.0
_mission_stats_cache: dict[tuple[int, int | None], tuple[float, dict[str, Any] | None]] = {}


async def _fetch_mission_stats(
    homeops_adapter: Any,
    robot_id: int,
    zone_id: int | None,
    now_monotonic: float,
) -> dict[str, Any] | None:
    """TTL-cached `get_vacuum_mission_stats` read. Never raises.

    A None result is cached like any other, deliberately: a robot with no logged
    missions (Sam, per design memo §5.3) would otherwise be re-queried on every
    tick forever to be told the same thing.
    """
    key = (robot_id, zone_id)
    cached = _mission_stats_cache.get(key)
    if cached is not None and (now_monotonic - cached[0]) < _MISSION_STATS_TTL_S:
        return cached[1]
    try:
        stats = await homeops_adapter.get_mission_stats(robot_id, zone_id)
    except Exception as exc:  # noqa: BLE001 — adapter already degrades; belt and braces
        log.warning(
            "mission_stats_fetch_failed", robot_id=robot_id, zone_id=zone_id, error=str(exc)
        )
        stats = None
    _mission_stats_cache[key] = (now_monotonic, stats)
    return stats


def build_tick_config(
    base: VacuumOpsConfig, live_settings: VacuumOpsLiveSettings
) -> VacuumOpsConfig:
    """Fold this tick's live HomeOps kill switches onto the process-scoped config.

    A named function rather than an inline `dataclasses.replace(...)` in
    vacuumops_loop(), for exactly the reason build_vacuumops_config() is one: a
    kill switch that ships UNWIRED is the known failure mode in this module
    (ARIIA finding 1 on the original mop_enabled env var — the field existed,
    the source was documented, and nothing connected them). Tests that build a
    VacuumOpsConfig directly cannot catch that class of bug; they have to go
    through the mapping. `opportunity_actuate` is the second flag to make this
    journey and it must not repeat the first one's mistake.

    ⚠ EVERY LIVE FLAG BELONGS HERE. If you add one to VacuumOpsLiveSettings,
    map it in this function and assert it in
    test_opportunity_check.py::TestLiveActuateWiring — otherwise the DB row will
    exist, the operator will flip it, and nothing will happen.

    Copy, never mutate: `base` is shared across every tick for the life of the
    process, so a flip must produce a new object rather than edit the one the
    next tick will read.
    """
    return dataclasses.replace(
        base,
        mop_enabled=live_settings.mop_enabled,
        opportunity_actuate=live_settings.opportunity_actuate,
        # Provenance, not a switch: `read_ok` False means we never heard back, so
        # the False above is a fail-closed default rather than an observed value.
        # r1.opportunity_check needs the difference to name a settings outage in
        # the decision log instead of logging it as an ordinary shadow tick.
        opportunity_actuate_degraded=not live_settings.read_ok,
    )


async def build_opportunity_context(
    ctx: ContextSnapshot,
    jobs: list[VacuumJob],
    prior_source: Any,
    cfg: VacuumOpsConfig,
    homeops_adapter: Any,
    now_monotonic: float | None = None,
) -> OpportunityContext | None:
    """Assemble the per-tick `OpportunityContext`, or None if nothing needs one.

    Returns None when no active job has `opportunity_enabled` — which is the
    state for every job but `Saros1FRoomsJob`, and would be the state for ALL of
    them if Carlos ever switched the feature off. A None context makes
    `opportunity_check` return PASS with `opportunity_inert`, so switching the
    feature off costs one flag and zero I/O rather than leaving a rule half-live.

    Stats are fetched per ZONE (single-zone missions) and per ROBOT (batches),
    because `duration_estimate()` picks between them on `zone_count` — a batch is
    not the sum of its zones' means, since the zones share one transit and one
    dock trip. Only zones belonging to opportunity-enabled jobs are fetched.
    """
    enabled = [job for job in jobs if job.opportunity_enabled]
    if not enabled or prior_source is None:
        return None

    if now_monotonic is None:
        now_monotonic = time.monotonic()

    zone_stats: dict[int, dict[str, Any]] = {}
    robot_stats: dict[str, dict[str, Any]] = {}

    for job in enabled:
        # `robot_id` on the homeOps stats endpoint is the numeric vac_units id,
        # not the robot's name. ZoneMeta carries it, so it is resolved from the
        # job's own zones rather than from a second lookup table that could drift.
        unit_id: int | None = None
        for zone_id in job.zones:
            meta = ctx.zone_metadata.get(zone_id)
            if meta is not None:
                unit_id = meta.unit_id
                break
        if unit_id is None:
            # No metadata this tick (homeOps degraded). Not fatal: the missing
            # duration degrades the read to `unavailable`, which is a named
            # fail-open path, not a silent one.
            log.warning("opportunity_unit_id_unresolved", job_id=job.job_id)
            continue

        robot_payload = await _fetch_mission_stats(homeops_adapter, unit_id, None, now_monotonic)
        if robot_payload is not None:
            robot_stats[job.robot] = robot_payload

        for zone_id in job.zones:
            zone_payload = await _fetch_mission_stats(
                homeops_adapter, unit_id, zone_id, now_monotonic
            )
            if zone_payload is not None:
                zone_stats[zone_id] = zone_payload

    return OpportunityContext(
        prior_source=prior_source,
        cfg=cfg,
        prior_entity_id=cfg.prior_learner_entities[0],
        # [0] is the 1F rollup — THE BINARY THE OCCUPANCY GATE READS. The other
        # four tracked entities are diagnostic only (config.py). Indexing rather
        # than hardcoding the entity id keeps the learner and the reader pointed
        # at the same sensor by construction.
        zone_stats=zone_stats,
        robot_stats=robot_stats,
        slot_minutes=cfg.prior_learner_slot_minutes,
    )


# ── Batch assembly ─────────────────────────────────────────────────────────────


def _zone_effective_simple(job: VacuumJob, zone_id: int, ctx: ContextSnapshot) -> bool:
    """Quick effectiveness check for bundle sweep (D11)."""
    from cortex_python.modules.vacuumops.r1 import floor_clearance_check, zone_active_use_check

    # Pass zone_meta so the bundle sweep resolves occupancy through the same
    # precedence chain as the main gate — a bundled zone bypasses L1 entirely,
    # so it must not be held to a weaker effectiveness standard than a primary.
    result, _, _ = zone_active_use_check(job, zone_id, ctx, ctx.zone_metadata.get(zone_id))
    if result == "FAIL":
        return False
    result, _, _ = floor_clearance_check(job, zone_id, ctx)
    return result != "FAIL"


def _noise_acceptable_simple(job: VacuumJob, zone_id: int, ctx: ContextSnapshot) -> bool:
    """Quick noise check for bundle sweep (D11)."""
    from cortex_python.modules.vacuumops.noise import noise_budget, noise_impact
    from cortex_python.modules.vacuumops.r1 import noise_radius_check

    impact = noise_impact(job, ctx)
    budget = noise_budget(ctx, job.floor)
    if impact > budget:
        return False
    result, _, _ = noise_radius_check(job, zone_id, ctx)
    return result != "FAIL"


async def _per_zone_cooldown_clear(
    job: VacuumJob,
    zone_id: int,
    redis_client: aioredis.Redis,
) -> bool:
    """Check if per-zone cooldown is clear (for bundle sweep)."""
    key = _R0_ZONE_COOLDOWN_KEY.format(job_id=job.job_id, zone_id=zone_id)
    exists = await redis_client.exists(key)
    return not bool(exists)


def _job_for_zone(zone_id: int) -> VacuumJob:
    """Look up the job that owns a given zone_id.

    Raises ValueError if the zone_id is not found in any active job.
    This surfaces misconfiguration immediately rather than silently using
    wrong cooldown keys or cleaning params.
    """
    for job in ACTIVE_JOBS:
        if zone_id in job.zones:
            return job
    raise ValueError(f"zone_id={zone_id} not found in any active job")


def _job_for_zone_or_none(zone_id: int) -> VacuumJob | None:
    """Non-raising variant of _job_for_zone, for callers that degrade gracefully.

    The mop gate uses this: an unmapped zone should cost that zone its mop
    evaluation, not abort the whole dispatch that R0/R1 already approved.
    """
    for job in ACTIVE_JOBS:
        if zone_id in job.zones:
            return job
    return None


def _zone_display(zone_id: int, ctx: ContextSnapshot) -> str:
    """Return the display label for a zone_id, falling back to str(zone_id)."""
    info = ctx.zone_info.get(zone_id)
    return info.display if info else str(zone_id)


def assemble_batch(
    robot: str,
    zone_outcomes: list[ZoneOutcome],
    ctx: ContextSnapshot,
    jobs: list[VacuumJob],
    l1_results: dict[tuple[str, int], Any] | None = None,
) -> list[BatchEntry]:
    """Assemble a dispatch batch for one robot (D10 + D11).

    Step 1: collect zones that independently PASSED all gates.
    Step 2: if batch is non-empty, sweep remaining zones for bundle eligibility.
    Bundle sweep is synchronous (no Redis) — async cooldown check is skipped here
    because bundle candidates that failed R0 (including cooldown) already have
    action="defer" with gate_failed="r0".

    l1_results: dict keyed by (job_id, zone_id) → L1Decision.
    Used to resolve passes/intensity via resolve_params(). If None or key absent,
    falls back to job.cleaning_params defaults (source="default").

    Spec: §8.3 assemble_batch pseudocode
    """
    if l1_results is None:
        l1_results = {}

    primary = [zo for zo in zone_outcomes if zo.action == "dispatch"]
    if not primary:
        return []

    batch: list[BatchEntry] = []
    for zo in primary:
        try:
            job = _job_for_zone(zo.zone)
        except ValueError:
            log.warning("assemble_batch_unknown_zone", zone_id=zo.zone)
            continue
        l1 = l1_results.get((job.job_id, zo.zone))
        passes, intensity, src = resolve_params(job, l1)
        params_reason = l1.params_reason if (l1 and src != "default") else None
        batch.append(
            BatchEntry(
                zone=zo.zone,
                bundled=False,
                score=zo.score,
                l1_confidence=zo.l1_confidence,
                passes=passes,
                intensity=intensity,
                params_source=src,
                params_reason=params_reason,
            )
        )

    # D11 bundle sweep — pull in below-threshold zones that pass gates
    primary_zones = {e.zone for e in batch}
    for zo in zone_outcomes:
        if zo.zone in primary_zones:
            continue
        try:
            job = _job_for_zone(zo.zone)
        except ValueError:
            log.warning("assemble_batch_unknown_zone", zone_id=zo.zone)
            continue
        if job.robot != robot:
            continue

        bundle_floor = job.bundle_threshold_pct * job.dispatch_threshold
        score = ctx.zone_scores.get(zo.zone, 0.0)

        # Hard-failed zones that cannot be bundled:
        # - effectiveness failures (robot physically in the way)
        # - comfort failures (noise acceptable gate hard-failed)
        # - robot_cooldown (robot-level gate, blocks everything for this robot)
        # NOTE: "r0" gate_failed for score_below_threshold CAN still be bundled —
        # bundle inclusion is explicitly for zones below dispatch_threshold.
        # "r0" for robot/battery/cooldown issues (not score) must be excluded:
        # we check the reason string rather than the gate_failed field.
        if zo.gate_failed in ("effectiveness", "comfort", "robot_cooldown"):
            continue
        if zo.gate_failed == "r0" and not zo.reason.startswith("score_below_threshold"):
            # R0 failed for a non-score reason (battery, dock, cooldown) → not bundleable
            continue

        if (
            score >= bundle_floor
            and _zone_effective_simple(job, zo.zone, ctx)  # zo.zone is int
            and _noise_acceptable_simple(job, zo.zone, ctx)
        ):
            # Bundled zones skip L1 (D11) — use job defaults
            batch.append(
                BatchEntry(
                    zone=zo.zone,
                    bundled=True,
                    score=score,
                    l1_confidence=None,
                    passes=job.cleaning_params.get("passes", "auto"),
                    intensity=job.cleaning_params.get("intensity", "auto"),
                    params_source="default",
                    params_reason=None,
                )
            )

    return batch


# ── Dispatch ──────────────────────────────────────────────────────────────────


async def dispatch_batch(
    robot: str,
    batch: list[BatchEntry],
    ctx: ContextSnapshot,
    tick_id: str,
    settings: Settings,
    homeops_adapter: Any,
    vacuumops_cfg: VacuumOpsConfig,
    dry_run: bool,
    mop_decision: MopDecision | None = None,
) -> dict:
    """Issue a single POST /api/vacuum/trigger with zones[] for the full batch.

    Per §10.1: one call per robot per tick, regardless of batch size.
    Dry-run: skip the HomeOps call; still log.

    mop_decision comes from the mop-cadence gate (mop.py) and applies to the
    whole batch — mop intensity is a unit-level HA setting, so the mission runs
    wet or dry as a unit. None means "no mop" (the pre-gate behaviour).

    Returns the HomeOps response dict (or a synthetic dry-run echo).
    """
    mop = mop_decision.mop if mop_decision else False
    mop_intensity = mop_decision.intensity if mop_decision else None
    primary_zones = [e for e in batch if not e.bundled]
    tier_reached = "R1"  # default; override if any zone was L1-decided
    for entry in primary_zones:
        if entry.l1_confidence is not None:
            tier_reached = "L1"
            break

    bundled_count = sum(1 for e in batch if e.bundled)
    reason = f"all_rules_pass+{bundled_count}_bundled" if bundled_count > 0 else "all_rules_pass"

    trigger_metadata = {
        "tick_id": tick_id,
        "decision_tier": tier_reached,
        "reason": reason,
        "mop": mop,
        "mop_intensity": mop_intensity,
        "mop_reason": mop_decision.reason if mop_decision else None,
    }

    zones_payload = [
        {
            # HomeOps /api/vacuum/trigger still expects zone label strings — resolve at dispatch
            "label": (
                ctx.zone_info[entry.zone].label if entry.zone in ctx.zone_info else str(entry.zone)
            ),
            "passes": entry.passes,
            "intensity": entry.intensity,
            "bundled": entry.bundled,
            "score": entry.score,
            "l1_confidence": entry.l1_confidence,
        }
        for entry in batch
    ]

    if dry_run:
        log.info(
            "dispatch_batch_dry_run",
            robot=robot,
            zones=[_zone_display(e.zone, ctx) for e in batch],
            tick_id=tick_id,
            mop=mop,
            mop_intensity=mop_intensity,
        )
        return {
            "ha_dispatched": False,
            "dry_run_echo": True,
            "zones": zones_payload,
            "mop": mop,
            "mop_intensity": mop_intensity,
        }

    result = await homeops_adapter.trigger_vacuum(
        robot=robot,
        zones=zones_payload,
        trigger_metadata=trigger_metadata,
        dry_run=False,
        mop=mop,
        mop_intensity=mop_intensity,
    )
    log.info(
        "dispatch_batch_sent",
        robot=robot,
        zones=[_zone_display(e.zone, ctx) for e in batch],
        tick_id=tick_id,
        mop=mop,
        mop_intensity=mop_intensity,
        result=result,
    )
    return result


# ── Cooldown management ───────────────────────────────────────────────────────


async def _set_per_zone_cooldowns(
    batch: list[BatchEntry], jobs: list[VacuumJob], redis_client: aioredis.Redis
) -> None:
    """Set per-zone cooldown keys for every zone in the dispatched batch.

    Key: cortex:vacuumops:cooldown:<job_id>:<zone_id>
    TTL: job.cooldown_minutes * 60 seconds
    """
    for entry in batch:
        job = _job_for_zone(entry.zone)
        key = _R0_ZONE_COOLDOWN_KEY.format(job_id=job.job_id, zone_id=entry.zone)
        ttl = job.cooldown_minutes * 60
        await redis_client.set(key, 1, ex=ttl)
        log.debug("zone_cooldown_set", zone_id=entry.zone, ttl_s=ttl)


async def _clear_opportunity_zone_state(
    batch: list[BatchEntry], redis_client: aioredis.Redis, *, actuating: bool
) -> None:
    """Clear the predictive-patience Redis state for every dispatched zone (§4.4).

    TWO KEYS, CLEARED FOR DIFFERENT REASONS:

    `over_threshold_since` — the starvation clock. A dispatch is exactly the
    event that ends a zone's wait, so leaving it set would make the zone read as
    having been over threshold since before a mission it already got, and the
    6-hour `patience_hard_cap_h` would fire spuriously on the next cycle.
    Cleared unconditionally, for every dispatched zone, whatever the job.

    `opportunity_defer_streak` — the chained-deferral instrument, and this one is
    cleared ONLY on ticks where actuation is live. §4.4 says "cleared on
    dispatch", which is correct while actuating: there, a dispatch can only
    happen on a tick the rule did not defer, so clearing on dispatch and clearing
    on a non-`better_window` verdict are the same event.

    ⚠ IN SHADOW THEY ARE NOT THE SAME EVENT, AND CLEARING HERE WOULD DESTROY THE
    MEASUREMENT. In LOG-ONLY mode every tick passes and dispatches regardless of
    the verdict, so an unconditional clear-on-dispatch would reset the counter
    every single tick and pin it at 1 forever — and the A4 go/no-go's "max defer
    streak >= 6 = do not flip" red light could never illuminate. The counter's
    entire purpose is to answer "would this zone have got stuck always-one-hour-
    away?", which is a counterfactual about a dispatch that, in shadow, did not
    happen. `r1._reset_defer_streak` maintains it from the verdict sequence
    instead. This branch is what restores the literal §4.4 semantics the moment
    actuation goes live, with no second code change.

    `actuating` IS REQUIRED AND KEYWORD-ONLY, WITH NO DEFAULT. It used to be read
    per-zone off `job.opportunity_actuate`; that field is gone, because the
    switch is now a live HomeOps setting resolved once per tick and handed down
    (`tick_vacuumops_cfg.opportunity_actuate`). Since a wrong value here silently
    corrupts soak evidence rather than raising, the caller is made to state it
    rather than inherit a default that would be right only by luck.
    """
    for entry in batch:
        keys = [over_threshold_since_key(entry.zone)]
        if actuating:
            keys.append(_OPPORTUNITY_DEFER_STREAK_KEY.format(zone_id=entry.zone))
        try:
            await redis_client.delete(*keys)
        except Exception as exc:  # noqa: BLE001 — instrumentation must not fail a dispatch
            log.warning("opportunity_clear_failed", zone_id=entry.zone, error=str(exc))


async def _set_robot_cooldown(
    robot: str, cfg: VacuumOpsConfig, redis_client: aioredis.Redis
) -> None:
    """Set per-robot cooldown key (D12).

    Key: cortex:vacuumops:robot_cooldown:<robot>
    TTL: robot_cooldown_minutes (or override) * 60 seconds
    Reset on every dispatch — not extended, always re-set fresh.
    """
    ttl_minutes = cfg.robot_cooldown_overrides.get(robot, cfg.robot_cooldown_minutes)
    key = _R1_ROBOT_COOLDOWN_KEY.format(robot=robot)
    await redis_client.set(key, 1, ex=ttl_minutes * 60)
    log.debug("robot_cooldown_set", robot=robot, ttl_min=ttl_minutes)


async def _robot_cooldown_active(robot: str, redis_client: aioredis.Redis) -> bool:
    """Check if per-robot cooldown is active."""
    key = _R1_ROBOT_COOLDOWN_KEY.format(robot=robot)
    return bool(await redis_client.exists(key))


# ── Persist decision ──────────────────────────────────────────────────────────


async def persist_decision(
    tick_id: str,
    robot: str,
    zone_outcomes: list[ZoneOutcome],
    batch: list[BatchEntry],
    ctx: ContextSnapshot,
    homeops_adapter: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
    dry_run: bool,
    l1_results: dict[tuple[str, int], Any] | None = None,
    mop_decision: MopDecision | None = None,
    opp_ctx: OpportunityContext | None = None,
) -> None:
    """Write the decision to BOTH:
    1. CORTEX-internal decision_log table (SQLAlchemy async session)
    2. HomeOps vac_decisions via POST /api/decisions/vacuumops

    If HomeOps call fails, log the error but do NOT abort the loop tick.

    mop_decision is recorded even when it resolves to False, so the log answers
    "why was this run dry?" as readily as "why was it wet?".

    Spec: §8.3, §7.4
    """
    import pytz

    pst = pytz.timezone("America/Los_Angeles")
    ts_pst = ctx.timestamp.astimezone(pst).isoformat()

    if l1_results is None:
        l1_results = {}

    # Map zone_id -> BatchEntry for quick lookup
    batch_by_zone: dict[int, BatchEntry] = {e.zone: e for e in batch}

    # Build DecisionEntry
    if batch:
        decision_str = "dispatch"
        gate_failed = None
        dispatched_at = ts_pst

        primary_zones = [e for e in batch if not e.bundled]
        tier_reached = "R1"
        l1_conf_values = []
        for entry in primary_zones:
            if entry.l1_confidence is not None:
                tier_reached = "L1"
                l1_conf_values.append(entry.l1_confidence)
        l1_confidence = min(l1_conf_values) if l1_conf_values else None

        bundled_count = sum(1 for e in batch if e.bundled)
        reason = f"all_rules_pass+{bundled_count}_bundled" if bundled_count else "all_rules_pass"
    else:
        # No dispatch — find the dominant deferral reason
        decision_str = "skip"
        dispatched_at = None
        l1_confidence = None

        dominant = zone_outcomes[0] if zone_outcomes else None
        if dominant:
            gate_failed = dominant.gate_failed
            tier_reached = dominant.tier
            reason = dominant.reason
        else:
            gate_failed = "r0"
            tier_reached = "R0"
            reason = "no_zones_evaluated"

    # Unified per-zone detail — log ALL evaluated zones regardless of outcome
    zone_details: list[ZoneDecisionDetail] = []
    for zo in zone_outcomes:
        be = batch_by_zone.get(zo.zone)
        info = ctx.zone_info.get(zo.zone)
        try:
            job = _job_for_zone(zo.zone)
        except ValueError:
            log.warning("persist_decision_unknown_zone", zone_id=zo.zone, tick_id=tick_id)
            continue
        l1 = l1_results.get((job.job_id, zo.zone))

        if be is not None:
            result = "bundled" if be.bundled else "dispatch"
            l1_conf = be.l1_confidence
        else:
            result = "defer"
            l1_conf = zo.l1_confidence

        zone_details.append(
            ZoneDecisionDetail(
                label=info.label if info else str(zo.zone),
                display=info.display if info else str(zo.zone),
                score=zo.score,
                bundled=(be.bundled if be else False),
                result=result,
                gate_failed=zo.gate_failed if zo.action == "defer" else None,
                gate_reason=zo.reason if zo.action == "defer" else None,
                l1_confidence=l1_conf,
                l1_decision=l1.decision if l1 else None,
                l1_reason=l1.reason if l1 else None,
                l1_defer_until_hint=l1.defer_until_hint if l1 else None,
                l1_passes=str(l1.passes) if (l1 and l1.passes is not None) else None,
                l1_intensity=str(l1.intensity) if (l1 and l1.intensity is not None) else None,
                l1_params_reason=l1.params_reason if l1 else None,
                # Structured predictive-patience read (PR A3). None for every job
                # but Saros 1F Rooms, and for any tick the rule declined to form
                # a read — the reason string in `gate_reason` still names why.
                opportunity=opp_ctx.reads.get(zo.zone) if opp_ctx else None,
            )
        )

    decision_entry = DecisionEntry(
        tick_id=tick_id,
        timestamp=ts_pst,
        robot=robot,
        zones=zone_details,
        tier_reached=tier_reached,
        gate_failed=gate_failed,
        decision=decision_str,
        reason=reason,
        l1_confidence=l1_confidence,
        dry_run=dry_run,
        dispatched_at=dispatched_at,
        mop=mop_decision.mop if mop_decision else False,
        mop_intensity=mop_decision.intensity if mop_decision else None,
        mop_reason=mop_decision.reason if mop_decision else None,
    )

    # 1. Write to CORTEX decision_log
    import json

    import sqlalchemy as sa

    try:
        async with db_session_factory() as session:
            row = {
                "id": str(uuid.uuid4()),
                "ts": ctx.timestamp,
                "tier": tier_reached,
                "model": "gemma4:31b" if tier_reached == "L1" else None,
                "module": "vacuumops",
                "trigger_id": tick_id,
                "context_snapshot": {
                    "robot": robot,
                    "zone_scores": {str(k): v for k, v in ctx.zone_scores.items()},
                    "people": {
                        k: {"activity": v.activity, "confidence": v.confidence}
                        for k, v in ctx.people.items()
                    },
                },
                "decision_payload": {
                    "decision": decision_str,
                    "reason": reason,
                    "zones": [
                        {"label": z.label, "score": z.score, "bundled": z.bundled}
                        for z in zone_details
                    ],
                    # Occupancy-gate bypass observability (spec §6.8):
                    # reason string already contains the bypass tag (e.g.
                    # "all_rules_pass|occ_bypass:home_empty") — surfaced here
                    # in structured form for easier querying during dry-run analysis.
                    "occupancy_gate_bypassed": any(
                        "|occ_bypass:" in zo.reason or "|occ_relax:" in zo.reason
                        for zo in zone_outcomes
                    ),
                },
                "outcome": "executed" if decision_str == "dispatch" else "suppressed",
                "latency_ms": None,
                "confidence": l1_confidence,
                "superseded_by": None,
                "notes": None,
            }
            await session.execute(
                sa.text(
                    "INSERT INTO decision_log "
                    "(id, ts, tier, model, module, trigger_id, context_snapshot, "
                    "decision_payload, outcome, latency_ms, confidence, superseded_by, notes) "
                    "VALUES (:id, :ts, :tier, :model, :module, :trigger_id, :context_snapshot, "
                    ":decision_payload, :outcome, :latency_ms, :confidence, :superseded_by, :notes)"
                ),
                {
                    **row,
                    "context_snapshot": json.dumps(row["context_snapshot"]),
                    "decision_payload": json.dumps(row["decision_payload"]),
                },
            )
            await session.commit()
    except Exception as exc:
        log.error("decision_log_write_failed", tick_id=tick_id, error=str(exc))

    # 2. Write to HomeOps vac_decisions — fire-and-forget; log error but don't raise
    try:
        await homeops_adapter.log_decision(decision_entry)
    except Exception as exc:
        log.error("homeops_decision_log_failed", tick_id=tick_id, error=str(exc))


# ── Adaptive interval ─────────────────────────────────────────────────────────


def next_interval(
    ctx: ContextSnapshot | None, robot_states_active: bool, l1_timeout_count: int
) -> int:
    """Compute the next sleep interval in seconds.

    Spec: §8.2 cadence table
    """
    if ctx is None:
        return 60  # default before first snapshot

    # Robot in active mission
    for rs in ctx.robot_states.values():
        if rs.state in ("cleaning", "returning"):
            return 300

    # Robot cooldown (checked by caller after setting cooldown keys)
    if robot_states_active:
        return 300

    # L1 timeout in last 2 ticks
    if l1_timeout_count >= 2:
        return 180

    # All people away + no events
    all_away = all(p.activity == "away" for p in ctx.people.values())
    no_events = len(ctx.upcoming_events) == 0
    if all_away and no_events:
        return 120

    return 60


# ── Occupancy prior learner (spec §4.2 / PR A1) ───────────────────────────────


async def _maybe_run_prior_backfill(
    store: PriorStore,
    ha_adapter: Any,
    redis_client: aioredis.Redis,
    cfg: VacuumOpsConfig,
    now: datetime,
) -> None:
    """Run the one-time HA-history seed, once per deployment. Never raises.

    Guarded by a Redis flag purely to avoid re-reading weeks of history on every
    process restart — the backfill is idempotent regardless (observations dedupe
    on their slot-start instant, and a backfilled sample can never overwrite a
    native one), so a lost flag costs HA reads, not correctness.

    A failure here is logged loudly and swallowed: the backfill removes the
    learner's cold start, it is not a dependency of it. §4.2 — "the PR still
    merges and the learner starts cold".
    """
    try:
        already = await redis_client.get(BACKFILL_DONE_KEY)
    except Exception as exc:
        log.warning("prior_backfill_flag_read_failed", error=str(exc))
        already = None
    if already:
        return

    try:
        report = await backfill_priors(
            store,
            ha_adapter,
            entities=list(cfg.prior_learner_entities),
            now=now,
            lookback_days=cfg.prior_learner_backfill_days,
            chunk_days=cfg.prior_learner_backfill_chunk_days,
            slot_minutes=cfg.prior_learner_slot_minutes,
        )
    except Exception as exc:
        log.error("prior_backfill_failed", error=str(exc))
        return

    if not report.ok:
        # Do NOT set the done-flag: nothing was seeded, so a retry on the next
        # restart is worth the reads. An HA that was merely unreachable at boot
        # would otherwise leave the table permanently cold.
        log.warning(
            "prior_backfill_seeded_nothing",
            entities_failed=report.entities_failed,
            slots_no_data=report.slots_no_data,
        )
        return

    with contextlib.suppress(Exception):
        await redis_client.set(BACKFILL_DONE_KEY, now.isoformat())


# ── Main loop ─────────────────────────────────────────────────────────────────


async def vacuumops_loop(settings: Settings) -> None:
    """Main VacuumOps adaptive polling loop. Never exits.

    Constructs its own Redis client, DB session factory, and LiteLLM client
    so the lifespan hook in api/main.py stays simple.

    All exceptions inside a tick are caught; the loop never dies.

    Spec: §8.3
    """
    log.info("vacuumops_loop.initializing")

    # Build clients
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    litellm_client = build_litellm_client(settings)

    engine = create_async_engine(settings.database_url, echo=False)
    db_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False
    )

    # Import adapters lazily to avoid circular import at module load time
    from cortex_python.adapters.ha_rest_adapter import HARestAdapter
    from cortex_python.adapters.homeops_adapter import HomeOpsAdapter
    from cortex_python.synth.vacuumops_synth import build_snapshot

    ha_adapter = HARestAdapter(settings)
    homeops_adapter = HomeOpsAdapter(settings)

    # Module config — dry-run sourced from settings
    vacuumops_cfg = build_vacuumops_config(settings)

    # Occupancy prior learner (spec §4.2 / PR A1). Writes cortex_occupancy_priors.
    # As of A3 this store IS read on the live path: `build_opportunity_context`
    # hands it to `run_r1` as `opp_ctx.prior_source`, where `opportunity_check`
    # consults it. Whether that rule can act is now a per-tick question, not a
    # deploy-time one: `opportunity_actuate` is a live HomeOps setting, ships
    # false, and fails closed. It shipped first because its sample clock is
    # wall-clock bound and everything else in the train is engineering time.
    prior_store = PriorStore(
        db_session_factory,
        slot_minutes=vacuumops_cfg.prior_learner_slot_minutes,
        retention=vacuumops_cfg.prior_learner_retention,
        min_slot_samples=vacuumops_cfg.opportunity_min_slot_samples,
    )
    prior_learner = PriorLearner(
        prior_store,
        ha_adapter,
        redis_client,
        entities=list(vacuumops_cfg.prior_learner_entities),
        slot_minutes=vacuumops_cfg.prior_learner_slot_minutes,
        max_catchup_slots=vacuumops_cfg.prior_learner_max_catchup_slots,
        max_lookback_days=vacuumops_cfg.prior_learner_backfill_days,
    )
    if vacuumops_cfg.prior_learner_enabled:
        await _maybe_run_prior_backfill(
            prior_store, ha_adapter, redis_client, vacuumops_cfg, datetime.now(tz=UTC)
        )

    log.info(
        "vacuumops_loop.started",
        dry_run=vacuumops_cfg.dry_run,
        prior_learner_enabled=vacuumops_cfg.prior_learner_enabled,
        # Neither kill switch is static config any more — both are read fresh
        # from HomeOps every tick (see live_settings below), so there is nothing
        # meaningful to log at startup beyond the source. Logged as two lines
        # rather than one shared one so that grepping either flag's provenance
        # finds it by name.
        mop_enabled_source="homeops_db(live, per-tick)",
        opportunity_actuate_source="homeops_db(live, per-tick)",
    )

    ctx: ContextSnapshot | None = None
    l1_timeout_count = 0  # consecutive ticks with L1 timeout
    last_dispatch_at: str | None = None
    consecutive_skip_reason: str | None = None
    dispatched_this_tick: bool = False

    while True:
        tick_id = str(uuid.uuid4())
        tick_start = datetime.now(tz=UTC)
        dispatched_this_tick = False

        try:
            # Load patterns (hot-reload on mtime change)
            patterns = _load_patterns()

            # Occupancy prior learner slot close-out (spec §4.2 / PR A1).
            #
            # Runs BEFORE the snapshot build, deliberately: it shares no state
            # with the snapshot, and a snapshot failure `continue`s the tick —
            # placing the learner after it would let a degraded HomeOps stall a
            # calendar-bound sample clock that has nothing to do with HomeOps.
            #
            # A tick crosses a 30-minute boundary at most once (the longest
            # cadence is 300 s), so this is a no-op on ~29 of every 30 minutes of
            # ticks and does no I/O at all on those. Wrapped despite
            # close_out_due_slots() being internally defensive — nothing in an
            # observability feature may ever kill a dispatch tick.
            if vacuumops_cfg.prior_learner_enabled:
                try:
                    await prior_learner.close_out_due_slots(tick_start)
                except Exception as exc:
                    log.error("prior_learner_tick_failed", tick_id=tick_id, error=str(exc))

            # Build snapshot
            # build_snapshot returns (ctx, unit_dry_runs, live_settings):
            #   unit_dry_runs   dict[robot_name → dry_run bool], from HomeOps
            #                   vac_units.dry_run column.
            #   live_settings   every live kill switch from HomeOps
            #                   cortex_vacuumops_settings, read in ONE call:
            #                   mop_enabled (mop-cadence gate) and
            #                   opportunity_actuate (predictive patience), plus
            #                   read_ok. All fail-closed to False on any read
            #                   problem — see HomeOpsAdapter
            #                   .get_vacuumops_settings().
            try:
                ctx, unit_dry_runs, live_settings = await build_snapshot(
                    tick_id, ha_adapter, homeops_adapter, settings
                )
            except Exception as exc:
                log.error("snapshot_build_failed", tick_id=tick_id, error=str(exc))
                # Skip tick — zone score is required (§8.5)
                await ha_adapter.publish_loop_status(
                    "error",
                    {
                        "last_tick_at": tick_start.isoformat(),
                        "last_decision": "snapshot_failed",
                        "last_dispatch_at": last_dispatch_at,
                        "consecutive_skip_reason": str(exc),
                    },
                )
                await asyncio.sleep(60)
                continue

            # Tick-scoped VacuumOpsConfig. Everything else on it is fixed for the
            # life of the process (vacuumops_cfg, built once above); the two live
            # kill switches must reflect what HomeOps said THIS tick, so they are
            # threaded in per-tick via dataclasses.replace rather than by mutating
            # the shared vacuumops_cfg object. Copy-per-tick is what makes a flip
            # take effect on the very next tick with no cache to invalidate.
            #
            #   mop_enabled          → consumed by resolve_batch_mop (below).
            #   opportunity_actuate  → consumed by r1.opportunity_check, reached
            #                          via opp_ctx.cfg, and by
            #                          _clear_opportunity_zone_state on dispatch.
            #
            # opportunity_actuate_degraded is NOT a switch; it carries whether we
            # actually heard back, so the rule can name a settings outage in the
            # decision log instead of silently logging it as an ordinary shadow
            # tick. Both flags fail closed, which makes the two states otherwise
            # indistinguishable. See VacuumOpsLiveSettings.
            #
            # ⚠ ONCE THIS EXISTS, NOTHING ELSE IN THE TICK MAY USE
            # `vacuumops_cfg`. The tick-scoped copy is a superset — every static
            # field is carried through unchanged by build_tick_config — so there
            # is no consumer that needs the process-scoped object, and passing
            # it anywhere below would silently pin that consumer to the
            # dataclass defaults for the live flags. That failure is invisible:
            # the DB column exists, the adapter reads it, the rule checks the
            # field, and the operator's flip does nothing. It is ARIIA finding 1
            # ("shipped unwired") one layer up from where it was found.
            #
            # Rather than rely on reviewers spotting the wrong identifier, the
            # rule is mechanical and test-pinned:
            # test_opportunity_check.py::TestTickScopedConfigIsTheOnlyOne parses
            # this function and fails if any call below passes the
            # process-scoped config.
            tick_vacuumops_cfg = build_tick_config(vacuumops_cfg, live_settings)

            # Predictive-patience context (PR A3). Built fresh each tick because
            # its `reads` sink is tick-scoped: it carries this tick's
            # OpportunityRead per zone from r1 to the L1 prompt and the decision
            # log, and reusing it across ticks would let a stale read be
            # attributed to a later decision. Never fatal — on any failure the
            # context is None and every opportunity_check returns PASS with
            # `opportunity_inert`, which is the pre-A3 behaviour, named.
            opp_ctx: OpportunityContext | None = None
            try:
                opp_ctx = await build_opportunity_context(
                    ctx=ctx,
                    jobs=ACTIVE_JOBS,
                    prior_source=prior_store,
                    # TICK-SCOPED, not the process-scoped vacuumops_cfg. This is
                    # the whole delivery path for the live opportunity_actuate
                    # flag: opportunity_check reads it off opp_ctx.cfg, so
                    # passing the process-scoped copy here would pin the rule to
                    # the dataclass default and no DB flip would ever reach it.
                    cfg=tick_vacuumops_cfg,
                    homeops_adapter=homeops_adapter,
                )
            except Exception as exc:
                log.warning("opportunity_context_build_failed", tick_id=tick_id, error=str(exc))

            # Group jobs by robot
            per_robot: dict[str, list[ZoneOutcome]] = defaultdict(list)
            per_robot_l1: dict[str, dict[tuple[str, int], L1Decision]] = defaultdict(dict)
            any_robot_cooldown = False
            tick_has_l1_timeout = False

            for job in ACTIVE_JOBS:
                # D12: per-robot cooldown short-circuit
                if await _robot_cooldown_active(job.robot, redis_client):
                    any_robot_cooldown = True
                    for zone_id in job.zones:
                        per_robot[job.robot].append(
                            ZoneOutcome(
                                zone=zone_id,
                                action="defer",
                                tier="R1",
                                gate_failed="robot_cooldown",
                                reason=f"robot_cooldown_active:{job.robot}",
                                score=ctx.zone_scores.get(zone_id, 0.0),
                            )
                        )
                    continue

                # Per-zone evaluations — parallel L1 calls where applicable (D13)
                # l1_results collects L1Decision objects keyed by (job_id, zone_id)
                # so that assemble_batch can resolve cleaning params from L1 output.
                l1_results: dict[tuple[str, int], L1Decision] = {}
                zone_coros = [
                    evaluate_zone(
                        job=job,
                        zone=zone,
                        ctx=ctx,
                        redis_client=redis_client,
                        settings=settings,
                        litellm_client=litellm_client,
                        vacuumops_cfg=tick_vacuumops_cfg,
                        patterns=patterns,
                        l1_results=l1_results,
                        opp_ctx=opp_ctx,
                    )
                    for zone in job.zones
                ]
                zone_results: list[ZoneOutcome] = list(
                    await asyncio.gather(*zone_coros, return_exceptions=False)
                )

                # Containment dedup — drop child zones whose parent is also a candidate.
                # Dropped zones are logged to decision_log with event "containment_dedup".
                # Phase 1: single zone, no containment relationships — this is a no-op
                # but infrastructure is wired for Phase 2.
                deduped_results, dropped_records = dedup_contained(zone_results, ctx.zone_metadata)
                for drop in dropped_records:
                    log.info(
                        "containment_dedup",
                        zone_id=drop.zone_id,
                        job_id=drop.job_id,
                        reason=drop.reason,
                        parent_zone_id=drop.parent_zone_id,
                    )

                # Track L1 timeouts
                for zo in deduped_results:
                    if zo.reason in ("l1_timeout", "l1_exception:l1_timeout"):
                        tick_has_l1_timeout = True

                per_robot[job.robot].extend(deduped_results)
                per_robot_l1[job.robot].update(l1_results)

            # Update L1 timeout counter (§8.2 backoff)
            if tick_has_l1_timeout:
                l1_timeout_count = min(l1_timeout_count + 1, 2)
            else:
                l1_timeout_count = max(l1_timeout_count - 1, 0)

            # Per-robot batch assembly + dispatch
            for robot, zone_outcomes in per_robot.items():
                batch = assemble_batch(
                    robot,
                    zone_outcomes,
                    ctx,
                    ACTIVE_JOBS,
                    l1_results=per_robot_l1.get(robot, {}),
                )

                # Effective dry_run for this robot: per-unit flag from HomeOps
                # vac_units.dry_run column is the sole control.
                # unit_dry_runs defaults to True (safe) when a robot is not in the map
                # (e.g. a new unit added before the column migration runs).
                effective_dry_run = unit_dry_runs.get(robot, True)

                # Mop-cadence gate (mop.py). Resolved AFTER batch assembly because
                # the mop is a modifier on the dispatch we have already decided to
                # make, and because mop intensity is a unit-level HA setting — the
                # whole batch runs wet or dry together. Deterministic, no L1.
                mop_job_map: dict[int, VacuumJob] = {}
                for batch_entry in batch:
                    owning_job = _job_for_zone_or_none(batch_entry.zone)
                    if owning_job is not None:
                        mop_job_map[batch_entry.zone] = owning_job

                mop_decision = resolve_batch_mop(
                    batch=batch,
                    ctx=ctx,
                    job_for_zone=mop_job_map,
                    cfg=tick_vacuumops_cfg,
                )

                await persist_decision(
                    tick_id=tick_id,
                    robot=robot,
                    zone_outcomes=zone_outcomes,
                    batch=batch,
                    ctx=ctx,
                    homeops_adapter=homeops_adapter,
                    db_session_factory=db_session_factory,
                    dry_run=effective_dry_run,
                    l1_results=per_robot_l1.get(robot, {}),
                    mop_decision=mop_decision,
                    opp_ctx=opp_ctx,
                )

                if batch:
                    try:
                        await dispatch_batch(
                            robot=robot,
                            batch=batch,
                            ctx=ctx,
                            tick_id=tick_id,
                            settings=settings,
                            homeops_adapter=homeops_adapter,
                            vacuumops_cfg=tick_vacuumops_cfg,
                            dry_run=effective_dry_run,
                            mop_decision=mop_decision,
                        )
                        await _set_per_zone_cooldowns(batch, ACTIVE_JOBS, redis_client)
                        await _set_robot_cooldown(robot, tick_vacuumops_cfg, redis_client)
                        # §4.4: the starvation clock and (when actuating) the
                        # deferral streak end when the zone actually gets its
                        # mission. `actuating` comes from THIS tick's live
                        # HomeOps read, so a flip changes the clearing semantics
                        # on the next tick without a deploy.
                        await _clear_opportunity_zone_state(
                            batch,
                            redis_client,
                            actuating=tick_vacuumops_cfg.opportunity_actuate,
                        )
                        import pytz

                        pst = pytz.timezone("America/Los_Angeles")
                        last_dispatch_at = tick_start.astimezone(pst).isoformat()
                        dispatched_this_tick = True
                        consecutive_skip_reason = None
                    except Exception as exc:
                        log.error("dispatch_failed", robot=robot, tick_id=tick_id, error=str(exc))
                else:
                    # Track skip reason for loop status sensor
                    skip_reasons = [zo.reason for zo in zone_outcomes if zo.action == "defer"]
                    if skip_reasons:
                        consecutive_skip_reason = skip_reasons[0]

            # Publish loop status to HA.
            # loop_state reflects the effective dry_run posture across all active robots
            # derived solely from per-unit DB flags (vac_units.dry_run column):
            #   "dry_run" — all units are dry_run
            #   "partial"  — some units live, some dry_run
            #   "healthy"  — all units are live
            all_robots = [job.robot for job in ACTIVE_JOBS]
            if not all_robots:
                loop_state = "idle"
            elif all(unit_dry_runs.get(r, True) for r in all_robots):
                loop_state = "dry_run"
            elif any(unit_dry_runs.get(r, True) for r in all_robots):
                loop_state = "partial"
            else:
                loop_state = "healthy"
            last_decision = consecutive_skip_reason or (
                "dispatch" if dispatched_this_tick else "defer"
            )
            await ha_adapter.publish_loop_status(
                loop_state,
                {
                    "last_tick_at": tick_start.isoformat(),
                    "last_decision": last_decision,
                    "last_dispatch_at": last_dispatch_at,
                    "consecutive_skip_reason": consecutive_skip_reason,
                },
            )

        except Exception as exc:
            log.exception("loop_tick_failed", tick_id=tick_id)
            with contextlib.suppress(Exception):  # never let status publish kill the loop
                await ha_adapter.publish_loop_status(
                    "error",
                    {
                        "last_tick_at": tick_start.isoformat(),
                        "last_decision": "error",
                        "last_dispatch_at": last_dispatch_at,
                        "consecutive_skip_reason": str(exc),
                    },
                )

        # Adaptive sleep
        interval = next_interval(ctx, any_robot_cooldown, l1_timeout_count)
        await asyncio.sleep(interval)


# Shim for circular import avoidance
# r1.py imports _parse_pattern_time from loop.py
__all__ = ["vacuumops_loop", "render_patterns_for", "_parse_pattern_time", "ACTIVE_JOBS"]
