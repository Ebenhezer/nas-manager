"""API contracts for the public upload folder and quotas (Phase 4)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

MAX_FILENAME_LENGTH = 255


class PublicUploadPolicy(BaseModel):
    """What an anonymous client may learn before uploading (no file data)."""

    enabled: bool
    max_file_bytes: int
    filename_max_length: int = MAX_FILENAME_LENGTH


class PublicUploadResult(BaseModel):
    """Metadata of one accepted public upload. The response tells the client
    the final stored name, which may differ from the submitted one because
    colliding names are auto-uniquified (the folder never overwrites)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    uploader_key: str
    filename: str
    size_bytes: int
    created_at: datetime


class UploaderSummary(BaseModel):
    uploader_key: str
    quota_bytes: int
    used_bytes: int
    file_count: int
    over_quota: bool


class PublicUploadStatus(BaseModel):
    enabled: bool
    dir: str
    max_file_bytes: int
    default_quota_bytes: int
    total_uploaders: int
    total_files: int
    total_used_bytes: int
    uploaders: list[UploaderSummary]
    limit: int
    offset: int


class PublicUploadFileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    uploader_key: str
    filename: str
    size_bytes: int
    ip_address: str | None
    created_at: datetime


class PublicUploadFilesPage(BaseModel):
    files: list[PublicUploadFileOut]
    total: int
    limit: int
    offset: int


class QuotaUpdateRequest(BaseModel):
    quota_bytes: int = Field(ge=0, le=2**63 - 1)


class CleanupRequest(BaseModel):
    older_than_days: int | None = Field(default=None, ge=1, le=3650)


class CleanupResult(BaseModel):
    removed_temp_files: int
    removed_files: int
    removed_bytes: int
    reconciled_uploaders: int
    message: str
