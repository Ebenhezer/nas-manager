"""Run the sync worker: python -m app.worker"""

from __future__ import annotations

import logging
import signal
import sys
from types import FrameType

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.migrations import get_current_revision, get_head_revision
from app.db.session import Database
from app.worker.runner import Worker

logger = logging.getLogger("app.worker")


def main() -> int:
    settings = get_settings()
    configure_logging(settings)
    settings.ensure_directories()
    db = Database(settings.resolved_database_url)
    try:
        if get_current_revision(db.engine) != get_head_revision(settings):
            logger.error("database schema is not up to date; run: alembic upgrade head")
            return 1
        worker = Worker(settings, db)

        def _stop(signum: int, _frame: FrameType | None) -> None:
            logger.info("signal received", extra={"signal": signum})
            worker.stop.set()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        worker.run_forever()
        return 0
    finally:
        db.dispose()


if __name__ == "__main__":
    sys.exit(main())
