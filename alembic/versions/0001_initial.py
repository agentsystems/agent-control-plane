"""Initial tables for audit & invocation tracking.

Revision ID: 0001_initial
Revises:
Create Date: 2025-07-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# Revision identifiers, used by Alembic.
revision = "0001_initial"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:  # noqa: D401
    # Ensure pgcrypto for UUID generation
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto";')

    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("user_token", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("status_code", sa.SmallInteger(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("error_msg", sa.Text(), nullable=True),
    )

    op.create_table(
        "invocations",
        sa.Column("thread_id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent", sa.Text(), nullable=False),
        sa.Column("user_token", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("progress", sa.JSON(), nullable=True),
    )


def downgrade() -> None:  # noqa: D401
    op.drop_table("invocations")
    op.drop_table("audit_log")
