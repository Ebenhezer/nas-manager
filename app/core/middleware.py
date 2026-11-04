"""Request-id propagation, access logging and security headers."""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import request_id_var

logger = logging.getLogger("app.access")

_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
_CSP = (
    "default-src 'self'; img-src 'self' data:; object-src 'none'; "
    "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    # Phase 14: no powerful browser features, and no cross-origin window or
    # resource access to this origin (media streams included).
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Request id, access log and security headers.

    ``Strict-Transport-Security`` is sent only on HTTPS requests (as seen
    through the trusted proxy's ``X-Forwarded-Proto``) and only when
    ``hsts_max_age_seconds`` is positive, so a plain-HTTP LAN install is
    never pinned to HTTPS by accident.
    """

    def __init__(self, app: object, *, hsts_max_age_seconds: int = 0) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.hsts = f"max-age={hsts_max_age_seconds}" if hsts_max_age_seconds > 0 else None

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _VALID_REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)

        response.headers["X-Request-ID"] = request_id
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if self.hsts is not None and request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", self.hsts)
        if not request.url.path.startswith("/api/docs"):
            response.headers.setdefault("Content-Security-Policy", _CSP)

        if not request.url.path.startswith("/static/"):
            logger.info(
                "request",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
        return response
