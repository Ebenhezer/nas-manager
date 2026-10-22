"""Run the web application: python -m app"""

from __future__ import annotations

import uvicorn

from app.core.config import get_settings
from app.main import create_app


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,  # use the application's logging configuration
        access_log=False,  # request logging is done by RequestContextMiddleware
    )


if __name__ == "__main__":
    main()
