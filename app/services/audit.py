"""Append-only audit log. Events are written and read; rows are never mutated.

Every sensitive action in Phase 2 records an event with the acting user,
outcome, target and request metadata so the log is searchable and sufficient
to reconstruct "who did what, when, and did it succeed".
"""

from __future__ import annotations

from typing import Any, TypedDict

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.logging import request_id_var
from app.models import AuditEvent, User

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

OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"

# auth events
AUTH_LOGIN_SUCCESS = "auth.login.success"
AUTH_LOGIN_FAILURE = "auth.login.failure"
AUTH_LOGIN_LOCKED = "auth.login.locked"
AUTH_LOGIN_RATE_LIMITED = "auth.login.rate_limited"
AUTH_LOGOUT = "auth.logout"

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
    if commit:
        session.commit()
    return row


def query_audit_events(
    session: Session,
    *,
    limit: int = 100,
    offset: int = 0,
    category: str | None = None,
    event: str | None = None,
    outcome: str | None = None,
    actor_username: str | None = None,
) -> tuple[list[AuditEvent], int]:
    """Return ``(events, total)`` with the newest events first and ``total``
    reflecting the filters before pagination (unbounded)."""
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    stmt = select(AuditEvent)
    total_stmt: Select[tuple[int]] = select(func.count(AuditEvent.id))
    if category:
        stmt = stmt.where(AuditEvent.category == category)
        total_stmt = total_stmt.where(AuditEvent.category == category)
    if event:
        stmt = stmt.where(AuditEvent.event == event)
        total_stmt = total_stmt.where(AuditEvent.event == event)
    if outcome:
        stmt = stmt.where(AuditEvent.outcome == outcome)
        total_stmt = total_stmt.where(AuditEvent.outcome == outcome)
    if actor_username:
        stmt = stmt.where(AuditEvent.actor_username == actor_username)
        total_stmt = total_stmt.where(AuditEvent.actor_username == actor_username)
    total_value = session.execute(total_stmt).scalar_one()
    total: int = total_value[0] if isinstance(total_value, tuple) else int(total_value)
    rows = (
        session.execute(
            stmt.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)
