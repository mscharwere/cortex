"""VacuumOps module-level configuration.

Separate from per-job descriptors (jobs.py). Loaded from env + module config
block at process start.

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

    # Master kill switch. Sourced from Settings.cortex_vacuumops_mop_enabled
    # (env: CORTEX_VACUUMOPS_MOP_ENABLED) and threaded in by loop.py — this
    # default is only the fallback for direct construction in tests.
    #
    # Defaults to FALSE: opt-in, not opt-out. Wet-mopping is a physical action on
    # real floors that runs unsupervised, so a missing or misspelled env var must
    # fail to "do not mop".
    #
    # When False the gate still evaluates every arm and records what it WOULD
    # have done (shadow mode, reason "off:disabled(would:...)"), so the decision
    # trail can be reviewed before the Saros is allowed to run wet.
    mop_enabled: bool = False


def build_vacuumops_config(settings: Settings) -> VacuumOpsConfig:
    """Map runtime Settings (env vars) onto the module config.

    This exists as a named function rather than an inline expression in
    vacuumops_loop() so the env-var -> behaviour path is directly testable.
    The mop kill switch originally shipped unwired precisely because the
    construction was a one-line expression buried in the loop: the field
    existed, the env var was documented, and nothing connected them. Tests that
    build VacuumOpsConfig directly cannot catch that class of bug — they have to
    go through this function.

    Every env-sourced field belongs here. If you add one to VacuumOpsConfig and
    it is meant to be operator-controlled, wire it in this function and assert it
    in tests/unit/vacuumops/test_mop.py::TestSettingsWiring.
    """
    return VacuumOpsConfig(
        dry_run=settings.cortex_vacuumops_dry_run,
        mop_enabled=settings.cortex_vacuumops_mop_enabled,
    )
