"""Alembic migration environment for CORTEX-Python.

Reads DATABASE_URL from the environment so no credentials are baked into
alembic.ini (which is committed to git).

Wires SQLAlchemy metadata from cortex_python.db.base so ``alembic revision
--autogenerate`` can diff the schema.  Import cortex_python.db.models (once
it exists) to populate Base.metadata.

Spec: C:/Jarvis/Team/TARS/cortex_architecture.md v3.1 Appendix A.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# ---------------------------------------------------------------------------
# Alembic config object — provides access to alembic.ini values.
# ---------------------------------------------------------------------------
config = context.config

# Set up logging from alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# Override sqlalchemy.url from DATABASE_URL env var.
# This keeps credentials out of alembic.ini (which is committed to git).
# ---------------------------------------------------------------------------
_db_url = os.environ.get("DATABASE_URL")
if _db_url:
    config.set_main_option("sqlalchemy.url", _db_url)

# ---------------------------------------------------------------------------
# Target metadata — import CORTEX models so autogenerate can diff them.
# The declarative Base lives in cortex_python.db.base; individual table
# models import and register against it.  We import the models module so
# all table definitions are registered on the Base before autogenerate runs.
#
# Phase 0 Item 2: db.base + db.models don't exist yet (they land with the
# actual ORM layer in a later item).  We guard the import so alembic can
# still run offline/online with target_metadata=None in the interim.
# ---------------------------------------------------------------------------
try:
    import cortex_python.db.models  # type: ignore[import-not-found]  # noqa: F401
    from cortex_python.db.base import Base  # type: ignore[import-not-found]  # noqa: F401

    target_metadata = Base.metadata  # type: ignore[union-attr]
except ImportError:
    # Phase 0 Item 2: ORM layer not yet implemented; autogenerate not available.
    # Manual revision files (decision_log, ait_overflow_queue) still work.
    target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Emits SQL to stdout without a live DB connection.  Useful for review /
    dry-run before applying against the NAS MariaDB instance.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # safer for MariaDB ALTER TABLE ops
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (live DB connection).

    This is the normal production path: ``alembic upgrade head`` from the
    Docker entrypoint script.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # safer for MariaDB ALTER TABLE ops
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
