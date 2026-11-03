"""Remote syslog destinations and the audit hash chain (Phase 13).

Adds ``syslog_destinations``, the ``audit_events.chain_hash`` column and an
index on ``audit_events.actor_username``. No new permission tokens: the
Logging page reuses ``system:read`` / ``system:write`` and the audit search
``audit:read``.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "syslog_destinations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("protocol", sa.String(length=8), nullable=False),
        sa.Column("ca_pem", sa.Text(), nullable=True),
        sa.Column("format", sa.String(length=16), server_default="rfc5424", nullable=False),
        sa.Column("facility", sa.String(length=16), server_default="local0", nullable=False),
        sa.Column("forward_audit", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("audit_categories", sa.Text(), server_default="[]", nullable=False),
        sa.Column("audit_failures_only", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("forward_logs", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("log_min_level", sa.String(length=16), server_default="WARNING", nullable=False),
        sa.Column("last_audit_id", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_syslog_destinations")),
        sa.UniqueConstraint("name", name=op.f("uq_syslog_destinations_name")),
    )
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("chain_hash", sa.String(length=64), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_audit_events_actor_username"), ["actor_username"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_audit_events_actor_username"))
        batch_op.drop_column("chain_hash")
    op.drop_table("syslog_destinations")
