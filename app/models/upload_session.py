"""Resumable upload sessions (Phase 6).

A row tracks one in-flight chunked upload: where the file will land, how big
it is declared to be and how many bytes the server has durably received.
The bytes live in a ``.nas-resumable-<id>.part`` file in the destination
directory until the client completes (or aborts, or the session expires).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow_naive

TARGET_FILES = "files"
TARGET_PUBLIC = "public"


class UploadSession(Base):
    __tablename__ = "upload_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_tokens.id", ondelete="SET NULL"), nullable=True
    )
    target: Mapped[str] = mapped_column(String(16), nullable=False)
    # Files target: the managed root and directory. Public target: the
    # uploader key (root) and "" (rel_dir).
    root: Mapped[str] = mapped_column(Text, nullable=False)
    rel_dir: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    overwrite: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow_naive, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow_naive,
        onupdate=utcnow_naive,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= utcnow_naive()
