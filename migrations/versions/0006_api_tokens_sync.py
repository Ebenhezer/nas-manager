"""API tokens, resumable upload sessions and the file change journal (Phase 6).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TOKENS = ["api_tokens:read", "api_tokens:write"]


def _grant_permissions() -> None:
    """Grant the Phase 6 permission tokens to the seeded system roles.

    Kept as raw statements so the migration never imports application code.
    ``admin`` receives read+write (oversee and revoke anyone's tokens),
    ``operator`` receives read-only (token metadata, never the secret).
    """
    bind = op.get_bind()
    roles = sa.table("roles", sa.column("id", sa.Integer), sa.column("name", sa.String))
    grants = sa.table(
        "role_permissions",
        sa.column("role_id", sa.Integer),
        sa.column("permission", sa.String),
    )
    for role_name, tokens in (
        ("admin", _TOKENS),
        ("operator", ["api_tokens:read"]),
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
    """Remove the Phase 6 tokens from the seeded system roles (downgrade)."""
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
                grants.c.permission.in_(_TOKENS),
            )
        )


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=12), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_ip", sa.String(length=64), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_api_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_tokens")),
    )
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_api_tokens_token_hash"), ["token_hash"], unique=True)
        batch_op.create_index(batch_op.f("ix_api_tokens_user_id"), ["user_id"], unique=False)

    op.create_table(
        "upload_sessions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_id", sa.Integer(), nullable=True),
        sa.Column("target", sa.String(length=16), nullable=False),
        sa.Column("root", sa.Text(), nullable=False),
        sa.Column("rel_dir", sa.Text(), server_default="", nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("received_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("overwrite", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
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
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["token_id"],
            ["api_tokens.id"],
            name=op.f("fk_upload_sessions_token_id_api_tokens"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_upload_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_upload_sessions")),
    )
    with op.batch_alter_table("upload_sessions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_upload_sessions_expires_at"), ["expires_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_upload_sessions_user_id"), ["user_id"], unique=False)

    op.create_table(
        "file_changes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("root", sa.String(length=1024), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("old_path", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("is_dir", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_file_changes_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_file_changes")),
    )
    with op.batch_alter_table("file_changes", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_file_changes_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_file_changes_root"), ["root"], unique=False)

    _grant_permissions()


def downgrade() -> None:
    _revoke_permissions()
    with op.batch_alter_table("file_changes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_file_changes_root"))
        batch_op.drop_index(batch_op.f("ix_file_changes_created_at"))
    with op.batch_alter_table("upload_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_upload_sessions_user_id"))
        batch_op.drop_index(batch_op.f("ix_upload_sessions_expires_at"))
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_api_tokens_user_id"))
        batch_op.drop_index(batch_op.f("ix_api_tokens_token_hash"))

    op.drop_table("file_changes")
    op.drop_table("upload_sessions")
    op.drop_table("api_tokens")
