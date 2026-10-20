from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.session import Database
from app.services.settings_service import ensure_instance_id, get_setting, set_setting


@pytest.fixture
def session(migrated_settings: Settings) -> Iterator[Session]:
    db = Database(migrated_settings.resolved_database_url)
    with db.session_factory() as s:
        yield s
    db.dispose()


def test_get_missing_returns_none(session: Session) -> None:
    assert get_setting(session, "nope") is None


def test_set_then_get_and_update(session: Session) -> None:
    set_setting(session, "theme", "dark")
    assert get_setting(session, "theme") == "dark"
    set_setting(session, "theme", "light")
    assert get_setting(session, "theme") == "light"


def test_instance_id_is_stable(session: Session) -> None:
    first = ensure_instance_id(session)
    assert len(first) == 36
    assert ensure_instance_id(session) == first
