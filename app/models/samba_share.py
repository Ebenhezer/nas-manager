"""Samba share definitions managed by the application.

Shares live in the database as the source of truth; the Samba service
renders them into the managed section of ``smb.conf`` (see
``app/services/samba.py``). Secrets are never stored here.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class SambaShare(Base):
    __tablename__ = "samba_shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # Absolute directory to share. Must exist and be absolute (validated).
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    read_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    browseable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    guest_ok: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    # Free-form Samba access lists, e.g. "alice bob" or "@staff". Passed
    # through verbatim; validated to contain no newlines to prevent config
    # injection into the generated stanza.
    valid_users: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    write_list: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )
