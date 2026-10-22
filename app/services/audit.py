"""Append-only audit log. Events are written and read; rows are never mutated.

Every sensitive action in Phase 2 records an event with the acting user,
outcome, target and request metadata so the log is searchable and sufficient
to reconstruct "who did what, when, and did it succeed".
"""

from __future__ import annotations

from typing import Any

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
