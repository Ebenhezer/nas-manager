from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db.session import Database, redact_url


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    yield database
    database.dispose()


def test_sqlite_pragmas_applied(db: Database) -> None:
    with db.engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert str(conn.execute(text("PRAGMA journal_mode")).scalar()).lower() == "wal"
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000


def test_session_factory_commits_and_reads(db: Database) -> None:
    with db.engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
    with db.session_factory() as session:
        session.execute(text("INSERT INTO t (v) VALUES ('x')"))
        session.commit()
    with db.session_factory() as session:
        assert session.execute(text("SELECT v FROM t")).scalar() == "x"


def test_redact_url_hides_password() -> None:
    redacted = redact_url("postgresql+psycopg://nas:supersecret@db.local/nas")
    assert "supersecret" not in redacted
    assert "nas" in redacted
