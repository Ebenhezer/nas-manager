"""Append-only audit log. Events are written and read; rows are never mutated.

Every sensitive action in Phase 2 records an event with the acting user,
outcome, target and request metadata so the log is searchable and sufficient
to reconstruct "who did what, when, and did it succeed".
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Any, TypedDict

from sqlalchemy import Select, String, cast, event, func, or_, select
from sqlalchemy.orm import Session

from app.core.logging import request_id_var
from app.models import AuditEvent, User

# Committed events are also written to the log stream (file, journald,
# console) on this logger. Remote syslog forwarding of audit events does not
# use it: the worker forwards from the table so nothing is lost when a
# collector is down (Phase 13).
audit_logger = logging.getLogger("app.audit")

CATEGORY_AUTH = "auth"
CATEGORY_USER = "user"
CATEGORY_GROUP = "group"
CATEGORY_ROLE = "role"
CATEGORY_SYSTEM = "system"
CATEGORY_SETTINGS = "settings"
CATEGORY_FILE = "file"
CATEGORY_SAMBA = "samba"
CATEGORY_PUBLIC_UPLOAD = "public_upload"
CATEGORY_DISK = "disk"
CATEGORY_RAID = "raid"
CATEGORY_API_TOKEN = "api_token"  # noqa: S105
CATEGORY_UPLOAD = "upload"
CATEGORY_REMOTE_SYNC = "remote_sync"
CATEGORY_MEDIA = "media"
CATEGORY_MONITORING = "monitoring"
CATEGORY_BACKUP = "backup"
CATEGORY_CONFIG = "config"
CATEGORY_AUDIT = "audit"
CATEGORY_SYSLOG = "syslog"

CATEGORIES: tuple[str, ...] = tuple(
    sorted(value for name, value in dict(globals()).items() if name.startswith("CATEGORY_"))
)

OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"

# auth events
AUTH_LOGIN_SUCCESS = "auth.login.success"
AUTH_LOGIN_FAILURE = "auth.login.failure"
AUTH_LOGIN_LOCKED = "auth.login.locked"
AUTH_LOGIN_RATE_LIMITED = "auth.login.rate_limited"
AUTH_LOGOUT = "auth.logout"
AUTH_PERMISSION_DENIED = "auth.permission_denied"

# user events
USER_CREATED = "user.created"
USER_UPDATED = "user.updated"
USER_DELETED = "user.deleted"
# The event name contains the word "password"; ruff's S105 heuristic must not
# treat the constant as a credential.
USER_PASSWORD_CHANGED_SELF = "user.password_changed_self"  # noqa: S105
USER_PASSWORD_RESET = "user.password_reset"  # noqa: S105
USER_ACTIVE_STATE_CHANGED = "user.active_state_changed"
USER_UNLOCKED = "user.unlocked"
USER_BOOTSTRAPPED = "user.bootstrapped"

# group events
GROUP_CREATED = "group.created"
GROUP_UPDATED = "group.updated"
GROUP_DELETED = "group.deleted"
GROUP_MEMBERS_CHANGED = "group.members_changed"
GROUP_ROLES_CHANGED = "group.roles_changed"

# role events
ROLE_CREATED = "role.created"
ROLE_UPDATED = "role.updated"
ROLE_DELETED = "role.deleted"
ROLE_PERMISSIONS_CHANGED = "role.permissions_changed"
USER_ROLES_CHANGED = "user.roles_changed"

# settings events
SETTINGS_UPDATED = "settings.updated"

# file events (Phase 3)
FILE_UPLOADED = "file.uploaded"
FILE_DOWNLOADED = "file.downloaded"
FILE_DELETED = "file.deleted"
FILE_RENAMED = "file.renamed"
FILE_CREATED_DIR = "file.created_dir"

# samba events (Phase 3)
SAMBA_SHARE_CREATED = "samba.share.created"
SAMBA_SHARE_UPDATED = "samba.share.updated"
SAMBA_SHARE_DELETED = "samba.share.deleted"
SAMBA_CONFIG_APPLIED = "samba.config.applied"

# public upload events (Phase 4)
PUBLIC_UPLOAD_UPLOADED = "public_upload.uploaded"
PUBLIC_UPLOAD_REJECTED = "public_upload.rejected"
PUBLIC_UPLOAD_RATE_LIMITED = "public_upload.rate_limited"
PUBLIC_UPLOAD_QUOTA_CHANGED = "public_upload.quota_changed"
PUBLIC_UPLOAD_FILE_DELETED = "public_upload.file_deleted"
PUBLIC_UPLOAD_CLEANUP = "public_upload.cleanup"
PUBLIC_UPLOAD_TOGGLE_CHANGED = "public_upload.toggle_changed"

# disk events (Phase 5)
DISK_MOUNTED = "disk.mounted"
DISK_UNMOUNTED = "disk.unmounted"
DISK_ACTION_FAILED = "disk.action_failed"
DISK_HELPER_UNAVAILABLE = "disk.helper_unavailable"

# raid events (Phase 5)
RAID_SCRUB_STARTED = "raid.scrub_started"
RAID_ACTION_FAILED = "raid.action_failed"

# api token events (Phase 6); the names contain "token", which ruff's S105
# heuristic would otherwise flag as credentials.
API_TOKEN_CREATED = "api_token.created"  # noqa: S105
API_TOKEN_REVOKED = "api_token.revoked"  # noqa: S105
API_TOKEN_AUTH_FAILED = "api_token.auth_failed"  # noqa: S105

# resumable upload events (Phase 6)
UPLOAD_SESSION_CREATED = "upload.session_created"
UPLOAD_SESSION_COMPLETED = "upload.session_completed"
UPLOAD_SESSION_ABORTED = "upload.session_aborted"
UPLOAD_SESSION_EXPIRED = "upload.session_expired"

# remote connection and sync events (Phase 7)
REMOTE_CREATED = "remote.created"
REMOTE_UPDATED = "remote.updated"
REMOTE_DELETED = "remote.deleted"
REMOTE_TESTED = "remote.tested"
REMOTE_OAUTH_STARTED = "remote.oauth_started"
REMOTE_OAUTH_CONNECTED = "remote.oauth_connected"
REMOTE_OAUTH_FAILED = "remote.oauth_failed"
SYNC_JOB_CREATED = "sync_job.created"
SYNC_JOB_UPDATED = "sync_job.updated"
SYNC_JOB_DELETED = "sync_job.deleted"
SYNC_RUN_QUEUED = "sync_run.queued"
SYNC_RUN_CANCEL_REQUESTED = "sync_run.cancel_requested"
SYNC_RUN_FINISHED = "sync_run.finished"
SYNC_RUN_FAILED = "sync_run.failed"
SYNC_CONFLICT = "sync.conflict"
SYNC_CONFLICT_ACKNOWLEDGED = "sync.conflict_acknowledged"
SYNC_ARCHIVED = "sync.archived"
SYNC_DELETION_GUARD_TRIPPED = "sync.deletion_guard_tripped"

# Photo gallery and media libraries (Phase 8)
MEDIA_LIBRARY_CREATED = "media.library_created"
MEDIA_LIBRARY_UPDATED = "media.library_updated"
MEDIA_LIBRARY_DELETED = "media.library_deleted"
MEDIA_LIBRARY_GROUPS_CHANGED = "media.library_groups_changed"
MEDIA_SCAN_QUEUED = "media.scan_queued"
MEDIA_ITEM_UPLOADED = "media.item_uploaded"
MEDIA_ITEM_DOWNLOADED = "media.item_downloaded"
MEDIA_ITEM_DELETED = "media.item_deleted"
MEDIA_ALBUM_CREATED = "media.album_created"
MEDIA_ALBUM_UPDATED = "media.album_updated"
MEDIA_ALBUM_DELETED = "media.album_deleted"
MEDIA_ALBUM_ITEMS_CHANGED = "media.album_items_changed"
# Video streaming (Phase 9)
MEDIA_TRANSCODE_REQUESTED = "media.transcode_requested"
MEDIA_TRANSCODE_FAILED = "media.transcode_failed"
# Explorer-style gallery (Phase 15)
MEDIA_ITEMS_DOWNLOADED = "media.items_downloaded"
MEDIA_ITEMS_MOVED = "media.items_moved"
MEDIA_ITEM_RENAMED = "media.item_renamed"
MEDIA_FOLDER_CREATED = "media.folder_created"
MEDIA_FOLDER_RENAMED = "media.folder_renamed"
MEDIA_FOLDER_DELETED = "media.folder_deleted"

# Monitoring and alerting (Phase 10)
ALERT_RULE_CREATED = "alert_rule.created"
ALERT_RULE_UPDATED = "alert_rule.updated"
ALERT_RULE_DELETED = "alert_rule.deleted"
ALERT_CHANNEL_CREATED = "alert_channel.created"
ALERT_CHANNEL_UPDATED = "alert_channel.updated"
ALERT_CHANNEL_DELETED = "alert_channel.deleted"
ALERT_CHANNEL_TESTED = "alert_channel.tested"
ALERT_ACKNOWLEDGED = "alert.acknowledged"
ALERT_NOTIFICATION_FAILED = "alert.notification_failed"

# Backup and restore (Phase 11)
BACKUP_REPOSITORY_CREATED = "backup_repository.created"
BACKUP_REPOSITORY_UPDATED = "backup_repository.updated"
BACKUP_REPOSITORY_DELETED = "backup_repository.deleted"
BACKUP_REPOSITORY_INITIALIZED = "backup_repository.initialized"
BACKUP_JOB_CREATED = "backup_job.created"
BACKUP_JOB_UPDATED = "backup_job.updated"
BACKUP_JOB_DELETED = "backup_job.deleted"
BACKUP_RUN_QUEUED = "backup_run.queued"
BACKUP_RUN_CANCEL_REQUESTED = "backup_run.cancel_requested"
BACKUP_RUN_FINISHED = "backup_run.finished"
BACKUP_RUN_FAILED = "backup_run.failed"
BACKUP_SNAPSHOT_DELETED = "backup.snapshot_deleted"
BACKUP_VERIFY_FAILED = "backup.verify_failed"
BACKUP_RESTORE_REPLACED = "backup.restore_replaced"
BACKUP_SYSTEM_RESTORED = "backup.system_restored"

# Configuration export and import (Phase 12)
CONFIG_EXPORTED = "config.exported"
CONFIG_IMPORT_PREVIEWED = "config.import_previewed"
CONFIG_IMPORTED = "config.imported"
CONFIG_IMPORT_REFUSED = "config.import_refused"
CONFIG_ROLLED_BACK = "config.rolled_back"

# Process lifecycle (Phase 13)
SYSTEM_STARTED = "system.started"
SYSTEM_STOPPED = "system.stopped"

# Auditing and syslog (Phase 13)
AUDIT_EXPORTED = "audit.exported"
AUDIT_VERIFIED = "audit.verified"
AUDIT_CHAIN_BROKEN = "audit.chain_broken"
AUDIT_PRUNED = "audit.pruned"
SYSLOG_DESTINATION_CREATED = "syslog.destination_created"
SYSLOG_DESTINATION_UPDATED = "syslog.destination_updated"
SYSLOG_DESTINATION_DELETED = "syslog.destination_deleted"
SYSLOG_DESTINATION_TESTED = "syslog.destination_tested"
SYSLOG_FORWARD_FAILED = "syslog.forward_failed"
SYSLOG_FORWARD_RECOVERED = "syslog.forward_recovered"


class RequestContext(TypedDict):
    ip_address: str | None
    user_agent: str | None
    request_id: str | None


def request_context(context: dict[str, str | None] | None) -> RequestContext:
    """Typed request metadata for ``record_audit_event(**...)``."""
    source = context or {}
    return {
        "ip_address": source.get("ip_address"),
        "user_agent": source.get("user_agent"),
        "request_id": source.get("request_id"),
    }


def record_audit_event(
    session: Session,
    *,
    event: str,
    category: str,
    outcome: str = OUTCOME_SUCCESS,
    actor: User | None = None,
    actor_username: str | None = None,
    target_type: str | None = None,
    target_id: str | int | None = None,
    details: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    commit: bool = True,
) -> AuditEvent:
    """Append one audit event and commit it (the default)."""
    if actor is not None:
        actor_username = actor.username
    request_id = request_id or request_id_var.get()
    row = AuditEvent(
        actor_user_id=actor.id if actor is not None else None,
        actor_username=actor_username or "",
        event=event,
        category=category,
        outcome=outcome,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        details=details,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
    )
    session.add(row)
    session.info.setdefault(_PENDING_KEY, []).append(row)
    if commit:
        session.commit()
    return row


# ---------------------------------------------------------------------------
# Log stream emission. An event reaches the logs only once its transaction
# commits: ids are captured after the flush, the lines are written after the
# commit, and a rollback discards them, so a refused or rolled-back action
# never shows up in journald or the log file.
# ---------------------------------------------------------------------------

_PENDING_KEY = "nas_pending_audit"
_FLUSHED_KEY = "nas_flushed_audit"


def _snapshot(row: AuditEvent) -> dict[str, Any]:
    target = f"{row.target_type}:{row.target_id}" if row.target_type else None
    return {
        "audit_id": row.id,
        "audit_event": row.event,
        "audit_category": row.category,
        "audit_outcome": row.outcome,
        "audit_actor": row.actor_username or None,
        "audit_target": target,
        "audit_ip": row.ip_address,
        "request_id": row.request_id,
    }


@event.listens_for(Session, "after_flush")
def _capture_flushed(session: Session, _flush_context: object) -> None:
    pending: list[AuditEvent] = session.info.get(_PENDING_KEY) or []
    if not pending:
        return
    flushed = session.info.setdefault(_FLUSHED_KEY, [])
    remaining: list[AuditEvent] = []
    for row in pending:
        if row.id is None:
            remaining.append(row)
        else:
            flushed.append(_snapshot(row))
    session.info[_PENDING_KEY] = remaining


@event.listens_for(Session, "after_commit")
def _emit_committed(session: Session) -> None:
    flushed: list[dict[str, Any]] = session.info.pop(_FLUSHED_KEY, None) or []
    for item in flushed:
        level = logging.INFO if item["audit_outcome"] == OUTCOME_SUCCESS else logging.WARNING
        actor = item["audit_actor"] or "-"
        audit_logger.log(
            level,
            "audit %s %s by %s",
            item["audit_event"],
            item["audit_outcome"],
            actor,
            extra={key: value for key, value in item.items() if value is not None},
        )


@event.listens_for(Session, "after_rollback")
def _discard_rolled_back(session: Session) -> None:
    session.info.pop(_PENDING_KEY, None)
    session.info.pop(_FLUSHED_KEY, None)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditFilters:
    """Search criteria shared by the list, the count and the export."""

    category: str | None = None
    event: str | None = None
    outcome: str | None = None
    actor_username: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    request_id: str | None = None
    ip_address: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    q: str | None = None

    def as_details(self) -> dict[str, Any]:
        """The filters that are set, for audit details of an export."""
        result: dict[str, Any] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if value not in (None, ""):
                result[item.name] = value.isoformat() if isinstance(value, datetime) else value
        return result


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def _like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _apply_filters[S: Select[Any]](stmt: S, filters: AuditFilters) -> S:
    exact = {
        AuditEvent.category: filters.category,
        AuditEvent.event: filters.event,
        AuditEvent.outcome: filters.outcome,
        AuditEvent.actor_username: filters.actor_username,
        AuditEvent.target_type: filters.target_type,
        AuditEvent.target_id: filters.target_id,
        AuditEvent.request_id: filters.request_id,
        AuditEvent.ip_address: filters.ip_address,
    }
    for column, value in exact.items():
        if value:
            stmt = stmt.where(column == value)
    if filters.since is not None:
        stmt = stmt.where(AuditEvent.created_at >= _naive_utc(filters.since))
    if filters.until is not None:
        stmt = stmt.where(AuditEvent.created_at < _naive_utc(filters.until))
    if filters.q and filters.q.strip():
        pattern = _like_pattern(filters.q.strip())
        stmt = stmt.where(
            or_(
                *(
                    column.ilike(pattern, escape="\\")
                    for column in (
                        AuditEvent.event,
                        AuditEvent.actor_username,
                        AuditEvent.target_type,
                        AuditEvent.target_id,
                        AuditEvent.ip_address,
                        AuditEvent.request_id,
                        AuditEvent.user_agent,
                        cast(AuditEvent.details, String),
                    )
                )
            )
        )
    return stmt


def query_audit_events(
    session: Session,
    *,
    limit: int = 100,
    offset: int = 0,
    category: str | None = None,
    event: str | None = None,
    outcome: str | None = None,
    actor_username: str | None = None,
    filters: AuditFilters | None = None,
) -> tuple[list[AuditEvent], int]:
    """Return ``(events, total)`` with the newest events first and ``total``
    reflecting the filters before pagination (unbounded)."""
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    if filters is None:
        filters = AuditFilters(
            category=category, event=event, outcome=outcome, actor_username=actor_username
        )
    stmt = _apply_filters(select(AuditEvent), filters)
    total_stmt = _apply_filters(select(func.count(AuditEvent.id)), filters)
    total = int(session.execute(total_stmt).scalar_one())
    rows = (
        session.execute(
            stmt.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return list(rows), total


def iter_audit_events(
    session: Session, filters: AuditFilters, *, max_rows: int, chunk: int = 1000
) -> Iterator[AuditEvent]:
    """Yield matching events newest first (by id), at most ``max_rows``,
    reading in keyset-paginated chunks so an export never loads everything."""
    emitted = 0
    before: int | None = None
    while emitted < max_rows:
        stmt = _apply_filters(select(AuditEvent), filters)
        if before is not None:
            stmt = stmt.where(AuditEvent.id < before)
        size = min(chunk, max_rows - emitted)
        rows = list(session.execute(stmt.order_by(AuditEvent.id.desc()).limit(size)).scalars())
        if not rows:
            return
        for row in rows:
            yield row
            session.expunge(row)
        emitted += len(rows)
        before = rows[-1].id


def audit_facets(session: Session) -> dict[str, list[str]]:
    """Known categories plus the event names that occur in the table."""
    present = session.execute(select(AuditEvent.category).distinct()).scalars()
    events = session.execute(select(AuditEvent.event).distinct().order_by(AuditEvent.event))
    return {
        "categories": sorted(set(CATEGORIES) | {str(value) for value in present}),
        "events": [str(value) for value in events.scalars()],
    }
