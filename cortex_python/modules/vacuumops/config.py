"""VacuumOps module-level configuration.

Separate from per-job descriptors (jobs.py). Loaded from env + module config
block at process start.

Spec: C:/Jarvis/Team/TARS/cortex_vacuumops_module_spec.md §5.1
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
