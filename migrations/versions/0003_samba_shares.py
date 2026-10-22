"""Samba share definitions (Phase 3: Samba integration).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "samba_shares",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("comment", sa.Text(), server_default="", nullable=False),
        sa.Column("read_only", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("browseable", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("guest_ok", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("valid_users", sa.Text(), server_default="", nullable=False),
        sa.Column("write_list", sa.Text(), server_default="", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_samba_shares")),
    )
    with op.batch_alter_table("samba_shares", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_samba_shares_name"), ["name"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("samba_shares", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_samba_shares_name"))

    op.drop_table("samba_shares")
