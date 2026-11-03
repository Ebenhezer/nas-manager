"""API contracts for remote connections and sync jobs (Phase 7).

Secret values are accepted on input (``secrets``) but never part of any
output shape: responses only say which secret fields are set.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RemoteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=16)
    config: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str | None] = Field(default_factory=dict)


class RemoteUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    # Keys merge over the stored config.
    config: dict[str, Any] | None = None
    # Missing key: keep; null or "": clear; string: replace.
    secrets: dict[str, str | None] | None = None


class RemoteOut(BaseModel):
    id: int
    name: str
    provider: str
    config: dict[str, Any]
    secrets_set: list[str]
    credentials_readable: bool
    oauth_connected: bool | None
    last_tested_at: datetime | None
    last_test_ok: bool | None
    last_test_message: str | None
    job_count: int
    created_at: datetime
    updated_at: datetime


class RemoteTestResult(BaseModel):
    ok: bool
    message: str


class RemoteBrowseEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int
    modified: datetime | None


class RemoteBrowse(BaseModel):
    path: str
    entries: list[RemoteBrowseEntry]
    rejected: int


class HostKeyProbe(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=22, ge=1, le=65535)


class HostKeyOut(BaseModel):
    host: str
    port: int
    key_type: str
    fingerprint: str
    host_key: str


class OAuthStart(BaseModel):
    authorization_url: str
    redirect_uri: str


class SyncJobCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    connection_id: int = Field(ge=1)
    local_root: str = Field(min_length=1, max_length=1024)
    local_path: str = Field(default="", max_length=4096)
    remote_path: str = Field(default="", max_length=4096)
    direction: str
    conflict_policy: str = "keep_both"
    delete_policy: str = "ignore"
    exclude_patterns: list[str] = Field(default_factory=list, max_length=50)
    schedule_minutes: int | None = Field(default=None, ge=1, le=44640)
    enabled: bool = False
    confirm: bool = False


class SyncJobUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    connection_id: int | None = Field(default=None, ge=1)
    local_root: str | None = Field(default=None, min_length=1, max_length=1024)
    local_path: str | None = Field(default=None, max_length=4096)
    remote_path: str | None = Field(default=None, max_length=4096)
    direction: str | None = None
    conflict_policy: str | None = None
    delete_policy: str | None = None
    exclude_patterns: list[str] | None = Field(default=None, max_length=50)
    schedule_minutes: int | None = Field(default=None, ge=1, le=44640)
    enabled: bool | None = None
    confirm: bool = False


class SyncJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    connection_id: int
    local_root: str
    local_path: str
    remote_path: str
    direction: str
    conflict_policy: str
    delete_policy: str
    exclude_patterns: list[str]
    schedule_minutes: int | None
    enabled: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_status: str | None
    created_at: datetime
    updated_at: datetime


class SyncRunRequest(BaseModel):
    dry_run: bool = False
    override_deletion_guard: bool = False
    confirm: bool = False


class SyncRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    status: str
    trigger: str
    dry_run: bool
    cancel_requested: bool
    uploaded: int
    downloaded: int
    conflicts: int
    archived: int
    skipped: int
    errors: int
    bytes_transferred: int
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class SyncRunDetail(SyncRunOut):
    summary: dict[str, Any]


class SyncConflictOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    run_id: int | None
    path: str
    kind: str
    resolution: str
    conflict_copy_path: str | None
    acknowledged: bool
    created_at: datetime


class RemoteSyncStatus(BaseModel):
    worker_online: bool
    worker_heartbeat_at: datetime | None
    worker_heartbeat_age_seconds: float | None
    queued_runs: int
    running_runs: int
    public_base_url_set: bool
    file_roots: list[str]
