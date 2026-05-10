"""create decision_log table

Revision ID: 9167d476eee1
Revises:
Create Date: 2026-05-09 20:53:15.875129

Schema spec: cortex_architecture.md v3.1 §3.3 + §3.5.
Forensics-grade audit log for every CORTEX decision.  Every row is write-once
(no UPDATE — only INSERT + soft-delete via superseded_by).

Columns:
  id               — surrogate PK (UUID stored as CHAR(36))
  ts               — decision timestamp (UTC, microsecond precision)
  tier             — enum: R0 | R1 | L1 | L2 | L3 | OVERFLOW
  model            — raw LiteLLM model string (e.g. gemma4:31b) or NULL for
                     R0/R1/OVERFLOW decisions (no LLM involved)
  module           — module name (e.g. vacuumops, presenceops)
  trigger_id       — opaque string identifying what triggered this decision
  context_snapshot — JSON blob: abbreviated context snapshot at decision time
  decision_payload — JSON blob: the Decision object produced
  outcome          — enum: executed | suppressed | failed | overflow
  latency_ms       — wall-clock ms from trigger to outcome (NULL for OVERFLOW
                     since outcome is deferred)
  confidence       — float 0.0–1.0; NULL for R0/R1 (rule-based, no score)
  superseded_by    — FK to id of a later revision of this decision (NULL =
                     current); used for forensic trails, never for updates
  notes            — free-form text for manual annotations / ARIIA review
  created_at       — wall-clock insert timestamp (UTC)
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------
revision: str = "9167d476eee1"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create decision_log table."""
    op.create_table(
        "decision_log",
        sa.Column("id", sa.CHAR(36), primary_key=True, comment="UUID v4"),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            index=True,
            comment="Decision timestamp (UTC)",
        ),
        sa.Column(
            "tier",
            sa.Enum("R0", "R1", "L1", "L2", "L3", "OVERFLOW", name="decision_tier"),
            nullable=False,
            index=True,
            comment="Decision tier per §3.3",
        ),
        sa.Column(
            "model",
            sa.String(128),
            nullable=True,
            comment="Raw LiteLLM model name (NULL for R0/R1/OVERFLOW)",
        ),
        sa.Column(
            "module",
            sa.String(64),
            nullable=False,
            index=True,
            comment="Module name (vacuumops, presenceops, …)",
        ),
        sa.Column(
            "trigger_id",
            sa.String(255),
            nullable=False,
            index=True,
            comment="Opaque trigger identifier (event ID, cron handle, etc.)",
        ),
        sa.Column(
            "context_snapshot",
            sa.JSON(),
            nullable=True,
            comment="Abbreviated context snapshot at decision time",
        ),
        sa.Column(
            "decision_payload",
            sa.JSON(),
            nullable=True,
            comment="Decision object produced (action, params, reasoning trace)",
        ),
        sa.Column(
            "outcome",
            sa.Enum(
                "executed",
                "suppressed",
                "failed",
                "overflow",
                name="decision_outcome",
            ),
            nullable=False,
            comment="Final outcome of this decision",
        ),
        sa.Column(
            "latency_ms",
            sa.Integer(),
            nullable=True,
            comment="Wall-clock ms from trigger to outcome (NULL for OVERFLOW)",
        ),
        sa.Column(
            "confidence",
            sa.Float(),
            nullable=True,
            comment="Score 0.0–1.0; NULL for R0/R1 (rule-based)",
        ),
        sa.Column(
            "superseded_by",
            sa.CHAR(36),
            sa.ForeignKey("decision_log.id", ondelete="SET NULL"),
            nullable=True,
            comment="FK to a later revision (NULL = current record)",
        ),
        sa.Column(
            "notes",
            sa.Text(),
            nullable=True,
            comment="Free-form annotations / ARIIA review notes",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="Row insert timestamp (UTC)",
        ),
        comment=(
            "Forensics-grade audit log for every CORTEX decision. "
            "Write-once: no UPDATEs; use superseded_by for corrections."
        ),
    )

    # Composite index for the common query pattern: module + tier + ts range.
    op.create_index(
        "ix_decision_log_module_tier_ts",
        "decision_log",
        ["module", "tier", "ts"],
    )


def downgrade() -> None:
    """Drop decision_log table."""
    op.drop_index("ix_decision_log_module_tier_ts", table_name="decision_log")
    op.drop_table("decision_log")
    # Enums must be dropped separately on PostgreSQL; MariaDB drops them with
    # the table but we call op.execute for portability.
    conn = op.get_bind()
    dialect = conn.dialect.name
    if dialect == "postgresql":
        op.execute(sa.text("DROP TYPE IF EXISTS decision_tier"))
        op.execute(sa.text("DROP TYPE IF EXISTS decision_outcome"))
