"""create ait_overflow_queue table

Revision ID: 1123aa9df53e
Revises: 9167d476eee1
Create Date: 2026-05-09 20:54:15.273455

Schema spec: cortex_architecture.md v3.1 §3.3 (Overflow tier) + §3.7.

AIT Overflow Lane — durable queue for decisions that exceeded CORTEX's local
confidence threshold or hit a hard failure (schema parse fail, 90s timeout,
confidence < 0.65).  Jarvis claims items from this queue during the scheduled
overflow processing window (§3.7).

Status lifecycle: pending → claimed → resolved | expired
                                    ↘ rejected (Jarvis declines)

Columns:
  id               — surrogate PK (UUID CHAR(36))
  decision_log_id  — FK to the decision_log row that triggered overflow
  ts_enqueued      — when the item entered the queue (UTC)
  ts_claimed       — when Jarvis claimed the item (NULL = unclaimed)
  ts_resolved      — when the item reached a terminal state (NULL = open)
  status           — enum: pending | claimed | resolved | rejected | expired
  priority         — enum: normal | high | kid_bypass
                     (kid_bypass per §3.7 — kids' messages get priority)
  module           — module name (denormalised from decision_log for fast
                     queue-scan queries without a join)
  trigger_id       — denormalised trigger identifier
  handoff_envelope — JSON: OverflowHandoff payload sent to Jarvis
                     (rendered prompt, context digest, failure reason,
                      confidence score, suggested_action)
  resolution       — JSON: Jarvis's resolution (action taken, notes) — NULL
                     until resolved/rejected
  jarvis_notes     — free-form text from Jarvis / AIT member post-resolution
  created_at       — row insert timestamp (UTC)
  updated_at       — last status-change timestamp (UTC)
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------
revision: str = "1123aa9df53e"
down_revision: str | Sequence[str] | None = "9167d476eee1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ait_overflow_queue table."""
    op.create_table(
        "ait_overflow_queue",
        sa.Column("id", sa.CHAR(36), primary_key=True, comment="UUID v4"),
        sa.Column(
            "decision_log_id",
            sa.CHAR(36),
            sa.ForeignKey("decision_log.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
            comment="FK → decision_log row that triggered overflow",
        ),
        sa.Column(
            "ts_enqueued",
            sa.DateTime(timezone=True),
            nullable=False,
            index=True,
            comment="Enqueue timestamp (UTC)",
        ),
        sa.Column(
            "ts_claimed",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Claim timestamp (NULL = unclaimed)",
        ),
        sa.Column(
            "ts_resolved",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Terminal-state timestamp (NULL = open)",
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "claimed",
                "resolved",
                "rejected",
                "expired",
                name="overflow_status",
            ),
            nullable=False,
            server_default="pending",
            index=True,
            comment="Lifecycle status",
        ),
        sa.Column(
            "priority",
            sa.Enum("normal", "high", "kid_bypass", name="overflow_priority"),
            nullable=False,
            server_default="normal",
            index=True,
            comment="Dispatch priority; kid_bypass = expedited per §3.7",
        ),
        sa.Column(
            "module",
            sa.String(64),
            nullable=False,
            index=True,
            comment="Module name (denormalised from decision_log)",
        ),
        sa.Column(
            "trigger_id",
            sa.String(255),
            nullable=False,
            comment="Trigger identifier (denormalised from decision_log)",
        ),
        sa.Column(
            "handoff_envelope",
            sa.JSON(),
            nullable=False,
            comment=(
                "OverflowHandoff payload: rendered_prompt, context_digest, "
                "failure_reason, confidence, suggested_action"
            ),
        ),
        sa.Column(
            "resolution",
            sa.JSON(),
            nullable=True,
            comment="Jarvis resolution payload (NULL until terminal state)",
        ),
        sa.Column(
            "jarvis_notes",
            sa.Text(),
            nullable=True,
            comment="Free-form notes from Jarvis / AIT member post-resolution",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="Row insert timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="Last status-change timestamp (UTC)",
        ),
        comment=(
            "AIT Overflow Lane queue — items CORTEX could not resolve locally "
            "(confidence < 0.65, parse fail, or 90s timeout). "
            "Jarvis claims and resolves these during scheduled processing windows."
        ),
    )

    # Index for the Jarvis claim-next-pending query: status + priority + ts_enqueued
    op.create_index(
        "ix_ait_overflow_queue_status_priority_ts",
        "ait_overflow_queue",
        ["status", "priority", "ts_enqueued"],
    )


def downgrade() -> None:
    """Drop ait_overflow_queue table."""
    op.drop_index(
        "ix_ait_overflow_queue_status_priority_ts",
        table_name="ait_overflow_queue",
    )
    op.drop_table("ait_overflow_queue")
    with op.get_bind() as conn:
        dialect = conn.dialect.name
        if dialect == "postgresql":
            op.execute(sa.text("DROP TYPE IF EXISTS overflow_status"))
            op.execute(sa.text("DROP TYPE IF EXISTS overflow_priority"))
