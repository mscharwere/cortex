"""Predictive patience — `opportunity()` and `patience()` (PR A2).

Spec: C:/Jarvis/Team/TARS/cortex_vacuum_patience_and_pause_resume_implementation_spec.md §4.3
Design memo: C:/Jarvis/Team/TARS/cortex_predictive_patience_design.md §3.1-§3.3, §5.1-§5.2

WHAT THIS IS
------------
The decision core for "is now a good time to run the vacuum, or is a materially
better window arriving soon?".  Two functions and one record:

  * `opportunity()` composes the A1 learner's per-slot occupancy priors into a
    forward clear-probability curve, and scores how much of a mission-length
    window each candidate start time can be expected to keep clear.
  * `patience()` answers the separate question of whether the zone can afford to
    wait at all.
  * `OpportunityRead` (schemas.py) is what both feed into the decision log and
    the L1 prompt.

WHAT THIS IS NOT
----------------
This PR wires NOTHING.  There is no R1 rule here, no dispatch path change, no
job flag flipped and no I/O of any kind.  PR A3 adds `opportunity_check` to
`r1.py` in LOG-ONLY mode; PR A4 is the one-line flip that lets a verdict change a
dispatch, and it needs a 14-day soak and Carlos's explicit go.  Keeping the maths
in its own PR is deliberate: it is the part that can be reviewed on its own
merits, against synthetic tables, without a robot in the loop.

Every function in this module is PURE.  No Redis, no HTTP, no database, no
`datetime.now()`.  Callers pass the clock in.  That is what makes the fit
arithmetic assertable to the decimal place instead of merely exception-free.

────────────────────────────────────────────────────────────────────────────────
THE SAFETY PROPERTY THIS MODULE EXISTS TO PROTECT: **EMPTY IS NOT ZERO**
────────────────────────────────────────────────────────────────────────────────
A1's `SlotPrior.unavailable()` carries `mean_occupied = 0.0`, because the field
needs a number.  Read naively, `1 - 0.0 = 1.0` says "this slot is ALWAYS clear"
— so a purged history window, a cold slot, or a recorder outage would arrive
here as the strongest possible argument FOR running the vacuum.  That inversion
is the single most dangerous failure available in this file, and ARIIA held A1 to
exactly this standard on the write side.

The invariants that hold the line, all directly tested:

 1. NOTHING in this module reads `SlotPrior.mean_occupied` or `SlotPrior.p_clear`
    directly.  Every read goes through `_slot_p_clear()`, which returns None —
    not 0.0, not 1.0 — for any slot that is not backed by at least one real
    observation.
 2. A single unavailable slot anywhere in the CONSULTED SET collapses the whole
    read to `confidence="unavailable"`.  The consulted set is every slot any
    evaluated candidate mission would overlap, not just the current one.
 3. An unavailable read pins its numbers PESSIMISTIC (`p_clear_now = 0.0`,
    `expected_fit_now = 0.0`, `best_slot_offset = None`), so a caller that
    forgets to check `confidence` lands on "this looks bad" -> L1 judgment,
    never on "this looks perfect" -> dispatch.
 4. A consulted set whose slots all report the SAME mean is treated as
    unavailable too.  That is the spec's "indistinguishable from no information"
    rule, and it doubles as a tripwire for an all-zeros table — the exact shape
    a regression of A1's empty-is-not-zero rule would produce.
 5. Degradation always NAMES ITSELF in `degraded_reason`.  The 2026-08-31 root
    cause was a gate that no-op'd silently; a degradation nobody can see in the
    decision log is the same bug wearing a different hat.

DURATION: ACTIVE, NEVER WALL-CLOCK
----------------------------------
`avg_duration_min` (Saros: 44.8) measures dispatch -> mission-log close-out,
including the return-to-dock leg and a double dock-bounce.  It matches nothing
the robot actually does.  `avg_active_duration_min` (26.3) matches the robot's
own `cleaning` -> `returning` window.  The fit check MUST size against the
ACTIVE percentile plus an explicit return-leg allowance; sizing against the
wall-clock figure silently reserves ~70% more window than a mission needs, which
turns every fit check into a deferral.  A0 (homeOps#206) shipped
`p75_active_duration_min` / `p90_active_duration_min` for this and flagged the
distinction as load-bearing in its own code comments.

The ban is structural rather than advisory: `_read_active_minutes()` reads from a
fixed whitelist of ACTIVE field names, and `_WALL_CLOCK_FIELDS` records the
banned names so a test can assert the two sets never intersect.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from cortex_python.modules.vacuumops.config import VacuumOpsConfig
from cortex_python.modules.vacuumops.priors import (
    CONFIDENCE_GOOD,
    CONFIDENCE_THIN,
    CONFIDENCE_UNAVAILABLE,
    HOUSEHOLD_TZ,
    SlotPrior,
    slot_key,
    slot_start,
    slots_per_day,
)
from cortex_python.modules.vacuumops.schemas import OpportunityRead

# ── patience() bands ─────────────────────────────────────────────────────────
# Two values and only two. The design memo (§2.D-i) measured the dirtiness score
# moving in +5/+15/+20 event-driven steps, so a continuous ramp's intermediate
# values are never visited in a way that changes behaviour — its shape would be
# cosmetic while implying a precision the input does not have. These constants
# exist so the two-band property is greppable and assertable rather than implied
# by two `return` statements.
PATIENT = 1.0
IMPATIENT = 0.0

BAND_PATIENT = "patient"
BAND_IMPATIENT_HARD_CAP = "impatient:hard_cap"
BAND_IMPATIENT_SCORE = "impatient:score"
BAND_IMPATIENT_NO_CLOCK = "impatient:no_threshold_clock"

# Redis key holding the ISO instant a zone first crossed its dispatch threshold.
# Redis rather than a column: `vac_zone_cleanliness` has `last_cleaned_at` and
# `last_calculated_at` but no `threshold_crossed_at` (verified across
# 007_vacuum_dirtiness.js and every subsequent migration), and the spec is
# explicit that A2 must NOT add a migration for it. Set on the first tick a zone
# is observed above dispatch_threshold; deleted on dispatch. The key builder
# lives here — pure — so A3 and this module cannot drift on the key name.
_OVER_THRESHOLD_SINCE_KEY = "cortex:vacuumops:over_threshold_since:{zone_id}"


def over_threshold_since_key(zone_id: int | str) -> str:
    """Redis key for a zone's "first crossed dispatch_threshold at" instant."""
    return _OVER_THRESHOLD_SINCE_KEY.format(zone_id=zone_id)


