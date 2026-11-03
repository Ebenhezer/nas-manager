"""Backup and restore API schemas (Phase 11). Passphrases are write-only."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BackupRepositoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    target_kind: str
    local_root: str | None = Field(default=None, max_length=1024)
    connection_id: int | None = Field(default=None, ge=1)
    repo_path: str = Field(default="", max_length=4096)
    passphrase: str = Field(min_length=12, max_length=1024)


class BackupRepositoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    target_kind: str | None = None
    local_root: str | None = Field(default=None, max_length=1024)
    connection_id: int | None = Field(default=None, ge=1)
    repo_path: str | None = Field(default=None, max_length=4096)
    passphrase: str | None = Field(default=None, min_length=12, max_length=1024)


class BackupRepositoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    target_kind: str
    local_root: str | None
    connection_id: int | None
    repo_path: str
    initialized_at: datetime | None
    last_verified_at: datetime | None
    last_verify_status: str | None
    created_at: datetime
    updated_at: datetime


class BackupRepositoryInitResult(BaseModel):
    repository: BackupRepositoryOut
    created: bool
    snapshots: int


class BackupJobCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    repository_id: int = Field(ge=1)
    source_kind: str
    source_root: str | None = Field(default=None, max_length=1024)
    source_path: str = Field(default="", max_length=4096)
    source_connection_id: int | None = Field(default=None, ge=1)
    exclude_patterns: list[str] = Field(default_factory=list, max_length=50)
    schedule_minutes: int | None = Field(default=None, ge=1, le=44640)
    enabled: bool = False
    keep_last: int = Field(default=7, ge=0, le=1000)
    keep_daily: int = Field(default=7, ge=0, le=1000)
    keep_weekly: int = Field(default=4, ge=0, le=1000)
    keep_monthly: int = Field(default=6, ge=0, le=1000)
    confirm: bool = False


class BackupJobUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    repository_id: int | None = Field(default=None, ge=1)
    source_kind: str | None = None
    source_root: str | None = Field(default=None, max_length=1024)
    source_path: str | None = Field(default=None, max_length=4096)
    source_connection_id: int | None = Field(default=None, ge=1)
    exclude_patterns: list[str] | None = Field(default=None, max_length=50)
    schedule_minutes: int | None = Field(default=None, ge=1, le=44640)
    enabled: bool | None = None
    keep_last: int | None = Field(default=None, ge=0, le=1000)
    keep_daily: int | None = Field(default=None, ge=0, le=1000)
    keep_weekly: int | None = Field(default=None, ge=0, le=1000)
    keep_monthly: int | None = Field(default=None, ge=0, le=1000)
    confirm: bool = False


class BackupJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    repository_id: int
    direction: str
    source_kind: str
    source_root: str | None
    source_path: str
    source_connection_id: int | None
    exclude_patterns: list[str]
    schedule_minutes: int | None
    enabled: bool
    keep_last: int
    keep_daily: int
    keep_weekly: int
    keep_monthly: int
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_status: str | None
    created_at: datetime
    updated_at: datetime


class BackupVerifyRequest(BaseModel):
    full: bool = True
    snapshot_id: int | None = Field(default=None, ge=1)


class BackupPruneRequest(BaseModel):
    dry_run: bool = True
    confirm: bool = False


class RestoreTarget(BaseModel):
    kind: str
    root: str | None = Field(default=None, max_length=1024)
    path: str = Field(default="", max_length=4096)
    connection_id: int | None = Field(default=None, ge=1)


class BackupRestoreRequest(BaseModel):
    target: RestoreTarget
    paths: list[str] = Field(default_factory=list, max_length=1000)
    overwrite: bool = False
    dry_run: bool = False
    confirm: bool = False


class BackupRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repository_id: int
    job_id: int | None
    kind: str
    status: str
    trigger: str
    dry_run: bool
    cancel_requested: bool
    files: int
    new_blobs: int
    reused: int
    skipped: int
    errors: int
    bytes_processed: int
    bytes_uploaded: int
    snapshot_id: str | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class BackupRunDetail(BackupRunOut):
    params: dict[str, Any]
    summary: dict[str, Any]


class BackupSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repository_id: int
    snapshot_id: str
    job_id: int | None
    job_name: str | None
    source_kind: str
    source_label: str
    taken_at: datetime
    file_count: int
    total_bytes: int
    verified_at: datetime | None
    verify_status: str | None
    verify_error: str | None


class BackupSnapshotEntry(BaseModel):
    path: str
    size: int
    mtime_ns: int | None


class BackupSnapshotEntries(BaseModel):
    snapshot: BackupSnapshotOut
    prefix: str
    total: int
    truncated: bool
    entries: list[BackupSnapshotEntry]


class BackupStatus(BaseModel):
    worker_online: bool
    worker_heartbeat_at: datetime | None
    queued_runs: int
    running_runs: int
    local_roots: list[str]
    file_roots: list[str]
    staging_dir: str
