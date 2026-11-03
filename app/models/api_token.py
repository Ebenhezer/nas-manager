"""Personal API tokens (Phase 6). Like sessions, only the SHA-256 of the raw
token is stored; the raw value is shown to its owner exactly once."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow_naive


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # The first characters of the raw token, shown in listings to tell tokens apart.
    token_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # JSON list of permission tokens; effective access is these scopes
    # intersected with the owner's *current* effective permissions.
    scopes_json: Mapped[str] = mapped_column("scopes", Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow_naive, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="api_tokens")

    @property
    def scopes(self) -> list[str]:
        value = json.loads(self.scopes_json or "[]")
        return [str(item) for item in value] if isinstance(value, list) else []

    @scopes.setter
    def scopes(self, value: list[str]) -> None:
        self.scopes_json = json.dumps(sorted(set(value)))

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= utcnow_naive()

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None


from app.models.user import User  # noqa: E402
