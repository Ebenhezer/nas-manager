"""USB / removable drives and Files page transfers (Phase 16).

Adds the ``removable:read`` and ``removable:write`` permission tokens
(granted to the seeded admin and operator roles) and the ``file_transfers``
table for background copy/move jobs.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TOKENS = ["removable:read", "removable:write"]
_ROLES = ["admin", "operator"]


def _roles_and_grants() -> tuple[sa.TableClause, sa.TableClause]:
    roles = sa.table("roles", sa.column("id", sa.Integer), sa.column("name", sa.String))
    grants = sa.table(
        "role_permissions",
        sa.column("role_id", sa.Integer),
        sa.column("permission", sa.String),
    )
    return roles, grants


def _grant_permissions() -> None:
    """Grant the Phase 16 tokens to the seeded roles (raw statements, so the
    migration never imports application code)."""
    bind = op.get_bind()
    roles, grants = _roles_and_grants()
    for name in _ROLES:
        role_id = bind.execute(
            sa.select(roles.c.id).where(roles.c.name == name)
        ).scalar_one_or_none()
        if role_id is None:
            continue
        for token in _TOKENS:
            exists = bind.execute(
                sa.select(grants.c.permission).where(
                    grants.c.role_id == role_id, grants.c.permission == token
                )
            ).first()
            if exists is None:
                bind.execute(grants.insert(), {"role_id": role_id, "permission": token})


def _revoke_permissions() -> None:
    bind = op.get_bind()
    _, grants = _roles_and_grants()
    bind.execute(grants.delete().where(grants.c.permission.in_(_TOKENS)))


def upgrade() -> None:
    op.create_table(
        "file_transfers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("sources", sa.Text(), nullable=False),
        sa.Column("dest_root", sa.String(length=1024), nullable=False),
        sa.Column("dest_path", sa.Text(), server_default="", nullable=False),
        sa.Column("conflict", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("done_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("total_files", sa.Integer(), server_default="0", nullable=False),
        sa.Column("done_files", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped_files", sa.Integer(), server_default="0", nullable=False),
        sa.Column("current_name", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("errors", sa.Text(), server_default="[]", nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("file_transfers", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_file_transfers_state"), ["state"], unique=False)
        batch_op.create_index(batch_op.f("ix_file_transfers_user_id"), ["user_id"], unique=False)
    _grant_permissions()


def downgrade() -> None:
    _revoke_permissions()
    with op.batch_alter_table("file_transfers", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_file_transfers_user_id"))
        batch_op.drop_index(batch_op.f("ix_file_transfers_state"))
    op.drop_table("file_transfers")
