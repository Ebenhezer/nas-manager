"""Public drop folder accounting (Phase 4).

The folder is upload-only: ``public_upload_files`` records every accepted file
(the ledger for admin operations and usage recomputation) and
``public_upload_quotas`` stores the per-uploader limits and the reserved
usage. ``used_bytes`` is incremented/decremented inside the same database
transaction that creates or deletes the file row, so the accounting cannot
diverge from what the API answers.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow_naive


class PublicUploadQuota(Base):
    """Per-uploader quota limit and reserved usage."""

    __tablename__ = "public_upload_quotas"

    # e.g. "user:alice" (authenticated session) or "ip:192.0.2.10" (anonymous).
    uploader_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    quota_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow_naive, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow_naive,
        onupdate=utcnow_naive,
        server_default=func.now(),
        index=True,
    )

    @property
    def is_over_quota(self) -> bool:
        return self.used_bytes > self.quota_bytes


class PublicUploadFile(Base):
    """One accepted file in the public drop folder."""

    __tablename__ = "public_upload_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uploader_key: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    # Final sanitized basename stored under <dir>/<sha256(key)[:24]>/<filename>.
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow_naive,
        server_default=func.now(),
        index=True,
    )
