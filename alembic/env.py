"""Alembic environment setup for Agent Control Plane.

This config does **not** rely on SQLAlchemy models.  We use raw SQL in the
migration scripts and construct the database URL from the same environment
variables used by the runtime so that `alembic upgrade head` works in both
local‐compose and CI.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# ---------------------------------------------------------------------------
# Alembic config & logging
# ---------------------------------------------------------------------------
config = context.config  # type: ignore[attr-defined]
fileConfig(config.config_file_name)  # reads loggers section of alembic.ini

# No ORM models → no target_metadata; we use imperative migrations.
target_metadata = None  # type: ignore  # noqa: S105 – not a secret


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db_url() -> str:
    """Build a SQLAlchemy URL from environment variables or DSN."""
    if dsn := os.getenv("ACP_AUDIT_DSN"):
        return dsn
    host = os.getenv("PG_HOST", "localhost")
    db = os.getenv("PG_DB", "agent_cp")
    user = os.getenv("PG_USER", "agent")
    pw = os.getenv("PG_PASSWORD", "agent")
    return f"postgresql+psycopg2://{user}:{pw}@{host}/{db}"


# ---------------------------------------------------------------------------
# Offline migration – generate SQL script without DB connection
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:  # noqa: D401 (imperative mood)
    url = _make_db_url()
    context.configure(url=url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online migration – run against live database
# ---------------------------------------------------------------------------


def run_migrations_online() -> None:  # noqa: D401
    connectable = engine_from_config(  # type: ignore[arg-type]
        config.get_section(config.config_ini_section),  # type: ignore[arg-type]
        prefix="sqlalchemy.",
        url=_make_db_url(),
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:  # type: ignore[attr-defined]
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


# Entrypoint -----------------------------------------------------------------
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