# ── Duration inputs ──────────────────────────────────────────────────────────

# The ONLY fields this module will size a mission against. Keyed by the
# `opportunity_duration_percentile` config value.
_ACTIVE_PERCENTILE_FIELDS: dict[str, str] = {
    "p75": "p75_active_duration_min",
    "p90": "p90_active_duration_min",
}

# The mean-active fallback, used only when the percentile is absent (a pre-A0
# homeOps, or a robot with too few missions to have percentiles).
_ACTIVE_MEAN_FIELD = "avg_active_duration_min"

# Multiplier applied to the mean fallback. A mean under-reserves for half of all
# missions by construction; 1.4 is the design memo's crude stand-in for the
# percentile it is replacing, and it is why the fallback also forces
# confidence="thin" — a guessed reserve must never be able to buy a DEFER.
_MEAN_FALLBACK_MULTIPLIER = 1.4

# Wall-clock duration fields. NEVER read by this module. Listed so the ban is a
# testable property of the code rather than a comment nobody re-reads: see
# test_opportunity.py::TestActiveDurationOnly.
_WALL_CLOCK_FIELDS = frozenset(
    {
        "duration_min",
        "avg_duration_min",
        "median_duration_min",
        "p75_duration_min",
        "p90_duration_min",
    }
)

BASIS_UNAVAILABLE = "unavailable"
BASIS_MEAN_FALLBACK = "mean_fallback"


