"""CSRF protection.

Defence in depth on top of SameSite cookies:

* Double-submit: every unsafe request that carries the ``nas_csrf`` cookie
  must echo its value in ``X-CSRF-Token`` (fetch) or the ``_csrf`` form field.
* Session binding: when the request also carries the session cookie, the
  echoed token must additionally match the digest stored in the session row,
  so a stolen CSRF cookie alone is useless.

Pure API clients that send *no* cookies are not blocked, and neither are
requests that carry ``Authorization: Bearer`` (Phase 6): the API then
authenticates with the token alone and ignores cookies, and a browser cannot
attach that header to a cross-site request without a CORS preflight, which
this application never grants. The cookie is delivered to browsers on HTML
responses (and the login route sets it explicitly on the login response).
"""

from __future__ import annotations

import hmac
import secrets
from typing import Literal
from urllib.parse import parse_qs

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.errors import error_response
from app.models import AuthSession
from app.services.auth_service import token_digest

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_URLENCODED = "application/x-www-form-urlencoded"
# The web forms that carry ``_csrf`` (login, logout) are tiny.
_MAX_FORM_BYTES = 64 * 1024


class CSRFProtectMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: object,
        *,
        csrf_cookie_name: str,
        session_cookie_name: str,
        csrf_max_age_seconds: int,
        cookie_secure: bool,
        cookie_samesite: Literal["lax", "strict", "none"],
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.csrf_cookie_name = csrf_cookie_name
        self.session_cookie_name = session_cookie_name
        self.csrf_max_age_seconds = csrf_max_age_seconds
        self.cookie_secure = cookie_secure
        self.cookie_samesite = cookie_samesite

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # A browser that has no CSRF cookie yet (first page load) receives one
        # on the response; make the same token available *before* the view
        # renders so forms and the <meta> tag embed the matching value.
        if request.method not in UNSAFE_METHODS and not request.cookies.get(self.csrf_cookie_name):
            request.state.csrf_token = secrets.token_urlsafe(32)
        if request.method in UNSAFE_METHODS and self._csrf_required(request):
            submitted = request.headers.get("X-CSRF-Token")
            if submitted is None:
                submitted = await self._read_form_token(request)
            cookie_token = request.cookies.get(self.csrf_cookie_name)
            if (
                not cookie_token
                or not submitted
                or not self._valid(request, cookie_token, submitted)
            ):
                return error_response(
                    request,
                    403,
                    "csrf_failure",
                    "CSRF token missing or invalid",
                    details={"method": request.method},
                )
        response = await call_next(request)
        self._maybe_set_cookie(request, response)
        return response

    def _csrf_required(self, request: Request) -> bool:
        """A check applies when the client carries either cookie; a cookie-less
        client (an API caller without browser state) cannot be CSRF-attacked,
        and a bearer-authenticated request is authorised by its header only."""
        if request.headers.get("authorization", "").lower().startswith("bearer "):
            return False
        return bool(
            request.cookies.get(self.csrf_cookie_name)
            or request.cookies.get(self.session_cookie_name)
        )

    def _valid(self, request: Request, cookie_token: str, submitted: str) -> bool:
        session_token = request.cookies.get(self.session_cookie_name)
        if session_token:
            row = self._session_row(request, session_token)
            if row is not None and not hmac.compare_digest(
                token_digest(submitted), row.csrf_token_hash
            ):
                return False
        return hmac.compare_digest(cookie_token, submitted)

    @staticmethod
    def _session_row(request: Request, raw_token: str) -> AuthSession | None:
        """Look up the session for the session-bound CSRF check. Failures are
        treated as "no session row" so the double-submit check still runs."""
        try:
            from typing import cast

            db = request.app.state.db
            with db.session_factory() as session:
                from sqlalchemy import select

                row = session.execute(
                    select(AuthSession).where(AuthSession.token_hash == token_digest(raw_token))
                ).scalar_one_or_none()
                return cast("AuthSession | None", row)
        except Exception:
            return None

    @staticmethod
    async def _read_form_token(request: Request) -> str | None:
        """Extract ``_csrf`` from a urlencoded form body and re-inject the
        buffered body so the route can parse the form afterwards.

        Only small urlencoded bodies are buffered. Multipart requests (file
        uploads) must send ``X-CSRF-Token``: reading them here would hold the
        whole upload in memory before the check could fail anyway.
        """
        content_type = request.headers.get("content-type", "").lower()
        if not content_type.startswith(_URLENCODED):
            return None
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > _MAX_FORM_BYTES:
            return None
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > _MAX_FORM_BYTES:
                return None
            chunks.append(chunk)
        body_bytes = b"".join(chunks)
        # Re-inject so FastAPI's form parser still sees the body.
        request._body = body_bytes
        values = parse_qs(body_bytes.decode("utf-8", errors="replace")).get("_csrf")
        return values[-1] if values else None

    def _maybe_set_cookie(self, request: Request, response: Response) -> None:
        if request.method != "GET":
            return
        if request.cookies.get(self.csrf_cookie_name):
            return
        content_type = response.headers.get("content-type", "")
        if not (content_type.startswith("text/html") or request.url.path == "/login"):
            return
        token = getattr(request.state, "csrf_token", None) or secrets.token_urlsafe(32)
        response.set_cookie(
            self.csrf_cookie_name,
            token,
            max_age=self.csrf_max_age_seconds,
            path="/",
            secure=self.cookie_secure,
            httponly=False,  # readable by api.js so fetch can echo it
            samesite=self.cookie_samesite,
        )
