"""FastAPI dependencies: current user/session and permission guards.

Two credentials are accepted:

* the browser session cookie (``nas_session``), and
* a personal API token in ``Authorization: Bearer nas_...`` (Phase 6).

When an ``Authorization`` header is present it is authoritative: an invalid
or non-Bearer value is a 401 even if a valid session cookie is also sent, so
a client never silently runs with credentials it did not intend to use.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ForbiddenError, TooManyRequestsError, UnauthorizedError
from app.db.dependencies import get_db
from app.models import ApiToken, AuthSession, User
from app.services import api_tokens, audit
from app.services.auth_service import load_valid_session
from app.services.security import effective_permissions_for_user
from app.services.throttle import IpLoginThrottle

AUTH_METHOD_SESSION = "session"
AUTH_METHOD_TOKEN = "token"  # noqa: S105 - a label, not a credential
_BEARER_CHALLENGE = {"WWW-Authenticate": 'Bearer realm="nas-manager"'}


@dataclass
class CurrentUser:
    user: User
    session: AuthSession | None
    permissions: frozenset[str]
    token: ApiToken | None = None

    @property
    def is_admin(self) -> bool:
        return "users:write" in self.permissions

    @property
    def auth_method(self) -> str:
        return AUTH_METHOD_TOKEN if self.token is not None else AUTH_METHOD_SESSION


def request_audit_context(request: Request) -> dict[str, str | None]:
    client = request.client
    return {
        "ip_address": client.host if client is not None else None,
        "user_agent": request.headers.get("user-agent"),
        "request_id": getattr(request.state, "request_id", None),
    }


def bearer_token(request: Request) -> str | None:
    """The raw bearer token, ``""`` for a malformed header, None when absent."""
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, _, value = header.strip().partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return ""
    return value.strip()


def _token_user(request: Request, db: Session, raw: str) -> CurrentUser:
    ctx = request_audit_context(request)
    ip = ctx.get("ip_address") or "unknown"
    throttle: IpLoginThrottle = request.app.state.token_throttle
    loaded = api_tokens.load_valid_token(db, raw, ip_address=ctx.get("ip_address")) if raw else None
    if loaded is None:
        # Only failures count against the window, so a busy legitimate
        # client is never throttled by its own successful requests. Each
        # failure inside the window is audited; once the window is
        # exhausted further attempts are refused without an audit row, so
        # an attacker cannot flood the log.
        state = throttle.check(ip)
        if not state.allowed:
            raise TooManyRequestsError(
                "too many invalid API token attempts; try again later",
                details={"retry_after_seconds": state.retry_after_seconds},
                headers={"Retry-After": str(state.retry_after_seconds)},
            )
        audit.record_audit_event(
            db,
            event=audit.API_TOKEN_AUTH_FAILED,
            category=audit.CATEGORY_API_TOKEN,
            outcome=audit.OUTCOME_FAILURE,
            details={"reason": "malformed" if not raw else "invalid"},
            **audit.request_context(ctx),
        )
        raise UnauthorizedError("Invalid or expired API token", headers=_BEARER_CHALLENGE)
    token, user = loaded
    return CurrentUser(
        user=user,
        session=None,
        permissions=api_tokens.token_permissions(db, token, user),
        token=token,
    )


def get_current_user(request: Request, db: Session = Depends(get_db)) -> CurrentUser:
    """Resolve the bearer token or the session cookie to an active user."""
    raw_bearer = bearer_token(request)
    if raw_bearer is not None:
        return _token_user(request, db, raw_bearer)
    settings: Settings = request.app.state.settings
    raw_token = request.cookies.get(settings.session_cookie_name)
    loaded = load_valid_session(db, raw_token, settings)
    if loaded is None:
        raise UnauthorizedError("Authentication required", headers=_BEARER_CHALLENGE)
    row, user = loaded
    permissions = effective_permissions_for_user(db, user)
    return CurrentUser(user=user, session=row, permissions=permissions)


def require_session_auth(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Allow only interactive (cookie session) callers.

    Used for account-control endpoints — minting/revoking tokens, changing
    the password, logging out — so a leaked token cannot entrench itself.
    """
    if current.session is None:
        raise ForbiddenError(
            "This action requires an interactive session, not an API token",
            details={"auth_method": current.auth_method},
        )
    return current


def require_permission(permission: str) -> Callable[[CurrentUser], CurrentUser]:
    """Parameterized dependency: ``Depends(require_permission("users:write"))``."""

    def guard(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if permission not in current.permissions:
            raise ForbiddenError(
                f"Missing required permission: {permission}",
                details={"permission": permission},
            )
        return current

    return guard
