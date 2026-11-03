"""File change journal (Phase 6): the source of sync cursors.

Every mutation the file manager performs appends one row in the same
transaction as its audit event. The autoincrement id is the cursor. Changes
made outside the application (Samba, the shell) are not journaled.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow_naive

CHANGE_CREATED = "created"
CHANGE_MODIFIED = "modified"
CHANGE_DELETED = "deleted"
CHANGE_RENAMED = "renamed"


class FileChange(Base):
    __tablename__ = "file_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    root: Mapped[str] = mapped_column(String(1024), index=True, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    old_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    is_dir: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
        nullable=False,
        default=utcnow_naive,
        server_default=func.now(),
    )
