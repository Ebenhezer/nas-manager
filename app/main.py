"""Application factory."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.api.v1.deps import DenialThrottle
from app.api.v1.public_uploads import PublicUploadGuardMiddleware
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.csrf import CSRFProtectMiddleware
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.db.migrations import get_current_revision, get_head_revision, upgrade_to_head
from app.db.session import Database, redact_url
from app.services import audit
from app.services.monitoring import prime_cpu_counter
from app.services.settings_service import ensure_instance_id
from app.services.syslog.log_handler import reload_log_forwarding
from app.services.throttle import IpLoginThrottle, SlidingWindowThrottle
from app.web import routes as web_routes

logger = logging.getLogger(__name__)


def _lifecycle_event(session: Session, event: str, settings: Settings) -> None:
    audit.record_audit_event(
        session,
        event=event,
        category=audit.CATEGORY_SYSTEM,
        actor_username="system",
        target_type="process",
        target_id=settings.syslog_identifier,
        details={"version": __version__, "pid": os.getpid()},
    )


def production_warnings(settings: Settings) -> list[str]:
    """Configuration choices that are unsafe on a production host."""
    if settings.environment != "production":
        return []
    warnings: list[str] = []
    if not settings.session_cookie_secure:
        warnings.append(
            "NAS_SESSION_COOKIE_SECURE is false; sessions will be sent over plain HTTP. "
            "Terminate TLS in front of the application and set it to true."
        )
    if settings.debug:
        warnings.append(
            "NAS_DEBUG is true in production; the API documentation endpoints are exposed."
        )
    if not settings.trusted_host_list:
        warnings.append(
            "NAS_TRUSTED_HOSTS is empty; any Host header is accepted. "
            "Set it to the names this NAS is reached by."
        )
    if "*" in settings.forwarded_allow_ips.split(","):
        warnings.append(
            "NAS_FORWARDED_ALLOW_IPS is '*'; any client can spoof its address "
            "with X-Forwarded-For. List the reverse proxy addresses instead."
        )
    if settings.public_upload_enabled and settings.public_upload_rate_max_requests == 0:
        warnings.append(
            "Public uploads are enabled without a rate limit "
            "(NAS_PUBLIC_UPLOAD_RATE_MAX_REQUESTS=0)."
        )
    return warnings


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
        for message in production_warnings(settings):
            logger.warning(message)
        if settings.auto_migrate:
            upgrade_to_head(settings)

        head = get_head_revision(settings)
        schema_current = get_current_revision(db.engine) == head
        if schema_current:
            with db.session_factory() as session:
                app.state.instance_id = ensure_instance_id(session)
                reload_log_forwarding(session)
                _lifecycle_event(session, audit.SYSTEM_STARTED, settings)
        else:
            app.state.instance_id = None
            logger.warning("database schema is not up to date; run: alembic upgrade head")

        prime_cpu_counter()
        try:
            yield
        finally:
            if schema_current:
                try:
                    with db.session_factory() as session:
                        _lifecycle_event(session, audit.SYSTEM_STOPPED, settings)
                except Exception:
                    logger.exception("could not record the shutdown audit event")
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
    # Separate window for failed bearer-token lookups (Phase 6) so token
    # guessing cannot burn the login allowance and vice versa.
    app.state.denial_throttle = DenialThrottle()
    app.state.token_throttle = IpLoginThrottle(
        max_attempts=settings.api_token_auth_max_failures,
        window_seconds=settings.api_token_auth_window_seconds,
    )
    # Request rate of the anonymous drop endpoint (Phase 14), per uploader.
    app.state.public_upload_throttle = (
        SlidingWindowThrottle(
            max_attempts=settings.public_upload_rate_max_requests,
            window_seconds=settings.public_upload_rate_window_seconds,
        )
        if settings.public_upload_rate_max_requests
        else None
    )

    register_exception_handlers(app)
    # Last added middleware is outermost: RequestContextMiddleware must stay
    # outside so request ids are available to the CSRF error envelope. The
    # drop-folder guard sits inside CSRF, so a forged cross-site request is
    # refused before it can use up a victim's upload allowance.
    app.add_middleware(
        PublicUploadGuardMiddleware,
        audit_window_seconds=settings.public_upload_rate_window_seconds,
    )
    app.add_middleware(
        CSRFProtectMiddleware,
        csrf_cookie_name=settings.csrf_cookie_name,
        session_cookie_name=settings.session_cookie_name,
        csrf_max_age_seconds=settings.csrf_max_age_seconds,
        cookie_secure=settings.session_cookie_secure,
        cookie_samesite=settings.session_cookie_samesite,
    )
    if settings.trusted_host_list:
        # Inside RequestContextMiddleware, so refusals still carry the
        # security headers and a request id.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_host_list)
    app.add_middleware(RequestContextMiddleware, hsts_max_age_seconds=settings.hsts_max_age_seconds)
    app.mount("/static", StaticFiles(directory=web_routes.STATIC_DIR), name="static")
    app.include_router(api_router)
    app.include_router(web_routes.router)
    return app
