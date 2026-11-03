"""Video playback and streaming: playback mode and cached transcodes (Phase 9).

Videos indexed before this revision keep ``playback`` NULL; the next library
scan re-probes them once ffprobe is available. No permission tokens change:
streaming and transcoding use ``media:read`` / ``media:admin``.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_transcodes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=32), nullable=False),
        sa.Column("output_size", sa.BigInteger(), nullable=True),
        sa.Column("progress_percent", sa.Float(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Integer(), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["media_items.id"],
            name=op.f("fk_media_transcodes_item_id_media_items"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_media_transcodes_requested_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_transcodes")),
        sa.UniqueConstraint("item_id", name=op.f("uq_media_transcodes_item_id")),
    )
    with op.batch_alter_table("media_transcodes", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_media_transcodes_state"), ["state"], unique=False)

    with op.batch_alter_table("media_items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("rotation", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("playback", sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("media_items", schema=None) as batch_op:
        batch_op.drop_column("playback")
        batch_op.drop_column("rotation")

    with op.batch_alter_table("media_transcodes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_media_transcodes_state"))

    op.drop_table("media_transcodes")
