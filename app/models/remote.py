"""Remote connections and sync jobs (Phase 7).

A ``RemoteConnection`` describes how to reach one remote store (SFTP, FTP,
FTPS, WebDAV, S3-compatible or Google Drive). Non-secret settings are kept
as JSON in ``config``; every secret (passwords, private keys, secret access
keys, OAuth refresh tokens) is one Fernet token in ``secrets_encrypted``.

A ``SyncJob`` pairs a directory inside a managed file root with a path on a
connection. ``SyncJobEntry`` rows are the per-file baseline of the last
successful sync (the third side of the three-way comparison), ``SyncRun``
rows are queued/executed runs, and ``SyncConflict`` rows record every
conflict a run resolved.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow_naive

PROVIDER_SFTP = "sftp"
PROVIDER_FTP = "ftp"
PROVIDER_FTPS = "ftps"
PROVIDER_WEBDAV = "webdav"
PROVIDER_S3 = "s3"
PROVIDER_GDRIVE = "gdrive"
PROVIDERS = (
    PROVIDER_SFTP,
    PROVIDER_FTP,
    PROVIDER_FTPS,
    PROVIDER_WEBDAV,
    PROVIDER_S3,
    PROVIDER_GDRIVE,
)

DIRECTION_PUSH = "push"
DIRECTION_PULL = "pull"
DIRECTION_BIDIRECTIONAL = "bidirectional"
DIRECTIONS = (DIRECTION_PUSH, DIRECTION_PULL, DIRECTION_BIDIRECTIONAL)

CONFLICT_KEEP_BOTH = "keep_both"
CONFLICT_PREFER_LOCAL = "prefer_local"
CONFLICT_PREFER_REMOTE = "prefer_remote"
CONFLICT_POLICIES = (CONFLICT_KEEP_BOTH, CONFLICT_PREFER_LOCAL, CONFLICT_PREFER_REMOTE)

DELETE_IGNORE = "ignore"
DELETE_ARCHIVE = "archive"
DELETE_POLICIES = (DELETE_IGNORE, DELETE_ARCHIVE)

RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_PARTIAL = "partial"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_ACTIVE_STATES = (RUN_QUEUED, RUN_RUNNING)

TRIGGER_MANUAL = "manual"
TRIGGER_SCHEDULE = "schedule"

CONFLICT_BOTH_MODIFIED = "both_modified"
CONFLICT_MODIFIED_VS_DELETED = "modified_vs_deleted"
CONFLICT_FIRST_SYNC_DIFFERS = "first_sync_differs"


def _load_json(value: str | None, default: Any) -> Any:
    try:
        loaded = json.loads(value) if value else default
    except ValueError:
        return default
    return loaded if isinstance(loaded, type(default)) else default


class RemoteConnection(Base):
    __tablename__ = "remote_connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    config_json: Mapped[str] = mapped_column("config", Text, nullable=False, default="{}")
    secrets_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Pending Google Drive authorization (state digest, PKCE verifier,
    # expiry), encrypted like the secrets and cleared once consumed.
    oauth_pending_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
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

    @property
    def config(self) -> dict[str, Any]:
        value: dict[str, Any] = _load_json(self.config_json, {})
        return value

    @config.setter
    def config(self, value: dict[str, Any]) -> None:
        self.config_json = json.dumps(value, sort_keys=True)


class SyncJob(Base):
    __tablename__ = "sync_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("remote_connections.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    local_root: Mapped[str] = mapped_column(String(1024), nullable=False)
    local_path: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    remote_path: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    conflict_policy: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CONFLICT_KEEP_BOTH, server_default=CONFLICT_KEEP_BOTH
    )
    delete_policy: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DELETE_IGNORE, server_default=DELETE_IGNORE
    )
    exclude_json: Mapped[str] = mapped_column(
        "exclude_patterns", Text, nullable=False, default="[]", server_default="[]"
    )
    schedule_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
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

    @property
    def exclude_patterns(self) -> list[str]:
        return [str(item) for item in _load_json(self.exclude_json, [])]

    @exclude_patterns.setter
    def exclude_patterns(self, value: list[str]) -> None:
        self.exclude_json = json.dumps(list(value))


class SyncJobEntry(Base):
    """Baseline: the state of one file on both sides after its last sync."""

    __tablename__ = "sync_job_entries"
    __table_args__ = (UniqueConstraint("job_id", "path", name="uq_sync_job_entries_job_id_path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("sync_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    local_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    local_mtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    remote_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    remote_mtime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_etag: Mapped[str | None] = mapped_column(String(256), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow_naive, server_default=func.now()
    )


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("sync_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    dry_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    requested_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    uploaded: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    downloaded: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    conflicts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    archived: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    bytes_transferred: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_json: Mapped[str] = mapped_column(
        "summary", Text, nullable=False, default="{}", server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
        nullable=False,
        default=utcnow_naive,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def summary(self) -> dict[str, Any]:
        value: dict[str, Any] = _load_json(self.summary_json, {})
        return value

    @summary.setter
    def summary(self, value: dict[str, Any]) -> None:
        self.summary_json = json.dumps(value)

    @property
    def is_active(self) -> bool:
        return self.status in RUN_ACTIVE_STATES


class SyncConflict(Base):
    __tablename__ = "sync_conflicts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("sync_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="SET NULL"), nullable=True
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    resolution: Mapped[str] = mapped_column(String(32), nullable=False)
    conflict_copy_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    acknowledged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow_naive, server_default=func.now()
    )