@dataclass(frozen=True)
class DurationEstimate:
    """How long to reserve for a mission, and what that number came from."""

    minutes: float | None
    basis: str
    source: str | None = None  # "zone" | "robot" — which stats payload was used
    degraded_reason: str | None = None

    @property
    def forces_thin(self) -> bool:
        """A guessed reserve can never support a "good" read (and so never a DEFER)."""
        return self.basis == BASIS_MEAN_FALLBACK

    @classmethod
    def unavailable(cls, reason: str) -> DurationEstimate:
        return cls(minutes=None, basis=BASIS_UNAVAILABLE, degraded_reason=reason)


def _positive_float(value: Any) -> float | None:
    """Coerce to a strictly-positive finite float, else None.

    Zero is REJECTED, not passed through. `get_vacuum_mission_stats` returns
    `avg_duration_min: 0` for Sam and Ethan — robots with no logged durations at
    all — and a 0-minute reservation would make every window a perfect fit. This
    is the duration-side twin of "empty is not zero".
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out <= 0.0:
        return None
    return out


def _read_active_minutes(
    stats: Mapping[str, Any] | None, percentile: str
) -> tuple[float | None, str]:
    """(minutes, basis) from ONE stats payload, reading ACTIVE fields only.

    Returns the requested active percentile if present, else the mean-active
    fallback scaled by `_MEAN_FALLBACK_MULTIPLIER`. Never touches a wall-clock
    field, and never falls back to one — a stats payload carrying only
    `avg_duration_min` yields nothing at all, by design.
    """
    if not stats:
        return None, BASIS_UNAVAILABLE

    field = _ACTIVE_PERCENTILE_FIELDS.get(percentile)
    if field is not None:
        exact = _positive_float(stats.get(field))
        if exact is not None:
            return exact, f"{percentile}_active+return_leg"

    mean_active = _positive_float(stats.get(_ACTIVE_MEAN_FIELD))
    if mean_active is not None:
        return mean_active * _MEAN_FALLBACK_MULTIPLIER, BASIS_MEAN_FALLBACK

    return None, BASIS_UNAVAILABLE


def duration_estimate(
    *,
    cfg: VacuumOpsConfig,
    zone_stats: Mapping[str, Any] | None = None,
    robot_stats: Mapping[str, Any] | None = None,
    zone_count: int = 1,
) -> DurationEstimate:
    """Minutes to reserve for the mission this decision would dispatch.

        base = <active percentile>            # p75 by default (A0)
        minutes = base + opportunity_return_leg_allowance_min

    Scope follows the spec: ZONE-scoped stats for a single-zone mission,
    ROBOT-WIDE stats for a batch. A batch is not the sum of its zones' means —
    the zones share one transit and one dock trip, so summing over-reserves badly
    and would defer batches that fit comfortably.

    If the preferred payload yields nothing usable, the other is tried and the
    substitution is recorded in `source`. If neither does, the estimate is
    UNAVAILABLE and `opportunity()` degrades on it — it does not invent a number.
    """
    prefer_zone = zone_count <= 1
    primary_name, primary = ("zone", zone_stats) if prefer_zone else ("robot", robot_stats)
    fallback_name, fallback = ("robot", robot_stats) if prefer_zone else ("zone", zone_stats)

    minutes, basis = _read_active_minutes(primary, cfg.opportunity_duration_percentile)
    source = primary_name
    if minutes is None:
        minutes, basis = _read_active_minutes(fallback, cfg.opportunity_duration_percentile)
        source = fallback_name

    if minutes is None:
        return DurationEstimate.unavailable(
            f"no_active_duration:zone_count={zone_count}:percentile="
            f"{cfg.opportunity_duration_percentile}"
        )

    # The return leg is added on BOTH paths. Active duration excludes the trip
    # home by construction (it is the robot's own cleaning-time counter), so the
    # allowance is orthogonal to which active statistic produced the base.
    total = minutes + max(0.0, float(cfg.opportunity_return_leg_allowance_min))
    reason = f"duration_basis={basis}:source={source}" if basis == BASIS_MEAN_FALLBACK else None
    return DurationEstimate(minutes=total, basis=basis, source=source, degraded_reason=reason)


# ── patience() ───────────────────────────────────────────────────────────────


def _hours_since(since: datetime | None, now: datetime) -> float | None:
    if since is None:
        return None
    if since.tzinfo is None or now.tzinfo is None:
        # Mixed awareness is a caller bug, not something to paper over with a
        # guess: an accidental naive/aware mix would silently produce a 7-hour
        # error in PST and could fire or suppress the starvation cap.
        raise ValueError("patience() requires timezone-aware datetimes on both sides")
    return (now - since).total_seconds() / 3600.0


def patience_band(
    *,
    zone_score: float,
    over_threshold_since: datetime | None,
    now: datetime,
    cfg: VacuumOpsConfig,
) -> str:
    """Which band `patience()` landed in. For the decision-log reason string.

    Separate from `patience()` so that function can keep the float return type
    the spec's A3 pseudocode compares against (`patience(...) == 0.0`), while the
    reason a zone is impatient still reaches the log. Every degraded path names
    itself — see invariant 5 in the module docstring.
    """
    over_h = _hours_since(over_threshold_since, now)
    if over_h is None:
        return BAND_IMPATIENT_NO_CLOCK
    if over_h >= cfg.patience_hard_cap_h:
        return BAND_IMPATIENT_HARD_CAP
    if zone_score >= cfg.patience_impatient_score:
        return BAND_IMPATIENT_SCORE
    return BAND_PATIENT


def patience(
    *,
    zone_score: float,
    over_threshold_since: datetime | None,
    now: datetime,
    cfg: VacuumOpsConfig,
) -> float:
    """How willing this zone is to wait for a better window. 1.0 or 0.0. Nothing else.

    A TWO-BAND STEP WITH AN ABSOLUTE TIME CAP — deliberately not a continuous
    ramp (design memo §3.3, spec §4.3):

      * `over_threshold_since` unknown  -> 0.0  (rule inert; see below)
      * over the threshold >= patience_hard_cap_h (6.0) -> 0.0  PRIMARY guard
      * zone_score >= patience_impatient_score (85.0)   -> 0.0  secondary guard
      * otherwise                                        -> 1.0

    ⚠ THE SCORE BAND IS A STARVATION BACKSTOP, NOT A "THE ROOM IS DIRTY SO GO"
    RULE. On Saros's 1F zones the score is driven by presence-derived signals, so
    a high score is itself evidence of imminent RE-occupancy — firing hardest at
    score 90 aims the mechanism at precisely the worst moment. That is why the
    band sits at 85 and why the ABSOLUTE TIME CAP is the primary lever. Do not
    tune `patience_impatient_score` down without reading patience memo §2.D-ii.

    An unknown `over_threshold_since` yields 0.0 (IMPATIENT), which makes the
    opportunity rule inert and leaves the decision exactly where it is today.
    That is the same fail-open direction as every other degraded path in this
    design: without the clock the starvation cap cannot fire, so a rule that
    could defer indefinitely must not be allowed to run at all. A3 sets the
    Redis key on the first tick a zone crosses threshold and reads it back in
    the same tick, so this branch means Redis is unreachable, not "just crossed".

    Returns exactly `PATIENT` or `IMPATIENT` — see the constants' comment for why
    an intermediate value would be dishonest about the resolution of the input.
    """
    band = patience_band(
        zone_score=zone_score,
        over_threshold_since=over_threshold_since,
        now=now,
        cfg=cfg,
    )
    return PATIENT if band == BAND_PATIENT else IMPATIENT


# ── Slot geometry ────────────────────────────────────────────────────────────


def lookahead_slot_count(
    *, patience_value: float, cfg: VacuumOpsConfig, slot_minutes: int = 30
) -> int:
    """`ceil(patience x opportunity_max_lookahead_h x 60 / slot_minutes)` (spec §4.3).

    Patience scales the horizon rather than merely gating it, so an impatient
    zone gets a lookahead of ZERO slots — it cannot defer to anything, because
    there is nothing forward to defer to. That makes "impatient" a structural
    property of the read, not just a branch A3 has to remember to take.
    """
    slots_per_day(slot_minutes)  # reject a slot length that does not divide 1440
    value = max(0.0, min(1.0, patience_value))
    horizon_min = value * float(cfg.opportunity_max_lookahead_h) * 60.0
    return max(0, math.ceil(horizon_min / slot_minutes))


def required_slot_count(
    *, duration_min: float, lookahead_slots: int, slot_minutes: int = 30
) -> int:
    """How many contiguous forward slots `opportunity()` needs, starting at now's slot.

    The last candidate start is `lookahead_slots` ahead of now; that mission then
    runs for `duration_min`. Because `now` can sit anywhere inside its own slot,
    a whole extra slot of headroom is needed at the tail. Sizing this too small
    is not a silent error — `opportunity()` degrades to "unavailable" rather than
    scoring a mission against slots it was not given.
    """
    slots_per_day(slot_minutes)
    span_min = (lookahead_slots + 1) * slot_minutes + max(0.0, duration_min)
    return max(1, math.ceil(span_min / slot_minutes))


def forward_slot_keys(
    *,
    now: datetime,
    count: int,
    slot_minutes: int = 30,
    tz: ZoneInfo = HOUSEHOLD_TZ,
) -> list[tuple[int, int]]:
    """`(day_of_week, slot)` keys for `count` contiguous slots starting at now's slot.

    The I/O boundary of this PR: A3 maps these onto `PriorStore.read_slot()` and
    hands the resulting `SlotPrior`s back to `opportunity()` in the same order.
    Keeping the key arithmetic here — pure, and sharing `priors.slot_key` /
    `slot_start` rather than reimplementing them — means the DST and
    local-vs-UTC handling A1 already got right is not re-derived at the call site.
    """
    start = slot_start(now, slot_minutes, tz)
    step = timedelta(minutes=slot_minutes)
    return [slot_key(start + step * i, slot_minutes, tz) for i in range(max(0, count))]


# ── The forward read ─────────────────────────────────────────────────────────


def _slot_p_clear(prior: SlotPrior | None) -> float | None:
    """P(clear) for one slot, or None when the slot carries no real observation.

    ⚠ THE ONLY PLACE IN THIS MODULE THAT MAY LOOK AT `mean_occupied`. A slot with
    no observations reports `mean_occupied = 0.0` because the dataclass field
    needs a number, and `SlotPrior.p_clear` would turn that into 1.0 — "always
    clear, dispatch now". Returning None instead is what keeps an absence of
    evidence from becoming evidence of absence.
    """
    if prior is None:
        return None
    if prior.confidence == CONFIDENCE_UNAVAILABLE or prior.sample_count <= 0:
        return None
    return max(0.0, min(1.0, 1.0 - prior.mean_occupied))


def _overlap_minutes(slot_index: int, start_min: float, end_min: float, slot_minutes: int) -> float:
    lo = float(slot_index * slot_minutes)
    hi = lo + slot_minutes
    return max(0.0, min(end_min, hi) - max(start_min, lo))


def _expected_fit(
    p_clear: Sequence[float | None],
    *,
    start_min: float,
    duration_min: float,
    slot_minutes: int,
) -> float | None:
    """Duration-weighted P(clear for the whole mission), or None if a slot is missing.

        expected_fit = PROD over overlapped slots s of  p_clear[s] ** (overlap(s) / slot)

    `start_min` and the mission window are measured in minutes from the START of
    slot 0, so a mission beginning part-way through the current slot is weighted
    on only the part of that slot it actually consumes.

    Slot independence is a known-wrong but useful approximation (spec §4.3, §10
    AR-5). It is materially weaker here than in the design memo, which OR-ed six
    correlated per-area priors: this composes ONE learned entity across time.
    """
    end_min = start_min + duration_min
    fit = 1.0
    touched = False
    for index in range(len(p_clear)):
        weight_min = _overlap_minutes(index, start_min, end_min, slot_minutes)
        if weight_min <= 0.0:
            continue
        value = p_clear[index]
        if value is None:
            return None
        touched = True
        fit *= value ** (weight_min / slot_minutes)
    if not touched:
        return None
    return max(0.0, min(1.0, fit))


def _unavailable_read(
    reason: str,
    *,
    duration: DurationEstimate,
    lookahead_slots: int,
    slot_minutes: int,
    consulted: int = 0,
) -> OpportunityRead:
    """A read with no opinion. Numbers pinned PESSIMISTIC — module invariant 3."""
    return OpportunityRead(
        p_clear_now=0.0,
        p_clear_curve=(),
        expected_fit_now=0.0,
        best_slot_offset=None,
        best_slot_gain=0.0,
        duration_estimate_min=duration.minutes,
        duration_basis=duration.basis,
        confidence=CONFIDENCE_UNAVAILABLE,
        degraded_reason=reason,
        lookahead_slots=lookahead_slots,
        slot_minutes=slot_minutes,
        consulted_slots=consulted,
    )


def opportunity(
    *,
    now: datetime,
    slots: Sequence[SlotPrior | None],
    duration: DurationEstimate,
    cfg: VacuumOpsConfig,
    patience_value: float = PATIENT,
    learner_native_days: float | None = None,
    context_degraded: bool = False,
    slot_minutes: int = 30,
    tz: ZoneInfo = HOUSEHOLD_TZ,
) -> OpportunityRead:
    """Score now against the next few slots. Pure; never raises on bad data.

    `slots[i]` is the learned prior for the slot `i` ahead of the one containing
    `now`, in the order `forward_slot_keys()` produced. `slots[0]` is therefore
    the CURRENT slot, not the next one. A `None` entry, or a `SlotPrior` the
    store returned as unavailable, is treated identically: no observation.

    The result NEVER recommends dispatching. It reports a fit and, at most,
    points at a better window; PR A3 turns that into a comfort-tier FAIL (defer)
    or AMBIGUOUS (escalate to L1), and the effectiveness rules that guard actual
    occupancy short-circuit ahead of it either way.

    Degradation ladder, in evaluation order, each naming itself in
    `degraded_reason` (module invariant 5):

      1. `context_degraded`            — HA WS down; the whole snapshot is suspect
      2. duration unavailable          — nothing to fit a window against
      3. learner younger than `opportunity_min_learn_days`
      4. fewer slots supplied than `required_slot_count()` asked for
      5. ANY consulted slot has no observations             <- the safety property
      6. every consulted slot reports an identical mean     <- "no information"
      7. any consulted slot is below `opportunity_min_slot_samples` native
         observations -> "thin" (usable, but can never justify a DEFER)
    """
    lookahead = lookahead_slot_count(
        patience_value=patience_value, cfg=cfg, slot_minutes=slot_minutes
    )

    if context_degraded:
        return _unavailable_read(
            "ctx_degraded",
            duration=duration,
            lookahead_slots=lookahead,
            slot_minutes=slot_minutes,
        )

    if duration.minutes is None:
        return _unavailable_read(
            duration.degraded_reason or "duration_unavailable",
            duration=duration,
            lookahead_slots=lookahead,
            slot_minutes=slot_minutes,
        )

    if learner_native_days is not None and learner_native_days < cfg.opportunity_min_learn_days:
        return _unavailable_read(
            f"learner_cold:{learner_native_days:.1f}d<{cfg.opportunity_min_learn_days}d",
            duration=duration,
            lookahead_slots=lookahead,
            slot_minutes=slot_minutes,
        )

    needed = required_slot_count(
        duration_min=duration.minutes, lookahead_slots=lookahead, slot_minutes=slot_minutes
    )
    if len(slots) < needed:
        return _unavailable_read(
            f"prior_slots_missing:need={needed}:have={len(slots)}",
            duration=duration,
            lookahead_slots=lookahead,
            slot_minutes=slot_minutes,
            consulted=len(slots),
        )

    consulted = list(slots[:needed])
    p_clear: list[float | None] = [_slot_p_clear(prior) for prior in consulted]

    # 5 — the safety property. One unobserved slot anywhere in the consulted set
    # collapses the read. Deliberately strict: the alternative is imputing a
    # value for a window nobody watched, and every imputation that is wrong in
    # the optimistic direction sends a vacuum into an occupied room.
    for index, value in enumerate(p_clear):
        if value is None:
            prior = consulted[index]
            key = (
                f"dow={prior.day_of_week},slot={prior.slot}"
                if prior is not None
                else f"offset={index}"
            )
            return _unavailable_read(
                f"prior_slot_unavailable:offset={index}:{key}",
                duration=duration,
                lookahead_slots=lookahead,
                slot_minutes=slot_minutes,
                consulted=needed,
            )

    known: list[float] = [v for v in p_clear if v is not None]

    # 6 — a flat table carries no decision-relevant information, and it is also
    # the exact shape an "empty written as 0.0" regression in A1 would produce.
    # Treating it as unavailable costs a real all-quiet overnight window (the
    # rule simply has no opinion there, which is today's behaviour) and buys a
    # tripwire on the failure that matters.
    if len(known) > 1 and len(set(known)) == 1:
        return _unavailable_read(
            f"priors_degenerate_uniform:p_clear={known[0]:.4f}",
            duration=duration,
            lookahead_slots=lookahead,
            slot_minutes=slot_minutes,
            consulted=needed,
        )

    # `now` can sit anywhere inside slot 0; the mission starts there, not at the
    # slot boundary, so the current slot is weighted on only its remaining part.
    now_offset_min = (now - slot_start(now, slot_minutes, tz)).total_seconds() / 60.0

    fit_now = _expected_fit(
        p_clear,
        start_min=now_offset_min,
        duration_min=duration.minutes,
        slot_minutes=slot_minutes,
    )
    if fit_now is None:  # pragma: no cover - unreachable after checks 4 and 5
        return _unavailable_read(
            "fit_uncomputable",
            duration=duration,
            lookahead_slots=lookahead,
            slot_minutes=slot_minutes,
            consulted=needed,
        )

    best_offset: int | None = None
    best_fit = fit_now
    for offset in range(1, lookahead + 1):
        candidate = _expected_fit(
            p_clear,
            start_min=now_offset_min + offset * slot_minutes,
            duration_min=duration.minutes,
            slot_minutes=slot_minutes,
        )
        if candidate is None:
            continue
        if candidate > best_fit:
            best_fit = candidate
            best_offset = offset

    # 7 — thin. Usable for an AMBIGUOUS->L1 escalation, never for a DEFER: A3
    # requires confidence == "good" for its `better_window` verdict, so a thin
    # read can raise a question but cannot answer one.
    thin_reasons: list[str] = []
    for index, prior in enumerate(consulted):
        if prior is not None and prior.native_count < cfg.opportunity_min_slot_samples:
            thin_reasons.append(
                f"offset={index}:native={prior.native_count}<{cfg.opportunity_min_slot_samples}"
            )
    if duration.forces_thin:
        thin_reasons.append(duration.degraded_reason or BASIS_MEAN_FALLBACK)

    confidence = CONFIDENCE_THIN if thin_reasons else CONFIDENCE_GOOD
    degraded_reason = "prior_thin:" + ",".join(thin_reasons[:4]) if thin_reasons else None

    return OpportunityRead(
        p_clear_now=known[0],
        p_clear_curve=tuple(v for v in p_clear[:lookahead] if v is not None),
        expected_fit_now=fit_now,
        best_slot_offset=best_offset,
        best_slot_gain=max(0.0, best_fit - fit_now),
        duration_estimate_min=duration.minutes,
        duration_basis=duration.basis,
        confidence=confidence,
        degraded_reason=degraded_reason,
        lookahead_slots=lookahead,
        slot_minutes=slot_minutes,
        consulted_slots=needed,
    )


def format_opportunity(read: OpportunityRead) -> str:
    """Compact one-line rendering for the decision log (`_fmt(opp)` in spec §4.4).

    Lives here rather than in A3's rule so the field set that reaches the
    decision log is reviewed alongside the maths that produced it.
    """
    parts = [
        f"fit_now={read.expected_fit_now:.2f}",
        f"p_clear_now={read.p_clear_now:.2f}",
        f"dur={read.duration_estimate_min:.0f}m" if read.duration_estimate_min else "dur=n/a",
        f"basis={read.duration_basis}",
        f"conf={read.confidence}",
    ]
    if read.best_slot_offset is not None:
        parts.append(f"best=+{read.best_slot_offset * read.slot_minutes}m")
        parts.append(f"gain={read.best_slot_gain:.2f}")
    if read.degraded_reason:
        parts.append(f"degraded={read.degraded_reason}")
    return " ".join(parts)
