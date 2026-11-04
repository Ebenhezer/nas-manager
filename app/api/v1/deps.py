"""FastAPI dependencies: current user/session and permission guards.

Two credentials are accepted:

* the browser session cookie (``nas_session``), and
* a personal API token in ``Authorization: Bearer nas_...`` (Phase 6).

When an ``Authorization`` header is present it is authoritative: an invalid
or non-Bearer value is a 401 even if a valid session cookie is also sent, so
a client never silently runs with credentials it did not intend to use.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field

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


@dataclass
class DenialThrottle:
    """Audit each (user, permission, path) denial at most once per window, so
    a client polling a forbidden endpoint cannot flood the audit log."""

    window_seconds: float = 60.0
    max_entries: int = 10_000
    _seen: dict[Hashable, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def should_record(self, key: Hashable) -> bool:
        now = time.monotonic()
        with self._lock:
            last = self._seen.get(key)
            if last is not None and now - last < self.window_seconds:
                return False
            if len(self._seen) >= self.max_entries:
                cutoff = now - self.window_seconds
                self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
            self._seen[key] = now
            return True


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


def require_permission(permission: str) -> Callable[..., CurrentUser]:
    """Parameterized dependency: ``Depends(require_permission("users:write"))``."""

    def guard(
        request: Request,
        current: CurrentUser = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> CurrentUser:
        if permission not in current.permissions:
            _audit_denial(request, db, current, permission)
            raise ForbiddenError(
                f"Missing required permission: {permission}",
                details={"permission": permission},
            )
        return current

    return guard


def _path_template(request: Request) -> str:
    """``/api/v1/users/{user_id}`` for ``/api/v1/users/7``: the throttle and the
    audit details name the endpoint, not each object id."""
    by_value = {str(value): name for name, value in request.path_params.items()}
    segments = [
        f"{{{by_value[segment]}}}" if segment in by_value else segment
        for segment in request.url.path.split("/")
    ]
    return "/".join(segments)


def _audit_denial(request: Request, db: Session, current: CurrentUser, permission: str) -> None:
    throttle: DenialThrottle = request.app.state.denial_throttle
    path = _path_template(request)
    if not throttle.should_record((current.user.id, permission, path)):
        return
    audit.record_audit_event(
        db,
        event=audit.AUTH_PERMISSION_DENIED,
        category=audit.CATEGORY_AUTH,
        outcome=audit.OUTCOME_FAILURE,
        actor=current.user,
        target_type="permission",
        target_id=permission,
        details={
            "method": request.method,
            "path": path,
            "auth_method": current.auth_method,
        },
        **audit.request_context(request_audit_context(request)),
    )


def require_session_permission(permission: str) -> Callable[[CurrentUser], CurrentUser]:
    """``require_permission`` that additionally refuses API tokens.

    Used where a request handles credentials (Phase 7 remote connections),
    so a leaked token can neither plant nor redirect stored secrets.
    """
    permission_guard = require_permission(permission)

    def guard(current: CurrentUser = Depends(permission_guard)) -> CurrentUser:
        return require_session_auth(current)

    return guard
