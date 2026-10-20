"""Declarative base with a stable constraint naming convention (Alembic/PostgreSQL friendly)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    return datetime.now(UTC)


def utcnow_naive() -> datetime:
    """Naive-UTC wall clock.

    SQLite stores ``DateTime`` values without an offset, so tz-aware values
    written to it come back naive. Authentication code compares timestamps in
    Python; using a consistently naive UTC clock there avoids mixing aware and
    naive values. The stored wall-clock time is identical to ``utcnow()``.
    """
    return datetime.now(UTC).replace(tzinfo=None)
