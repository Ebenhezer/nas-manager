"""Engine and session factory. The URL alone selects the backend (SQLite now, PostgreSQL later)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


def create_db_engine(url: str) -> Engine:
    kwargs: dict[str, Any] = {}
    is_sqlite = make_url(url).get_backend_name() == "sqlite"
    if is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
            finally:
                cursor.close()

    return engine


def redact_url(url: str) -> str:
    return make_url(url).render_as_string(hide_password=True)


class Database:
    """Owns the engine and session factory for one application instance."""

    def __init__(self, url: str) -> None:
        self.engine = create_db_engine(url)
        self.session_factory: sessionmaker[Session] = sessionmaker(
            self.engine, expire_on_commit=False
        )

    def dispose(self) -> None:
        self.engine.dispose()
