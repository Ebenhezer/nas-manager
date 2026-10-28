"""Public upload folder and quotas (Phase 4).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _grant_permissions() -> None:
    """Grant the Phase 4 permission tokens to the seeded system roles.

    Kept as raw statements so the migration never imports application code.
    ``admin`` receives read+write, ``operator`` receives read-only (admins
    view and manage uploads; operators may inspect usage but never change
    quotas or delete files).
    """
    bind = op.get_bind()
    roles = sa.table("roles", sa.column("id", sa.Integer), sa.column("name", sa.String))
    grants = sa.table(
        "role_permissions",
        sa.column("role_id", sa.Integer),
        sa.column("permission", sa.String),
    )
    for role_name, tokens in (
        ("admin", ["uploads:read", "uploads:write"]),
        ("operator", ["uploads:read"]),
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
    """Remove the Phase 4 tokens from the seeded system roles (downgrade)."""
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
                grants.c.permission.in_(["uploads:read", "uploads:write"]),
            )
        )


def upgrade() -> None:
    op.create_table(
        "public_upload_quotas",
        sa.Column("uploader_key", sa.String(length=128), nullable=False),
        sa.Column("quota_bytes", sa.BigInteger(), nullable=False),
        sa.Column("used_bytes", sa.BigInteger(), server_default="0", nullable=False),
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
        sa.PrimaryKeyConstraint("uploader_key", name=op.f("pk_public_upload_quotas")),
    )
    op.create_table(
        "public_upload_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uploader_key", sa.String(length=128), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_public_upload_files")),
    )
    with op.batch_alter_table("public_upload_quotas", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_public_upload_quotas_updated_at"), ["updated_at"])
    with op.batch_alter_table("public_upload_files", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_public_upload_files_uploader_key"), ["uploader_key"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_public_upload_files_created_at"), ["created_at"], unique=False
        )

    _grant_permissions()


def downgrade() -> None:
    _revoke_permissions()
    with op.batch_alter_table("public_upload_files", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_public_upload_files_created_at"))
        batch_op.drop_index(batch_op.f("ix_public_upload_files_uploader_key"))
    with op.batch_alter_table("public_upload_quotas", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_public_upload_quotas_updated_at"))

    op.drop_table("public_upload_files")
    op.drop_table("public_upload_quotas")
