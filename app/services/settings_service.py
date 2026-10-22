"""Runtime key/value settings stored in the database (non-secret values only)."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models import User
from app.models.app_setting import AppSetting
from app.services import audit

INSTANCE_ID_KEY = "instance_id"

# Runtime policy switches. Values are "1" / "0".
# When on, users holding the built-in `operator` role additionally see the
# File manager and Samba pages read-only (files:read + samba:read). Writes
# always require explicit grants and are unaffected.
OPERATOR_VIEW_FILES_SAMBA_KEY = "operator_view_files_samba"


def get_setting(session: Session, key: str) -> str | None:
    row = session.get(AppSetting, key)
    return None if row is None else row.value


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    session.commit()


def get_bool_setting(session: Session, key: str, *, default: bool = False) -> bool:
    value = get_setting(session, key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def set_bool_setting(session: Session, key: str, value: bool) -> None:
    set_setting(session, key, "1" if value else "0")


def operator_files_samba_view_enabled(session: Session) -> bool:
    """Whether the `operator` role may view the Files and Samba pages."""
    return get_bool_setting(session, OPERATOR_VIEW_FILES_SAMBA_KEY)


def set_operator_files_samba_view(
    session: Session,
    *,
    enabled: bool,
    actor: User,
    ip_address: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> None:
    """Flip the runtime toggle and record the audit event (admin-only in the API)."""
    set_bool_setting(session, OPERATOR_VIEW_FILES_SAMBA_KEY, enabled)
    audit.record_audit_event(
        session,
        event=audit.SETTINGS_UPDATED,
        category=audit.CATEGORY_SETTINGS,
        actor=actor,
        target_type="setting",
        target_id=OPERATOR_VIEW_FILES_SAMBA_KEY,
        details={"operator_view_files_samba": enabled},
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
        commit=False,
    )
    session.commit()


def ensure_instance_id(session: Session) -> str:
    """Return this installation's stable identifier, creating it on first use."""
    existing = get_setting(session, INSTANCE_ID_KEY)
    if existing is not None:
        return existing
    instance_id = str(uuid.uuid4())
    set_setting(session, INSTANCE_ID_KEY, instance_id)
    return instance_id
