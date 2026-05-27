"""VacuumOps job descriptors.

A *job* is a CORTEX-VacuumOps concept that bundles "what to clean, with which
robot, with which cleaning parameters, at which noise level, with which
decision-tier configuration." A loop tick evaluates each active job
independently.

Spec: C:/Jarvis/Team/TARS/cortex_vacuumops_module_spec.md §5
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class VacuumJob:
    """Base job descriptor. One concrete subclass per Phase 1 job.

    Phase 1 has exactly one job: LitterBoxJob.
    """

    # Identity
    job_id: str  # stable string, e.g. "litter_box_clean"
    robot: str  # "ethan" | "sam"
    zones: list[str]  # HomeOps zone_label values (must exist in vac_zone_cleanliness)
    floor: str = "1F"  # operating floor — used by floor_clearance_check

    # Noise model inputs (§6)
    noise_level: int = 1  # 1–5; intrinsic disruption from this robot running in these zones
    noise_radius: str = "floor"  # "local" | "floor" | "house"

    # Dispatch gating
    dispatch_threshold: float = 50.0  # zone_score > threshold → eligible
    cooldown_minutes: int = 240
    # Per-zone cooldown (D12). After this zone is included in a dispatch, skip
    # re-considering it for this long. Covers mission run + dock + post-clean
    # settle + resoil window. Independent of per-robot cooldown.
    bundle_threshold_pct: float = 0.70
    # D11 — bundle inclusion floor. A zone below dispatch_threshold may still
    # be included in a batch if its score ≥ bundle_threshold_pct × dispatch_threshold
    # AND a batch is already being assembled for this robot in this tick AND it
    # passes zone_effective + noise_acceptable. Bundle inclusion is deterministic
    # — no L1 call. Logged as bundled=True in the decision entry.

    # Cleaning parameters (passed to HomeOps /api/vacuum/trigger)
    cleaning_params: dict[str, str] = field(default_factory=dict)
    # e.g. {"passes": "auto", "intensity": "auto"} — matches HomeOps schema

    # Decision config
    prompt_file: str = ""  # relative path under cortex_python/modules/vacuumops/prompts/
    r0_checks: list[str] = field(default_factory=list)
    # names of R0 predicate functions to run
    r1_rules: list[str] = field(default_factory=list)
    # names of R1 scored rules to run
    l1_required: bool = False
    # If True, L1 always runs after R0/R1 PASS (no shortcut).
    # If False, L1 only runs when R1 produces an ambiguous result.


@dataclass
class LitterBoxJob(VacuumJob):
    """Phase 1 pilot job — Litter Box via Ethan (j9+, 1F, rid 2).

    Signal path: Petivity → smsGateway → HA → homeops.post_vacuum_zone_signal
    → vac_zone_cleanliness is already live (2026-05-23). Score moves from a
    real-world event, not a synthetic decay curve.

    Spec: §5 D3, D9.
    """

    job_id: str = "litter_box_clean"
    robot: str = "ethan"
    zones: list[str] = field(default_factory=lambda: ["Litter Box"])
    floor: str = "1F"  # Ethan operates on 1F — floor_clearance_check scope
    noise_level: int = 1  # 1F, far from bedrooms — low-impact run
    noise_radius: str = "floor"  # only 1F context contributes to noise_acceptable
    dispatch_threshold: float = 50.0
    # One Oliver visit (60pts) crosses threshold; ~6 Sasha visits (8pts each)
    # needed. Filters single-cat visits while ensuring Oliver's heavy
    # litter-kicking always triggers. D9.
    cooldown_minutes: int = 240
    # 4 hours post-dispatch. Covers mission run + dock + post-clean settle +
    # reasonable resoil window. D9.
    cleaning_params: dict[str, str] = field(
        default_factory=lambda: {
            "passes": "auto",
            "intensity": "auto",
        }
    )
    prompt_file: str = "prompts/litter_box.md"
    r0_checks: list[str] = field(
        default_factory=lambda: [
            "robot_docked",
            "battery_above_30",
            "score_above_threshold",
            "not_in_active_mission",
            "not_in_cooldown",
        ]
    )
    r1_rules: list[str] = field(
        default_factory=lambda: [
            # Effectiveness rules (hard FAIL on any → robot in the way)
            "zone_active_use_check",
            "floor_clearance_check",
            "transit_pattern_lookahead",
            # Comfort rules (ambiguous → may escalate to L1)
            "noise_budget_check",
            "noise_radius_check",
        ]
    )
    l1_required: bool = True
    # L1 always fires for LitterBoxJob — ensures cleaning params (passes/intensity)
    # are selected from Petivity signal context. cleaning_params is the fallback
    # if L1 is unreachable or declines to specify params.
