"""API contracts for sync cursors and client capability discovery (Phase 6)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SyncCursor(BaseModel):
    cursor: str


class FileChangeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    path: str
    old_path: str | None
    is_dir: bool
    size_bytes: int | None
    created_at: datetime


class SyncChanges(BaseModel):
    """``reset`` means the cursor is too old (or foreign): re-list the root
    and continue from ``cursor``."""

    root: str
    cursor: str
    has_more: bool
    reset: bool
    changes: list[FileChangeOut]


class ServerLimits(BaseModel):
    max_upload_bytes: int
    resumable_chunk_max_bytes: int
    resumable_upload_ttl_hours: int
    sync_change_retention_days: int


class ServerInfo(BaseModel):
    """Public discovery document for API clients; contains nothing sensitive."""

    name: str
    version: str
    api_version: str
    features: list[str]
    auth_schemes: list[str]
    limits: ServerLimits
