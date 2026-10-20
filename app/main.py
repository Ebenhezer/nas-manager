"""Application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.csrf import CSRFProtectMiddleware
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.db.migrations import get_current_revision, get_head_revision, upgrade_to_head
from app.db.session import Database, redact_url
from app.services.monitoring import prime_cpu_counter
from app.services.settings_service import ensure_instance_id
from app.services.throttle import IpLoginThrottle
from app.web import routes as web_routes

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    settings.ensure_directories()
    db = Database(settings.resolved_database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "starting",
            extra={
                "version": __version__,
                "environment": settings.environment,
                "database": redact_url(settings.resolved_database_url),
            },
        )
        if settings.environment == "production" and not settings.session_cookie_secure:
            logger.warning(
                "NAS_SESSION_COOKIE_SECURE is false; sessions will be sent over plain HTTP. "
                "Terminate TLS in front of the application and set it to true."
            )
        if settings.auto_migrate:
            upgrade_to_head(settings)

        head = get_head_revision(settings)
        if get_current_revision(db.engine) == head:
            with db.session_factory() as session:
                app.state.instance_id = ensure_instance_id(session)
        else:
            app.state.instance_id = None
            logger.warning("database schema is not up to date; run: alembic upgrade head")

        prime_cpu_counter()
        try:
            yield
        finally:
            db.dispose()
            logger.info("stopped")

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs" if settings.debug else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.debug else None,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.throttle = IpLoginThrottle(
        max_attempts=settings.login_ip_max_attempts,
        window_seconds=settings.login_ip_window_seconds,
    )

    register_exception_handlers(app)
    # Last added middleware is outermost: RequestContextMiddleware must stay
    # outside so request ids are available to the CSRF error envelope.
    app.add_middleware(
        CSRFProtectMiddleware,
        csrf_cookie_name=settings.csrf_cookie_name,
        session_cookie_name=settings.session_cookie_name,
        csrf_max_age_seconds=settings.csrf_max_age_seconds,
        cookie_secure=settings.session_cookie_secure,
        cookie_samesite=settings.session_cookie_samesite,
    )
    app.add_middleware(RequestContextMiddleware)
    app.mount("/static", StaticFiles(directory=web_routes.STATIC_DIR), name="static")
    app.include_router(api_router)
    app.include_router(web_routes.router)
    return app
