"""Groups organise users and carry roles; Phase 3 uses them for share permissions."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow_naive


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
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

    members: Mapped[list[User]] = relationship(
        secondary="user_groups", back_populates="groups", lazy="selectin"
    )
    roles: Mapped[list[Role]] = relationship(
        secondary="group_roles", back_populates="groups", lazy="selectin"
    )


from app.models.role import Role  # noqa: E402
from app.models.user import User  # noqa: E402
