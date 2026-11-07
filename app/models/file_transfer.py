"""Background copy and move jobs started from the Files page (Phase 16).

A transfer copies (or moves) files and folders between locations: the
configured NAS file roots and auto-mounted USB drives. It runs in the web
process's transfer pool; this row is its progress, which the page polls.
``sources`` and ``errors`` are JSON lists.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow_naive

TRANSFER_COPY = "copy"
TRANSFER_MOVE = "move"

CONFLICT_KEEP_BOTH = "keep_both"
CONFLICT_SKIP = "skip"
CONFLICT_REPLACE = "replace"

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_INTERRUPTED = "interrupted"

ACTIVE_STATES = (STATE_QUEUED, STATE_RUNNING)


class FileTransfer(Base):
    __tablename__ = "file_transfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    sources: Mapped[str] = mapped_column(Text, nullable=False)
    dest_root: Mapped[str] = mapped_column(String(1024), nullable=False)
    dest_path: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    conflict: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    total_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    done_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    total_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    done_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    skipped_files: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    current_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    errors: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow_naive, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
