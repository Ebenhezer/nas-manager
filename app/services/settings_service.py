"""Runtime key/value settings stored in the database (non-secret values only)."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models.app_setting import AppSetting

INSTANCE_ID_KEY = "instance_id"


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


def ensure_instance_id(session: Session) -> str:
    """Return this installation's stable identifier, creating it on first use."""
    existing = get_setting(session, INSTANCE_ID_KEY)
    if existing is not None:
        return existing
    instance_id = str(uuid.uuid4())
    set_setting(session, INSTANCE_ID_KEY, instance_id)
    return instance_id
