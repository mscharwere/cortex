"""VacuumOps L1 — LLM disambiguation tier.

L1 only runs when R0 and R1 both produce a non-FAIL result AND at least one R1
rule flagged AMBIGUOUS/marginal. Default l1_required=False for Litter Box
per §5 — the rules carry most decisions; L1 is the safety net for edge cases.

Per D13: L1 is invoked PER-ZONE independently. The LLM never reasons about
batching — it answers exactly one question: "is this specific zone acceptable
right now?"

Path: Python → HTTPX → LiteLLM (http://ollama.perwnet.com:4000) → gemma4:31b.
JSON-schema response enforced via Pydantic. Timeout 30s (tighter than the 90s
system default — vacuum dispatch should be fast or skip).

Spec: C:/Jarvis/Team/TARS/cortex_vacuumops_module_spec.md §7.3
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC
from typing import Any, Literal

import httpx
import redis.asyncio as aioredis
import structlog
from pydantic import BaseModel, Field

from cortex_python.config.settings import Settings
from cortex_python.modules.vacuumops.jobs import VacuumJob
from cortex_python.modules.vacuumops.noise import noise_budget, noise_impact
from cortex_python.modules.vacuumops.schemas import ContextSnapshot, ZoneMeta

log = structlog.get_logger()

# L1 model — gemma4:31b via LiteLLM proxy (MS-S1 MAX)
_L1_MODEL = "gemma4:31b"

# L1 HTTPX timeout — 30s read (vacuum dispatch should be fast or skip)
# Tighter than the 90s litellm_client default — intentional per spec.
_L1_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=5.0)

# Redis key template for L1 cache
# cortex:vacuumops:l1:litter_box:<context_hash>
_L1_CACHE_KEY = "cortex:vacuumops:l1:litter_box:{context_hash}"
_L1_CACHE_TTL = 600  # 10 minutes

# PST timezone offset (UTC-7 during DST, UTC-8 otherwise — Carlos uses "PST" year-round)
_PST = UTC  # timestamps rendered as UTC; UI renders PST; loop renders PST in prompts


class L1Decision(BaseModel):
    """Pydantic schema for L1 LLM response. Spec §7.3."""

    decision: Literal["dispatch", "defer"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    defer_until_hint: str | None = None
    # Cleaning parameters (dispatch params spec)
    passes: Literal["auto", "single", "double"] | None = None
    intensity: Literal["auto", "normal", "high"] | None = None
    params_reason: str | None = None  # ≤120 chars


def resolve_zone_meta(zone_label: str, ctx: "ContextSnapshot") -> "ZoneMeta":
    """Resolve ZoneMeta for a zone. Phase 1: single-zone, returns first entry.
    Phase 2: match by label field once ZoneMeta.label is added."""
    if not ctx.zone_metadata:
        return ZoneMeta(zone_id=0, unit_id=0)
    # Phase 1: single active zone — first (only) entry is correct
    # Phase 2 TODO: match by zone_label when ZoneMeta gains a label field
    return next(iter(ctx.zone_metadata.values()))


def resolve_params(job: VacuumJob, l1: "L1Decision | None") -> tuple[str, str, str]:
    """Resolve passes + intensity from L1 result, falling back to job defaults.

    Returns (passes, intensity, source) where source is "l1" | "mixed" | "default".
    """
    default_passes = job.cleaning_params.get("passes", "auto")
    default_intensity = job.cleaning_params.get("intensity", "auto")
    if l1 is None:
        return default_passes, default_intensity, "default"
    p = l1.passes or default_passes
    i = l1.intensity or default_intensity
    src = (
        "l1"
        if (l1.passes and l1.intensity)
        else "mixed"
        if (l1.passes or l1.intensity)
        else "default"
    )
    return p, i, src


def _build_context_hash(
    job: VacuumJob,
    zone: str,
    ctx: ContextSnapshot,
    bypassed_for_zone: bool = False,
    reason_for_zone: str | None = None,
) -> str:
    """Build a deterministic cache key from the context subset the prompt uses.

    Only includes fields the prompt actually references (people activities,
    room activities to one-decimal confidence, score rounded to int,
    upcoming-events titles+start-minute). Spec §7.3.

    Home-occupancy fields (spec §6.7 cache-key note) are included so that a
    presence change — or a bypass state change — busts the Redis cache rather
    than returning a stale L1Decision computed under different presence context.
    """
    # Resolve zone metadata for cache-key purposes using the shared helper.
    # floor_type and debris_profile are included so that a metadata change
    # (e.g. floor type corrected in DB) busts the Redis cache rather than
    # returning a stale L1Decision computed under the old profile.
    _zm = resolve_zone_meta(zone, ctx)

    subset: dict[str, Any] = {
        "zone": zone,
        "score": round(ctx.zone_scores.get(zone, 0.0)),
        "zone_meta": {
            "floor_type": _zm.floor_type,
            "debris_profile": sorted(_zm.debris_profile) if _zm.debris_profile else [],
        },
        "people": {
            name: {
                "activity": p.activity,
                "confidence": round(p.confidence, 1),
                "piano": p.piano,
                "sleep_confidence": (
                    round(p.sleep_confidence, 1) if p.sleep_confidence is not None else None
                ),
            }
            for name, p in ctx.people.items()
        },
        "rooms": {
            k: {
                "detected": r.detected,
                "confidence": round(r.confidence, 1),
                "raw_occupancy": r.raw_occupancy,
            }
            for k, r in ctx.rooms.items()
        },
        "events": [
            {
                "title": e.title,
                "start_minute": e.start.hour * 60 + e.start.minute,
            }
            for e in ctx.upcoming_events
        ],
        "robot": {
            "state": ctx.robot_states.get(job.robot, None) and ctx.robot_states[job.robot].state,
            "battery": ctx.robot_states.get(job.robot, None)
            and ctx.robot_states[job.robot].battery_pct,
        },
        # Home-occupancy context (spec §6.7 cache-key note): presence changes and bypass
        # state changes must bust the cache or stale L1 decisions silently persist.
        "home": {
            "count": ctx.home_count,
            "empty": ctx.home_empty,
            "bypass": bypassed_for_zone,
            "reason": reason_for_zone,
        },
    }
    canonical = json.dumps(subset, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _render_prompt(
    job: VacuumJob,
    zone: str,
    ctx: ContextSnapshot,
    marginal_result: tuple[str, str, str],
    prompt_template: str,
    patterns_block: str,
    bypassed_for_zone: bool = False,
    reason_for_zone: str | None = None,
) -> str:
    """Render the Jinja2 prompt template for L1.

    Uses Jinja2 Environment to render {{ }} placeholders from the spec.
    """
    # Build PST timestamp string (Carlos uses PST year-round)
    import pytz  # type: ignore[import]
    from jinja2 import Environment, StrictUndefined

    pst = pytz.timezone("America/Los_Angeles")
    ts_pst = ctx.timestamp.astimezone(pst)
    ts_pst_str = ts_pst.strftime("%Y-%m-%d %H:%M:%S PST")

    # Time since last clean — use robot's last_dock_at if available
    robot = ctx.robot_states.get(job.robot)
    if robot and robot.last_dock_at:
        delta = ctx.timestamp - robot.last_dock_at
        hours = int(delta.total_seconds() // 3600)
        time_since_last_clean = f"{hours}h ago"
    else:
        time_since_last_clean = "unknown"

    # R1 outcomes for prompt
    r1_zone_active_use_outcome = marginal_result[2] if marginal_result else "PASS"
    r1_floor_clearance_outcome = "PASS"
    r1_transit_lookahead_outcome = "PASS"
    r1_noise_budget_outcome = marginal_result[2] if "noise" in marginal_result[2] else "PASS"
    r1_noise_radius_outcome = "PASS"

    impact = noise_impact(job, ctx)
    budget = noise_budget(ctx)

    # Build Jinja2 template variables
    env = Environment(undefined=StrictUndefined)

    # Build event PST renderers
    class EventView:
        def __init__(self, event):  # type: ignore[no-untyped-def]
            self._e = event

        @property
        def start_pst(self) -> str:
            return self._e.start.astimezone(pst).strftime("%H:%M PST")

        @property
        def end_pst(self) -> str:
            return self._e.end.astimezone(pst).strftime("%H:%M PST")

        @property
        def title(self) -> str:
            return self._e.title

        @property
        def calendar_id(self) -> str:
            return self._e.calendar_id

    events = [EventView(e) for e in ctx.upcoming_events]

    # Namespace object for ctx access in template
    class CtxView:
        def __init__(self) -> None:
            self.timestamp_pst = ts_pst_str
            self.zone_scores = ctx.zone_scores
            self.people = ctx.people
            self.rooms = ctx.rooms
            self.robot_states = ctx.robot_states
            self.upcoming_events = events

    # Resolve ZoneMeta for this zone via the shared helper.
    # Phase 1: single active zone — helper returns first entry (correct).
    # Phase 2+: helper will match by label field once ZoneMeta.label is added.
    zone_meta = resolve_zone_meta(zone, ctx)

    zone_score = ctx.zone_scores.get(zone)

    # Render
    tmpl = env.from_string(prompt_template)
    rendered = tmpl.render(
        ctx=CtxView(),
        time_since_last_clean=time_since_last_clean,
        r1_zone_active_use_outcome=r1_zone_active_use_outcome,
        r1_floor_clearance_outcome=r1_floor_clearance_outcome,
        r1_transit_lookahead_outcome=r1_transit_lookahead_outcome,
        r1_noise_budget_outcome=r1_noise_budget_outcome,
        r1_noise_radius_outcome=r1_noise_radius_outcome,
        noise_impact=round(impact, 2),
        noise_budget=round(budget, 2),
        patterns_block=patterns_block,
        zone_meta=zone_meta,
        zone_score=zone_score,
        # Home-occupancy context (spec §6.7)
        home_count=ctx.home_count,
        who_home=", ".join(ctx.who_home) if ctx.who_home else "nobody",
        home_empty=ctx.home_empty,
        occupancy_gate_bypassed=bypassed_for_zone,
        bypass_reason=reason_for_zone or "none",
    )
    return rendered


async def run_l1(
    job: VacuumJob,
    zone: str,
    ctx: ContextSnapshot,
    marginal_result: tuple[str, str, str],
    settings: Settings,
    litellm_client: httpx.AsyncClient,
    redis_client: aioredis.Redis,
    prompt_template: str,
    patterns_block: str,
    bypassed_for_zone: bool = False,
    reason_for_zone: str | None = None,
) -> L1Decision:
    """Run the L1 LLM disambiguation call for one (job, zone) pair.

    Per D13: one prompt per zone; LLM never reasons about batching.

    Failure modes (all conservative — defer):
      - Timeout (30s)          → defer with reason "l1_timeout"
      - Schema parse fail      → defer with reason "l1_schema_fail"
      - confidence < threshold → defer with reason "l1_low_confidence"
                                 (no AIT overflow queue in Phase 1 — just defer)

    Redis cache (TTL 600s): identical-context ticks within 10 min don't
    re-hit the LLM. Cache key = cortex:vacuumops:l1:<job_id>:<context_hash>.

    Spec: §7.3
    """
    # Check Redis cache first
    context_hash = _build_context_hash(
        job, zone, ctx,
        bypassed_for_zone=bypassed_for_zone,
        reason_for_zone=reason_for_zone,
    )
    cache_key = _L1_CACHE_KEY.format(context_hash=context_hash)

    try:
        cached = await redis_client.get(cache_key)
        if cached:
            decision = L1Decision.model_validate_json(cached)
            log.debug("l1_cache_hit", job_id=job.job_id, zone=zone, cache_key=cache_key)
            return decision
    except Exception as exc:
        log.warning("l1_cache_read_failed", job_id=job.job_id, zone=zone, error=str(exc))

    # Render prompt
    try:
        prompt = _render_prompt(
            job, zone, ctx, marginal_result, prompt_template, patterns_block,
            bypassed_for_zone=bypassed_for_zone,
            reason_for_zone=reason_for_zone,
        )
    except Exception as exc:
        log.error("l1_prompt_render_failed", job_id=job.job_id, zone=zone, error=str(exc))
        return L1Decision(
            decision="defer",
            confidence=0.0,
            reason="l1_prompt_render_failed",
        )

    # Build LiteLLM request
    payload = {
        "model": _L1_MODEL,
        "messages": [
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,  # Low temperature — we want deterministic judgment
        "max_tokens": 512,  # Bumped from 256 — cleaning params section adds ~100 tokens
    }

    # Call LiteLLM
    try:
        response = await litellm_client.post(
            "/v1/chat/completions",
            json=payload,
            timeout=_L1_TIMEOUT,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    except httpx.TimeoutException:
        log.warning("l1_timeout", job_id=job.job_id, zone=zone)
        return L1Decision(decision="defer", confidence=0.0, reason="l1_timeout")
    except Exception as exc:
        log.error("l1_call_failed", job_id=job.job_id, zone=zone, error=str(exc))
        return L1Decision(decision="defer", confidence=0.0, reason="l1_schema_fail")

    # Parse response — extract JSON from content
    try:
        # Strip markdown code fences if present
        clean = content.strip()
        if clean.startswith("```"):
            lines = clean.split("\n")
            clean = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
        decision = L1Decision.model_validate_json(clean)
    except Exception as exc:
        log.warning(
            "l1_schema_fail",
            job_id=job.job_id,
            zone=zone,
            content=content[:200],
            error=str(exc),
        )
        return L1Decision(decision="defer", confidence=0.0, reason="l1_schema_fail")

    # Confidence gate — defer if below threshold (no AIT overflow in Phase 1)
    # l1_overflow_confidence is on VacuumOpsConfig, not settings directly.
    # The caller (loop.py) checks this after run_l1 returns — we return the
    # decision and let loop.py handle the low-confidence path so it can log
    # the reason correctly.

    # Cache successful response
    try:
        await redis_client.set(
            cache_key,
            decision.model_dump_json(),
            ex=_L1_CACHE_TTL,
        )
    except Exception as exc:
        log.warning("l1_cache_write_failed", job_id=job.job_id, zone=zone, error=str(exc))

    log.info(
        "l1_decision",
        job_id=job.job_id,
        zone=zone,
        decision=decision.decision,
        confidence=decision.confidence,
        reason=decision.reason,
    )
    return decision
