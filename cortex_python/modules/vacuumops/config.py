"""VacuumOps module-level configuration.

Separate from per-job descriptors (jobs.py). Most fields are loaded from env +
module config block at process start (build_vacuumops_config()). `mop_enabled`
is the one exception — it is live and DB-backed (HomeOps), re-read every loop
tick rather than fixed at process start; see its field docstring below.

Spec: C:/Jarvis/Team/TARS/cortex_vacuumops_module_spec.md §5.1
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cortex_python.config.settings import Settings


@dataclass
class VacuumOpsConfig:
    """Module-level VacuumOps configuration.

    Per-job tuning lives on the job descriptors (jobs.py). This class governs
    robot-level and module-level defaults.
    """

    # Per-robot cooldown (D12). Minimum time between any dispatches for this
    # robot, regardless of zone scores. Resets on every dispatch — single-zone
    # or batched. Protects against rapid re-dispatch if a third zone crosses
    # threshold shortly after a run. Independent of per-zone cooldown
    # (JobDescriptor.cooldown_minutes). Default 120 min. May be overridden
    # per-robot via robot_cooldown_overrides.
    robot_cooldown_minutes: int = 120
    robot_cooldown_overrides: dict[str, int] = field(default_factory=dict)
    # e.g. {"sam": 180} to give Sam a longer cooldown than Ethan.

    # Dry-run toggle (env: CORTEX_VACUUMOPS_DRY_RUN). When True, loop evaluates
    # and logs decisions but does NOT call /api/vacuum/trigger.
    dry_run: bool = False

    # L1 confidence threshold for overflow queue (§7.3). L1 results with
    # confidence below this value defer conservatively (no AIT overflow in
    # Phase 1 — just log and defer).
    l1_overflow_confidence: float = 0.65

    # ── Mop-cadence gate (mop.py) ────────────────────────────────────────────
    # The 2026-07-03 design specifies intensity as "light/deep". The real HA
    # control (select.saros_10r_mop_intensity) is a 6-step scale:
    #   slight | low | medium | moderate | high | extreme
    #
    # Mapping rationale:
    #   light → "low"   — "slight" is barely damp and effectively pointless for a
    #                     routine maintenance pass; "low" is the lightest setting
    #                     that actually wets the pad.
    #   deep  → "high"  — "extreme" saturates the pad, which risks standing water
    #                     on sealed hardwood and lengthens dry time well past what
    #                     an unattended daytime run should leave behind. "high" is
    #                     the strongest setting that stays safe unsupervised.
    #
    # Both extremes of the HA scale are deliberately unused. Tunable here rather
    # than hardcoded so Carlos can shift the pair after observing real results.
    mop_intensity_light: str = "low"
    mop_intensity_deep: str = "high"

    # Floor-type safety cap. Mop intensity is a UNIT-level setting applied once
    # per mission, so a batch spanning several zones runs at a single intensity.
    # If any zone in the batch has one of these floor types, the batch is capped
    # at mop_intensity_light regardless of how overdue it is — the wettest
    # setting is chosen for the most water-sensitive surface in the run, not the
    # dirtiest zone.
    mop_deep_floor_type_blocklist: tuple[str, ...] = ("hardwood",)

    # Master kill switch.
    #
    # NOT env-sourced. Originally CORTEX_VACUUMOPS_MOP_ENABLED (env var, took
    # effect only at process start — a real flip required SSH + .env edit +
    # `docker compose up -d` on the NAS). Carlos asked for a fast kill switch
    # instead, so this is now a live, DB-backed setting
    # (HomeOps `cortex_vacuumops_settings`, GET/PATCH /api/cortex/vacuumops-
    # settings) that loop.py reads fresh every tick via
    # HomeOpsAdapter.get_vacuumops_mop_enabled() and threads in per-tick with
    # `dataclasses.replace(vacuumops_cfg, mop_enabled=live_value)` — see
    # loop.vacuumops_loop(). build_vacuumops_config() below deliberately does
    # NOT set this field; the dataclass default is only the fallback for
    # direct construction (tests, or a code path that never receives a live
    # value) and is never the value the running loop actually dispatches on.
    #
    # Two prior-art precedents inform this design:
    #   1. The analogous per-unit vac_units.dry_run column (commit 682f687)
    #      proved the "small DB flag, hot-toggleable, no redeploy" shape works.
    #   2. commit bb0d47b then REMOVED that dry_run flow's global env-var
    #      override entirely ("remove global dry_run override; per-unit DB
    #      flags are sole control") because an env var silently OR-ing with a
    #      DB flag produced confusing state — an operator flips the DB value
    #      expecting it to take effect and it silently doesn't, because a
    #      stale env var is still forcing the old behavior. This field follows
    #      that same precedent: DB is the sole source of truth, no env-var
    #      override kept. See the PR description for the fuller reasoning.
    #
    # Defaults to FALSE: opt-in, not opt-out. Wet-mopping is a physical action on
    # real floors that runs unsupervised, so any read failure (see
    # HomeOpsAdapter.get_vacuumops_mop_enabled()'s docstring for the full list —
    # HomeOps unreachable, malformed response, missing/non-bool field) must
    # resolve to "do not mop".
    #
    # When False the gate still evaluates every arm and records what it WOULD
    # have done (shadow mode, reason "off:disabled(would:...)"), so the decision
    # trail can be reviewed before the Saros is allowed to run wet.
    mop_enabled: bool = False

    # ── Rolling occupancy prior learner (priors.py, PR A1) ───────────────────
    # Spec: cortex_vacuum_patience_and_pause_resume_implementation_spec.md §4.2 + §7
    #
    # The learner writes cortex_occupancy_priors and NOTHING reads it yet — PR A2's
    # opportunity() is its only consumer. A1 ships alone and first because the
    # learner's sample clock is the only calendar-bound item in the whole
    # patience/pause-resume train: every other PR is engineering time, this one is
    # wall-clock time, so it has to start accruing before the rest is built.

    # Master switch. Env-wired (CORTEX_VACUUMOPS_PRIOR_LEARNER_ENABLED) in
    # build_vacuumops_config below — a kill switch that ships unwired is a known
    # ARIIA finding in this module (finding 1 on the original mop_enabled env var:
    # the field existed, the env var was documented, and nothing connected them).
    # TestPriorLearnerSettingsWiring guards it through that function, which is the
    # only place a test can catch that class of bug.
    #
    # Defaults TRUE, unlike mop_enabled. The asymmetry is deliberate and worth
    # stating: mop_enabled actuates a physical wet pass on real floors and so is
    # opt-in; the learner writes rows to a table nothing reads. Its worst failure
    # mode is a wasted HA history call every 30 minutes.
    prior_learner_enabled: bool = True

    # 30-minute slots => 48/day, 336/week/entity. CORTEX keeps its own table
    # precisely so it is not bound by HA's non-configurable
    # area_occupancy DEFAULT_SLOT_MINUTES = 60; 30 min is the right resolution
    # against a ~25-minute Saros mission. Must divide 1440 (priors.slots_per_day
    # rejects anything else, rather than silently producing a runt final slot).
    prior_learner_slot_minutes: int = 30

    # Retention, in weekly recurrences of each slot. Each slot is observed once a
    # week, so 8 observations ≈ 56 days — double HA's own 28-day interval
    # retention. Stored as a JSON array rather than a running mean so that stddev
    # is available on read: the variance the patience memo §3.2 promised and which
    # HA cannot supply at all.
    prior_learner_retention_weeks: int = 8

    # Tracked entities. The FIRST is the one that matters — CORTEX's occupancy gate
    # reads binary_sensor.first_floor_occupancy_status directly (homeOps#201), so
    # the learner learns THE BINARY THE GATE READS rather than reconstructing a
    # floor from member areas. That drops the largest piece of complexity the
    # patience memo contemplated and removes its OR-vs-MEAN calibration gap
    # entirely (§4.2).
    #
    # The four 1F member areas are SECONDARY/DIAGNOSTIC: they are not consumed by
    # opportunity() and have no behavioural coupling. They ride along on the same
    # timeline pass for a handful of extra reads, so that if Carlos ever reopens
    # the deferred §8.4 question ("is the 1F rollup a human-presence proxy at
    # all?" — D1, §10 AR-1) eight weeks of the exact comparison data are already
    # in the table instead of needing to be gathered from scratch.
    #
    # All five verified live in HA 2026-09-04.
    prior_learner_entities: tuple[str, ...] = (
        "binary_sensor.first_floor_occupancy_status",  # primary — the gate signal
        "binary_sensor.kitchen_occupancy_status",  # secondary/diagnostic
        "binary_sensor.living_room_occupancy_status",  # secondary/diagnostic
        "binary_sensor.hallway_occupancy_status",  # secondary/diagnostic
        "binary_sensor.bathroom_occupancy_status",  # secondary/diagnostic
    )

    # One-time HA-history seed. Removes the eight-week cold start.
    #
    # ⚠ 28 is what §4.2 specified; it is NOT what the recorder can deliver.
    # Verified live 2026-09-04: history for the 1F rollup is dense back to
    # 2026-08-15 and absent on 2026-08-14 and earlier, because
    # home-assistant-config/configuration.yaml:13 sets `purge_keep_days: 20`.
    # Real yield is therefore 2–3 observations per slot, not the 4 §4.2 assumed.
    #
    # The default stays at 28 on purpose. Over-asking costs only a few empty
    # windows (which seed nothing — priors_backfill treats empty as unknown, never
    # as unoccupied), whereas pinning CORTEX's default to HA's CURRENT recorder
    # setting would silently rot the day Carlos changes it — Dream Pass v5 #1,
    # "logged once ≠ tracked". BackfillReport measures the coverage actually
    # achieved on every run instead of assuming it.
    prior_learner_backfill_days: int = 28

    # History is read in chunks of this many days. A single 28-day range over a
    # sensor that flips dozens of times an hour is a large response and a long
    # held-open read; chunking bounds both, and a failed chunk costs its own slots
    # rather than the whole entity.
    prior_learner_backfill_chunk_days: int = 7

    # Ceiling on slots closed out in one tick. Bounds the HA history reads a single
    # tick can issue after a long outage. Oldest-first, so a process that was down
    # for days catches up across successive ticks rather than in one burst.
    # 48 = one full day of 30-minute slots.
    prior_learner_max_catchup_slots: int = 48

    # Native (non-backfilled) observations a slot needs before its confidence
    # promotes from "thin" to "good". Consumed by A1 only to LABEL rows; A2's
    # opportunity() is what acts on the label. Backfilled samples deliberately
    # cannot satisfy this — they lift the mean without being able to buy an early
    # actuation.
    opportunity_min_slot_samples: int = 3

    # ── Opportunity / patience (opportunity.py, PR A2) ───────────────────────
    # Spec: cortex_vacuum_patience_and_pause_resume_implementation_spec.md §4.3 + §7
    #
    # A2 ships the maths only. Nothing here changes a dispatch: the R1 rule that
    # consumes these constants is PR A3, and it ships log-only behind a second
    # flag (opportunity_actuate) that only PR A4 flips. These are the tunables
    # §11 O-8 flags as TARS's judgment rather than Carlos's measurements —
    # everything except the D2/D7 pause/park numbers, which live in homeOps.

    # Forward horizon, in hours. Beyond ~3 h a slot prior backed by <=8 weekly
    # observations is not distinguishable from its own noise, so a longer
    # lookahead would buy precision the input does not have. Also bounds the
    # damage a single deferral can do: the rule re-evaluates every tick and can
    # never defer past this horizon in one decision.
    opportunity_max_lookahead_h: float = 3.0

    # Native (non-backfilled) learner days required before opportunity() will
    # report confidence="good". Distinct from opportunity_min_slot_samples,
    # which is a per-slot bar: this one is the whole-learner age bar, and it is
    # what stops a table seeded entirely from the 28-day HA-history backfill from
    # ever satisfying the actuation floor.
    opportunity_min_learn_days: int = 14

    # DEFER band. Both must hold, AND confidence must be "good":
    #   best_slot_gain    >= opportunity_strong_gain   (the wait buys a lot)
    #   expected_fit_now  <= opportunity_weak_fit      (now is genuinely poor)
    # Two conditions rather than one because a large gain over an already-fine
    # window is not a reason to make the house wait.
    opportunity_strong_gain: float = 0.35
    opportunity_weak_fit: float = 0.30

    # AMBIGUOUS band — escalates to L1 rather than deciding algorithmically.
    # This is the design's release valve: a marginal fit is a judgment call, and
    # judgment calls go to the LLM tier, never to a threshold.
    opportunity_marginal_fit: float = 0.55

    # Added to the ACTIVE-duration percentile to reserve for the return-to-dock
    # leg, which active duration excludes by construction (§4.1 ⚠).
    opportunity_return_leg_allowance_min: float = 5.0

    # Which active-duration percentile backs the fit check. "p90" is available
    # for a more conservative reserve. A mean is NOT an option here — sizing a
    # window on a mean under-reserves for half of all missions by definition —
    # and the wall-clock percentiles are not an option either (see
    # opportunity._ACTIVE_PERCENTILE_FIELDS).
    opportunity_duration_percentile: str = "p75"

    # ── patience() — two-band step + absolute cap (§4.3) ─────────────────────
    # PRIMARY starvation guard. Hours a zone may sit above its dispatch
    # threshold before patience collapses to 0 and the opportunity rule goes
    # inert. Saros's observed dispatch cadence is ~3.6/day (~6.7 h mean
    # spacing), so a 6 h cap costs at most one normal cycle.
    patience_hard_cap_h: float = 6.0

    # SECONDARY guard only, and set high ON PURPOSE. On Saros's 1F zones the
    # dirtiness score is driven by presence-derived signals (kitchen_presence
    # +15, post_meal +15, heavy_activity +20, cooking_started +5,
    # entry_door_open +8), so a HIGH SCORE IS ITSELF EVIDENCE OF IMMINENT
    # RE-OCCUPANCY. A low value here would aim the mechanism at exactly the
    # wrong moment. Do not tune this down without reading patience memo §2.D-ii.
    patience_impatient_score: float = 85.0

    @property
    def prior_learner_retention(self) -> int:
        """Max observations kept per slot. One recurrence per week, so weeks == rows."""
        return self.prior_learner_retention_weeks


def build_vacuumops_config(settings: Settings) -> VacuumOpsConfig:
    """Map runtime Settings (env vars) onto the module config.

    This exists as a named function rather than an inline expression in
    vacuumops_loop() so the env-var -> behaviour path is directly testable.
    A kill switch shipping unwired is a known failure mode here (ARIIA finding
    1 on the original mop_enabled env var): the field existed, the env var was
    documented, and nothing connected them. Tests that build VacuumOpsConfig
    directly cannot catch that class of bug — they have to go through this
    function.

    Every env-sourced field belongs here. If you add one to VacuumOpsConfig and
    it is meant to be operator-controlled, wire it in this function and assert
    it in tests/unit/vacuumops/test_mop.py::TestSettingsWiring.

    mop_enabled is intentionally NOT wired here — it is no longer env-sourced.
    See the field's docstring above for the live DB-backed replacement; the
    per-tick wiring lives in loop.vacuumops_loop(), and its own regression
    coverage lives in TestLiveMopEnabledWiring (test_mop.py), analogous to
    what TestSettingsWiring does for the fields that remain env-sourced.
    """
    return VacuumOpsConfig(
        dry_run=settings.cortex_vacuumops_dry_run,
        prior_learner_enabled=settings.cortex_vacuumops_prior_learner_enabled,
    )
