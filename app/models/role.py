"""Roles group permissions. System roles (admin, operator, viewer) are seeded by migration 0002."""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKeyConstraint, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # System roles are seeded by migrations and cannot be renamed or deleted.
    system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    users: Mapped[list[User]] = relationship(
        secondary="user_roles", back_populates="roles", lazy="selectin"
    )
    groups: Mapped[list[Group]] = relationship(
        secondary="group_roles", back_populates="roles", lazy="selectin"
    )
    permissions: Mapped[list[RolePermission]] = relationship(
        back_populates="role",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )


class RolePermission(Base):
    """One permission token granted to a role.

    Tokens are validated against ``app/core/permissions.py``; unknown tokens
    are rejected at write time so the registry stays the single source of truth.
    """

    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    permission: Mapped[str] = mapped_column(String(64), primary_key=True)

    role: Mapped[Role] = relationship(back_populates="permissions")

    __table_args__ = (ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),)


from app.models.group import Group  # noqa: E402
from app.models.user import User  # noqa: E402
