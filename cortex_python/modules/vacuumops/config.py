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
    )
