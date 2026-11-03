"""Photo gallery and personal media libraries (Phase 8).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TOKENS = ["media:read", "media:write", "media:admin"]
_GRANTS = (
    ("admin", _TOKENS),
    ("operator", ["media:read", "media:write"]),
    ("viewer", ["media:read", "media:write"]),
)


def _roles_and_grants() -> tuple[sa.TableClause, sa.TableClause]:
    roles = sa.table("roles", sa.column("id", sa.Integer), sa.column("name", sa.String))
    grants = sa.table(
        "role_permissions",
        sa.column("role_id", sa.Integer),
        sa.column("permission", sa.String),
    )
    return roles, grants


def _grant_permissions() -> None:
    """Grant the Phase 8 permission tokens to the seeded system roles.

    Kept as raw statements so the migration never imports application code.
    Every system role may use its own personal library (``media:read`` +
    ``media:write``); only ``admin`` manages shared libraries
    (``media:admin``).
    """
    bind = op.get_bind()
    roles, grants = _roles_and_grants()
    for role_name, tokens in _GRANTS:
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
    """Remove the Phase 8 tokens from every role (downgrade)."""
    _, grants = _roles_and_grants()
    op.get_bind().execute(grants.delete().where(grants.c.permission.in_(_TOKENS)))


def upgrade() -> None:
    op.create_table(
        "media_libraries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
        sa.Column("root", sa.String(length=1024), nullable=False),
        sa.Column("rel_path", sa.Text(), server_default="", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("include_videos", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("scan_state", sa.String(length=16), server_default="idle", nullable=False),
        sa.Column("scan_lease_owner", sa.String(length=128), nullable=True),
        sa.Column("scan_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_scan_status", sa.String(length=16), nullable=True),
        sa.Column("last_scan_error", sa.Text(), nullable=True),
        sa.Column("last_scan_summary", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_media_libraries_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_media_libraries_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_libraries")),
        sa.UniqueConstraint("name", name=op.f("uq_media_libraries_name")),
        sa.UniqueConstraint("owner_user_id", name=op.f("uq_media_libraries_owner_user_id")),
    )
    with op.batch_alter_table("media_libraries", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_media_libraries_next_scan_at"), ["next_scan_at"], unique=False
        )

    op.create_table(
        "media_changes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("library_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("rel_path", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["media_libraries.id"],
            name=op.f("fk_media_changes_library_id_media_libraries"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_changes")),
    )
    with op.batch_alter_table("media_changes", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_media_changes_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_media_changes_library_id"), ["library_id"], unique=False
        )

    op.create_table(
        "media_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("library_id", sa.Integer(), nullable=False),
        sa.Column("rel_path", sa.Text(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("mime", sa.String(length=64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("camera_make", sa.String(length=64), nullable=True),
        sa.Column("camera_model", sa.String(length=64), nullable=True),
        sa.Column("orientation", sa.Integer(), nullable=True),
        sa.Column("gps_lat", sa.Float(), nullable=True),
        sa.Column("gps_lon", sa.Float(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("video_codec", sa.String(length=32), nullable=True),
        sa.Column("audio_codec", sa.String(length=32), nullable=True),
        sa.Column("container", sa.String(length=64), nullable=True),
        sa.Column("bitrate", sa.BigInteger(), nullable=True),
        sa.Column("thumb_state", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("thumb_error", sa.Text(), nullable=True),
        sa.Column(
            "indexed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["media_libraries.id"],
            name=op.f("fk_media_items_library_id_media_libraries"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_items")),
        sa.UniqueConstraint("library_id", "rel_path", name=op.f("uq_media_items_library_id")),
    )
    with op.batch_alter_table("media_items", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_media_items_kind"), ["kind"], unique=False)
        batch_op.create_index(
            "ix_media_items_library_taken", ["library_id", "taken_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_media_items_sha256"), ["sha256"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_media_items_thumb_state"), ["thumb_state"], unique=False
        )

    op.create_table(
        "media_library_groups",
        sa.Column("library_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
            name=op.f("fk_media_library_groups_group_id_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["media_libraries.id"],
            name=op.f("fk_media_library_groups_library_id_media_libraries"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("library_id", "group_id", name=op.f("pk_media_library_groups")),
    )
    op.create_table(
        "media_albums",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("cover_item_id", sa.Integer(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["cover_item_id"],
            ["media_items.id"],
            name=op.f("fk_media_albums_cover_item_id_media_items"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_media_albums_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_albums")),
        sa.UniqueConstraint("owner_user_id", "name", name=op.f("uq_media_albums_owner_user_id")),
    )
    with op.batch_alter_table("media_albums", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_media_albums_owner_user_id"), ["owner_user_id"], unique=False
        )

    op.create_table(
        "media_album_items",
        sa.Column("album_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["album_id"],
            ["media_albums.id"],
            name=op.f("fk_media_album_items_album_id_media_albums"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["media_items.id"],
            name=op.f("fk_media_album_items_item_id_media_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("album_id", "item_id", name=op.f("pk_media_album_items")),
    )
    with op.batch_alter_table("media_album_items", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_media_album_items_item_id"), ["item_id"], unique=False)

    _grant_permissions()


def downgrade() -> None:
    _revoke_permissions()
    with op.batch_alter_table("media_album_items", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_media_album_items_item_id"))

    op.drop_table("media_album_items")
    with op.batch_alter_table("media_albums", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_media_albums_owner_user_id"))

    op.drop_table("media_albums")
    op.drop_table("media_library_groups")
    with op.batch_alter_table("media_items", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_media_items_thumb_state"))
        batch_op.drop_index(batch_op.f("ix_media_items_sha256"))
        batch_op.drop_index("ix_media_items_library_taken")
        batch_op.drop_index(batch_op.f("ix_media_items_kind"))

    op.drop_table("media_items")
    with op.batch_alter_table("media_changes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_media_changes_library_id"))
        batch_op.drop_index(batch_op.f("ix_media_changes_created_at"))

    op.drop_table("media_changes")
    with op.batch_alter_table("media_libraries", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_media_libraries_next_scan_at"))

    op.drop_table("media_libraries")
