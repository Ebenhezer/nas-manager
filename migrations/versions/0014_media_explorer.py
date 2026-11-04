"""Per-user media favorites for the explorer-style gallery (Phase 15).

Adds ``media_favorites``. No new permission tokens: starring needs
``media:read`` (favorites are private to their owner, like albums) and the
folder operations reuse ``media:write``.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_favorites",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["item_id"], ["media_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "item_id"),
    )
    with op.batch_alter_table("media_favorites", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_media_favorites_item_id"), ["item_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("media_favorites", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_media_favorites_item_id"))
    op.drop_table("media_favorites")
