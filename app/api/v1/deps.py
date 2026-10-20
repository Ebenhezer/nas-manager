"""FastAPI dependencies: current user/session and permission guards."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ForbiddenError, UnauthorizedError
from app.db.dependencies import get_db
from app.models import AuthSession, User
from app.services.auth_service import load_valid_session
from app.services.security import effective_permissions_for_user


@dataclass
class CurrentUser:
    user: User
    session: AuthSession
    permissions: frozenset[str]

    @property
    def is_admin(self) -> bool:
        return "users:write" in self.permissions


def request_audit_context(request: Request) -> dict[str, str | None]:
    client = request.client
    return {
        "ip_address": client.host if client is not None else None,
        "user_agent": request.headers.get("user-agent"),
        "request_id": getattr(request.state, "request_id", None),
    }


def get_current_user(request: Request, db: Session = Depends(get_db)) -> CurrentUser:
    """Resolve the session cookie to a live session and its active user."""
    settings: Settings = request.app.state.settings
    raw_token = request.cookies.get(settings.session_cookie_name)
    loaded = load_valid_session(db, raw_token, settings)
    if loaded is None:
        raise UnauthorizedError("Authentication required")
    row, user = loaded
    permissions = effective_permissions_for_user(db, user)
    return CurrentUser(user=user, session=row, permissions=permissions)


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
