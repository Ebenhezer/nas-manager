"""API contracts for resumable uploads (Phase 6)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.files import FileEntry
from app.schemas.public_uploads import PublicUploadResult


class UploadCreate(BaseModel):
    """Declare an upload. ``root``/``path``/``overwrite`` apply to the
    ``files`` target only; the ``public`` target is the drop folder."""

    target: Literal["files", "public"]
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0)
    root: str | None = None
    path: str = ""
    overwrite: bool = False
    sha256: str | None = Field(default=None, min_length=64, max_length=64)


class UploadStatus(BaseModel):
    id: str
    target: str
    filename: str
    offset: int
    total_bytes: int
    chunk_max_bytes: int
    created_at: datetime
    expires_at: datetime


class UploadComplete(BaseModel):
    sha256: str | None = Field(default=None, min_length=64, max_length=64)


class UploadCompleted(BaseModel):
    """Exactly one of ``file`` (files target) or ``public`` is set."""

    target: str
    file: FileEntry | None = None
    public: PublicUploadResult | None = None


class UploadCleanupResult(BaseModel):
    expired_uploads: int
    pruned_changes: int
