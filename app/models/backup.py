"""Backup repositories, jobs, runs and the snapshot catalogue (Phase 11).

A ``BackupRepository`` is an encrypted, deduplicated store on a target: a
directory under one of ``NAS_BACKUP_LOCAL_ROOTS`` (external storage) or a
path on a ``RemoteConnection``. Its passphrase is one Fernet token in
``passphrase_encrypted``. The repository on the target is self-describing
(``repo.json``, encrypted snapshot manifests and content blobs); the rows
here are a catalogue and a cache that a verify run can rebuild.

A ``BackupJob`` backs one source up into one repository: a folder inside a
file root, the personal media libraries, the system state (database,
credentials key, Samba configuration) or, for *pull* backups, a folder on a
remote connection. ``BackupRun`` rows are queued/executed backup, verify,
prune and restore runs; ``BackupSnapshot`` rows list the snapshots in each
repository and ``BackupBlob`` rows cache which content blobs exist.
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
from app.models.remote import _load_json

TARGET_LOCAL = "local"
TARGET_REMOTE = "remote"
TARGET_KINDS = (TARGET_LOCAL, TARGET_REMOTE)

SOURCE_FILES = "files"
SOURCE_MEDIA = "media"
SOURCE_SYSTEM = "system"
SOURCE_REMOTE = "remote"
SOURCE_KINDS = (SOURCE_FILES, SOURCE_MEDIA, SOURCE_SYSTEM, SOURCE_REMOTE)

DIRECTION_PUSH = "push"
DIRECTION_PULL = "pull"

RUN_BACKUP = "backup"
RUN_VERIFY = "verify"
RUN_PRUNE = "prune"
RUN_RESTORE = "restore"
RUN_KINDS = (RUN_BACKUP, RUN_VERIFY, RUN_PRUNE, RUN_RESTORE)

VERIFY_OK = "ok"
VERIFY_FAILED = "failed"


class BackupRepository(Base):
    __tablename__ = "backup_repositories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    connection_id: Mapped[int | None] = mapped_column(
        ForeignKey("remote_connections.id", ondelete="RESTRICT"), index=True, nullable=True
    )
    local_root: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    repo_path: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    passphrase_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    initialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_verify_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
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


class BackupJob(Base):
    __tablename__ = "backup_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("backup_repositories.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_root: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_path: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    source_connection_id: Mapped[int | None] = mapped_column(
        ForeignKey("remote_connections.id", ondelete="RESTRICT"), index=True, nullable=True
    )
    exclude_json: Mapped[str] = mapped_column(
        "exclude_patterns", Text, nullable=False, default="[]", server_default="[]"
    )
    schedule_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    # Retention (all zero keeps every snapshot of this job).
    keep_last: Mapped[int] = mapped_column(Integer, nullable=False, default=7, server_default="7")
    keep_daily: Mapped[int] = mapped_column(Integer, nullable=False, default=7, server_default="7")
    keep_weekly: Mapped[int] = mapped_column(Integer, nullable=False, default=4, server_default="4")
    keep_monthly: Mapped[int] = mapped_column(
        Integer, nullable=False, default=6, server_default="6"
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

    @property
    def direction(self) -> str:
        return DIRECTION_PULL if self.source_kind == SOURCE_REMOTE else DIRECTION_PUSH

    @property
    def retention_enabled(self) -> bool:
        return any((self.keep_last, self.keep_daily, self.keep_weekly, self.keep_monthly))


class BackupRun(Base):
    __tablename__ = "backup_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("backup_repositories.id", ondelete="CASCADE"), index=True, nullable=False
    )
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("backup_jobs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
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
    files: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    new_blobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reused: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    bytes_processed: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    bytes_uploaded: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    params_json: Mapped[str] = mapped_column(
        "params", Text, nullable=False, default="{}", server_default="{}"
    )
    summary_json: Mapped[str] = mapped_column(
        "summary", Text, nullable=False, default="{}", server_default="{}"
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    def params(self) -> dict[str, Any]:
        value: dict[str, Any] = _load_json(self.params_json, {})
        return value

    @params.setter
    def params(self, value: dict[str, Any]) -> None:
        self.params_json = json.dumps(value)

    @property
    def summary(self) -> dict[str, Any]:
        value: dict[str, Any] = _load_json(self.summary_json, {})
        return value

    @summary.setter
    def summary(self, value: dict[str, Any]) -> None:
        self.summary_json = json.dumps(value)

    @property
    def is_active(self) -> bool:
        return self.status in ("queued", "running")


class BackupSnapshot(Base):
    __tablename__ = "backup_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "repository_id", "snapshot_id", name="uq_backup_snapshots_repository_id_snapshot_id"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("backup_repositories.id", ondelete="CASCADE"), index=True, nullable=False
    )
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("backup_jobs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    job_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_label: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verify_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    verify_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class BackupBlob(Base):
    """Cache of the content blobs known to exist in a repository."""

    __tablename__ = "backup_blobs"

    repository_id: Mapped[int] = mapped_column(
        ForeignKey("backup_repositories.id", ondelete="CASCADE"), primary_key=True
    )
    blob: Mapped[str] = mapped_column(String(64), primary_key=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
