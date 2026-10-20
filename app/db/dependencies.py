"""FastAPI dependency that yields one database session per request."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from app.db.session import Database


def get_db(request: Request) -> Iterator[Session]:
    """Provide a request-scoped session. FastAPI closes it when done."""
    db: Database = request.app.state.db
    with db.session_factory() as session:
        yield session
