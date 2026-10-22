"""Application health: database reachability and migration state."""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.core.config import Settings
from app.db.migrations import get_current_revision, get_head_revision
from app.db.session import Database
from app.schemas.health import DatabaseHealth, HealthResponse

logger = logging.getLogger(__name__)


def build_health_report(settings: Settings, db: Database) -> HealthResponse:
    head = get_head_revision(settings)
    try:
        with db.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        current = get_current_revision(db.engine)
        reachable = True
    except SQLAlchemyError:
        logger.warning("database health check failed", exc_info=True)
        current = None
        reachable = False

    schema_current = reachable and current == head
    status = "unhealthy" if not reachable else ("ok" if schema_current else "degraded")
    return HealthResponse(
        status=status,
        version=__version__,
        environment=settings.environment,
        database=DatabaseHealth(
            reachable=reachable,
            schema_current=schema_current,
            revision=current,
            head_revision=head,
        ),
    )
