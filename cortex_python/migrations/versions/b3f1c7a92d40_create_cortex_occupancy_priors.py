"""create cortex_occupancy_priors table

Revision ID: b3f1c7a92d40
Revises: 1123aa9df53e
Create Date: 2026-09-04 11:20:00.000000

Spec: C:/Jarvis/Team/TARS/cortex_vacuum_patience_and_pause_resume_implementation_spec.md
      §4.2 (PR A1 — CORTEX-side rolling occupancy prior learner)

WHY THIS TABLE EXISTS
---------------------
`opportunity()` (PR A2) needs a FORWARD-looking answer to "how likely is this
floor to be clear during the next ~30 minutes?".  Home Assistant cannot answer
it.  Re-verified live 2026-09-04: the `area_occupancy` HACS integration exposes
exactly three services (`run_analysis`, `export_config`, `purge_area_history`),
none of which return priors; `Prior.time_prior` returns only the *current*
(day_of_week, time_slot); the floor-aggregate sensors return `{}` from
`extra_state_attributes`; and `DEFAULT_SLOT_MINUTES = 60` is a non-configurable
module constant.  There is no forward-slot surface in HA and no way to
synthesise one from the existing entities, so CORTEX learns its own table.

SHAPE
-----
One row per (entity_id, day_of_week, slot).  Slots are 30 minutes of HOUSEHOLD
LOCAL time (America/Los_Angeles), 48 per day, 336 per entity per week.  CORTEX
owns this table precisely so it is not bound to HA's fixed 60-minute constant —
30 min is the right resolution against a ~25-minute Saros mission.

Row-count ceiling: 5 tracked entities x 336 = 1,680 rows.  Storage is a
non-issue; this table never grows with time, only with entity count.

`observations` is a small JSON array, newest last, capped at
`prior_learner_retention_weeks` entries (default 8 ~= 56 days, double HA's
28-day interval retention):

    [{"f": 0.4217, "at": "2026-09-04T22:00:00+00:00", "src": "native"}, ...]

  f    occupied FRACTION of that slot (0.0-1.0), derived from the HA state
       timeline via last_changed deltas -- NOT a tick-sampled binary.  The loop
       returns a 300 s interval whenever a robot is `cleaning`, so tick-sampling
       would be biased by exactly the periods this feature cares about.
  at   UTC instant at which the observed slot began.  Doubles as the dedupe key:
       re-observing the same slot instant replaces rather than appends, which is
       what makes both the one-time backfill and a mid-slot process restart
       idempotent.
  src  "native"   -- observed live by the learner at slot close-out
       "backfill" -- seeded from HA history by priors_backfill.py

`native_count` counts ONLY src="native" observations and is the sole input to
the `confidence` promotion from "thin" to "good".  Backfilled rows deliberately
lift `mean_occupied` (that is what removes the eight-week cold start) while
being unable to fool the confidence accounting into actuating early.

mean/stddev are DENORMALISED off `observations` on every write so the A2 read
path is a single indexed row fetch with no JSON parsing in the hot loop.  They
are derived, never authoritative -- `observations` is the source of truth and
any disagreement is a bug in the writer, not a reason to trust the scalars.

DOWNGRADE
---------
See downgrade() -- it snapshots before dropping.  This is a CREATE TABLE, so the
"exact-value capture-and-restore via a snapshot table, never a blanket-NULL
revert" rule that ARIIA enforced on homeOps#201 has no columns to capture; but
the spirit of it applies with more force here than there, because this table is
the ONLY calendar-bound asset in the whole patience/pause-resume train.  A bare
DROP would silently destroy up to eight weeks of wall-clock learning that
cannot be re-derived (HA's recorder keeps 20 days).  So the downgrade preserves
the rows in a snapshot table instead.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------
revision: str = "b3f1c7a92d40"
down_revision: str | Sequence[str] | None = "1123aa9df53e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "cortex_occupancy_priors"
_SNAPSHOT = "cortex_occupancy_priors_pre_b3f1c7a92d40"


def upgrade() -> None:
    """Create cortex_occupancy_priors.

    If a snapshot from a previous downgrade() is present, its rows are restored
    and the snapshot dropped -- so a downgrade/upgrade cycle is lossless rather
    than merely non-destructive.
    """
    op.create_table(
        _TABLE,
        # BIGINT on MariaDB (the production target); INTEGER on SQLite, because
        # SQLite only auto-increments a column declared exactly `INTEGER PRIMARY
        # KEY` — a BIGINT PK there is NOT NULL with no default and every INSERT
        # fails. Settings._validate_db_url accepts a sqlite:// DATABASE_URL, so
        # that path is reachable for local runs and tests, not hypothetical.
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "entity_id",
            sa.String(128),
            nullable=False,
            comment="HA binary_sensor whose occupancy this row summarises",
        ),
        sa.Column(
            "day_of_week",
            sa.SmallInteger(),
            nullable=False,
            comment="0 = Monday .. 6 = Sunday, in America/Los_Angeles local time",
        ),
        sa.Column(
            "slot",
            sa.SmallInteger(),
            nullable=False,
            comment="Local-time slot index, 0..47 for the default 30-minute slots",
        ),
        sa.Column(
            "observations",
            sa.JSON(),
            nullable=False,
            comment=(
                "JSON array, newest last, max prior_learner_retention_weeks entries: "
                '[{"f": <occupied fraction 0-1>, "at": <UTC slot start ISO>, '
                '"src": "native"|"backfill"}]'
            ),
        ),
        sa.Column(
            "native_count",
            sa.SmallInteger(),
            nullable=False,
            server_default="0",
            comment=(
                "Count of src='native' observations only. Drives the thin->good "
                "confidence promotion; backfilled rows cannot inflate it."
            ),
        ),
        sa.Column(
            "mean_occupied",
            sa.Numeric(5, 4),
            nullable=False,
            comment="Denormalised mean of observations[].f (native + backfill)",
        ),
        sa.Column(
            "stddev_occupied",
            sa.Numeric(5, 4),
            nullable=True,
            comment=(
                "Denormalised sample stddev of observations[].f; NULL when fewer "
                "than 2 observations exist. The variance HA cannot supply."
            ),
        ),
        sa.Column(
            "last_sample_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="UTC slot-start instant of the newest observation in the array",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("entity_id", "day_of_week", "slot", name="uq_entity_slot"),
        comment=(
            "Rolling occupancy priors learned by CORTEX VacuumOps. One row per "
            "(entity, local day-of-week, 30-min slot). Spec §4.2 / PR A1."
        ),
    )

    # The A2 read path is always (entity_id, day_of_week, slot) -- covered by the
    # unique constraint. This secondary index serves the whole-entity scans the
    # backfill and any future calibration report do.
    op.create_index("idx_entity", _TABLE, ["entity_id"])

    _restore_snapshot_if_present()


def downgrade() -> None:
    """Drop cortex_occupancy_priors, preserving its rows in a snapshot table.

    The learner's sample clock is wall-clock bound: 8 weeks of native
    observations take 8 weeks to re-accumulate and HA's recorder only retains 20
    days, so a re-run of the backfill cannot reconstruct what a bare DROP would
    delete. The snapshot makes the downgrade genuinely reversible.

    Guarded so a second downgrade cannot clobber a snapshot that a matching
    upgrade has not yet consumed -- losing the ORIGINAL data to a repeated
    downgrade would be exactly the failure this is here to prevent.
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = set(inspector.get_table_names())

    if _TABLE in tables:
        if _SNAPSHOT in tables:
            # A previous downgrade already captured the authoritative rows and no
            # upgrade has restored them yet. Leave that snapshot untouched.
            op.execute(sa.text(f"DROP TABLE {_TABLE}"))
        else:
            op.execute(sa.text(f"CREATE TABLE {_SNAPSHOT} AS SELECT * FROM {_TABLE}"))
            op.execute(sa.text(f"DROP TABLE {_TABLE}"))


def _restore_snapshot_if_present() -> None:
    """Copy a downgrade snapshot back into the fresh table, then drop it.

    Column list is explicit rather than `SELECT *` so that a snapshot taken by an
    older revision with a different column set fails loudly here instead of
    silently mis-populating the new table.
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if _SNAPSHOT not in set(inspector.get_table_names()):
        return

    cols = (
        "entity_id, day_of_week, slot, observations, native_count, "
        "mean_occupied, stddev_occupied, last_sample_at, created_at, updated_at"
    )
    op.execute(sa.text(f"INSERT INTO {_TABLE} ({cols}) SELECT {cols} FROM {_SNAPSHOT}"))
    op.execute(sa.text(f"DROP TABLE {_SNAPSHOT}"))
