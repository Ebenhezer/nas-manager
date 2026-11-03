"""Configuration import runs (Phase 12).

Adds the ``config:export`` and ``config:import`` permission tokens (admin).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TOKENS = ["config:export", "config:import"]


def _roles_and_grants() -> tuple[sa.TableClause, sa.TableClause]:
    roles = sa.table("roles", sa.column("id", sa.Integer), sa.column("name", sa.String))
    grants = sa.table(
        "role_permissions",
        sa.column("role_id", sa.Integer),
        sa.column("permission", sa.String),
    )
    return roles, grants


def _grant_permissions() -> None:
    """Grant the Phase 12 tokens to the seeded admin role (raw statements, so
    the migration never imports application code)."""
    bind = op.get_bind()
    roles, grants = _roles_and_grants()
    role_id = bind.execute(
        sa.select(roles.c.id).where(roles.c.name == "admin")
    ).scalar_one_or_none()
    if role_id is None:
        return
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
        "config_import_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("source_name", sa.String(length=255), server_default="", nullable=False),
        sa.Column("exported_at", sa.String(length=64), nullable=True),
        sa.Column("sections", sa.Text(), server_default="{}", nullable=False),
        sa.Column("warnings", sa.Text(), server_default="[]", nullable=False),
        sa.Column("snapshot_encrypted", sa.Text(), nullable=False),
        sa.Column("database_copy", sa.Text(), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("actor_username", sa.String(length=64), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rolled_back_by", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_config_import_runs_actor_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_config_import_runs")),
    )
    with op.batch_alter_table("config_import_runs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_config_import_runs_created_at"), ["created_at"], unique=False
        )
    _grant_permissions()


def downgrade() -> None:
    _revoke_permissions()
    with op.batch_alter_table("config_import_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_config_import_runs_created_at"))
    op.drop_table("config_import_runs")
