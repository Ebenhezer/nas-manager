"""Configuration import runs (Phase 12).

Every applied import records what it created, updated and deleted, and a
*pre-import snapshot*: a full configuration export of the state before the
import (secrets included), stored as one Fernet token with the credentials
key. Rolling an import back deletes what it created and re-applies the
snapshot, inside one transaction.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow_naive
from app.models.remote import _load_json

IMPORT_APPLIED = "applied"
IMPORT_ROLLED_BACK = "rolled_back"


class ConfigImportRun(Base):
    __tablename__ = "config_import_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    source_name: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    exported_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sections_json: Mapped[str] = mapped_column(
        "sections", Text, nullable=False, default="{}", server_default="{}"
    )
    warnings_json: Mapped[str] = mapped_column(
        "warnings", Text, nullable=False, default="[]", server_default="[]"
    )
    snapshot_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    database_copy: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_username: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
        nullable=False,
        default=utcnow_naive,
        server_default=func.now(),
    )
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rolled_back_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    @property
    def sections(self) -> dict[str, Any]:
        value: dict[str, Any] = _load_json(self.sections_json, {})
        return value

    @sections.setter
    def sections(self, value: dict[str, Any]) -> None:
        self.sections_json = json.dumps(value)

    @property
    def warnings(self) -> list[str]:
        return [str(item) for item in _load_json(self.warnings_json, [])]

    @warnings.setter
    def warnings(self, value: list[str]) -> None:
        self.warnings_json = json.dumps(list(value))
