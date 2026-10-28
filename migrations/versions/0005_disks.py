"""Privileged helper permissions: disk discovery and storage/RAID admin (Phase 5).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _grant_permissions() -> None:
    """Grant the Phase 5 tokens to the seeded system roles.

    Kept as raw statements so the migration never imports application code.
    ``admin`` receives read+write (discovery plus mount/unmount/scrub),
    ``operator`` receives read-only discovery (never state changes).
    """
    bind = op.get_bind()
    roles = sa.table("roles", sa.column("id", sa.Integer), sa.column("name", sa.String))
    grants = sa.table(
        "role_permissions",
        sa.column("role_id", sa.Integer),
        sa.column("permission", sa.String),
    )
    for role_name, tokens in (
        ("admin", ["disks:read", "disks:write"]),
        ("operator", ["disks:read"]),
    ):
        role_id = bind.execute(
            sa.select(roles.c.id).where(roles.c.name == role_name)
        ).scalar_one_or_none()
        if role_id is None:
            continue
        for token in tokens:
            exists = bind.execute(
                sa.select(grants.c.permission).where(
                    grants.c.role_id == role_id, grants.c.permission == token
                )
            ).first()
            if exists is None:
                bind.execute(grants.insert(), {"role_id": role_id, "permission": token})


def _revoke_permissions() -> None:
    """Remove the Phase 5 tokens from the seeded system roles (downgrade)."""
    bind = op.get_bind()
    roles = sa.table("roles", sa.column("id", sa.Integer), sa.column("name", sa.String))
    grants = sa.table(
        "role_permissions",
        sa.column("role_id", sa.Integer),
        sa.column("permission", sa.String),
    )
    role_ids = [
        row[0]
        for row in bind.execute(
            sa.select(roles.c.id).where(roles.c.name.in_(["admin", "operator"]))
        ).all()
    ]
    if role_ids:
        bind.execute(
            grants.delete().where(
                grants.c.role_id.in_(role_ids),
                grants.c.permission.in_(["disks:read", "disks:write"]),
            )
        )


def upgrade() -> None:
    # Phase 5 adds no tables: discovery reads the live system and privileged
    # actions go through the helper. Only the RBAC tokens change here.
    _grant_permissions()


def downgrade() -> None:
    _revoke_permissions()
