from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import __version__
from app.api.v1.deps import CurrentUser
from app.core.errors import UnauthorizedError
from app.db.dependencies import get_db
from app.services import auth_service
from app.services.security import effective_permissions_for_user

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
templates = Jinja2Templates(directory=WEB_DIR / "templates")

router = APIRouter(include_in_schema=False)


def resolve_web_user(request: Request, db: Session = Depends(get_db)) -> CurrentUser | None:
    """Return the current user for web pages, or ``None`` when anonymous."""
    settings = request.app.state.settings
    raw_token = request.cookies.get(settings.session_cookie_name)
    loaded = auth_service.load_valid_session(db, raw_token, settings)
    if loaded is None:
        return None
    row, user = loaded
    return CurrentUser(user=user, session=row, permissions=effective_permissions_for_user(db, user))


def _page_context(request: Request, current: CurrentUser | None) -> dict[str, object]:
    settings = request.app.state.settings
    # The middleware puts a fresh token on request.state for first-time
    # visitors (before the Set-Cookie response exists); afterwards the browser
    # cookie is authoritative.
    csrf = (
        request.cookies.get(settings.csrf_cookie_name, "")
        or getattr(request.state, "csrf_token", "")
        or ""
    )
    perms = frozenset(current.permissions) if current else frozenset()
    return {
        "app_name": settings.app_name,
        "version": __version__,
        "csrf_token": csrf,
        "user": current.user if current else None,
        "permissions": perms,
        "can": lambda perm: perm in perms,
    }


def _protected(current: CurrentUser | None) -> RedirectResponse | None:
    if current is None:
        return RedirectResponse("/login", status_code=302)
    return None


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request, current: CurrentUser | None = Depends(resolve_web_user)
) -> Response:
    if current is not None:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", _page_context(request, None))


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(min_length=1, max_length=64),
    password: str = Form(min_length=1, max_length=512),
    db: Session = Depends(get_db),
) -> Response:
    settings = request.app.state.settings
    client = request.client
    result = auth_service.authenticate(
        db,
        username=username,
        password=password,
        ip_address=client.host if client is not None else None,
        user_agent=request.headers.get("user-agent"),
        settings=settings,
        throttle=request.app.state.throttle,
    )
    if result.ok and result.user is not None and result.raw_session_token is not None:
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            settings.session_cookie_name,
            result.raw_session_token,
            max_age=settings.session_idle_max_seconds,
            path="/",
            secure=settings.session_cookie_secure,
            httponly=True,
            samesite=settings.session_cookie_samesite,
        )
        response.set_cookie(
            settings.csrf_cookie_name,
            result.csrf_token or "",
            max_age=settings.csrf_max_age_seconds,
            path="/",
            secure=settings.session_cookie_secure,
            httponly=False,
            samesite=settings.session_cookie_samesite,
        )
        return response
    ctx = _page_context(request, None)
    ctx["error"] = result.error_message or "Invalid username or password"
    return templates.TemplateResponse(request, "login.html", ctx, status_code=401)


@router.post("/logout")
def logout_submit(
    request: Request,
    current: CurrentUser | None = Depends(resolve_web_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if current is not None:
        auth_service.logout(db, current.session, actor=current.user)
    response = RedirectResponse("/login", status_code=303)
    settings = request.app.state.settings
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")
    return response


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request, current: CurrentUser | None = Depends(resolve_web_user)
) -> Response:
    redirect = _protected(current)
    if redirect is not None:
        return redirect
    return templates.TemplateResponse(request, "dashboard.html", _page_context(request, current))


@router.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request, current: CurrentUser | None = Depends(resolve_web_user)
) -> Response:
    redirect = _protected(current)
    if redirect is not None:
        return redirect
    if current is None or "users:read" not in current.permissions:
        raise UnauthorizedError("Missing required permission: users:read")
    return templates.TemplateResponse(request, "users.html", _page_context(request, current))


@router.get("/groups", response_class=HTMLResponse)
def groups_page(
    request: Request, current: CurrentUser | None = Depends(resolve_web_user)
) -> Response:
    redirect = _protected(current)
    if redirect is not None:
        return redirect
    if current is None or "groups:read" not in current.permissions:
        raise UnauthorizedError("Missing required permission: groups:read")
    return templates.TemplateResponse(request, "groups.html", _page_context(request, current))


@router.get("/audit", response_class=HTMLResponse)
def audit_page(
    request: Request, current: CurrentUser | None = Depends(resolve_web_user)
) -> Response:
    redirect = _protected(current)
    if redirect is not None:
        return redirect
    if current is None or "audit:read" not in current.permissions:
        raise UnauthorizedError("Missing required permission: audit:read")
    return templates.TemplateResponse(request, "audit.html", _page_context(request, current))


@router.get("/profile", response_class=HTMLResponse)
def profile_page(
    request: Request, current: CurrentUser | None = Depends(resolve_web_user)
) -> Response:
    redirect = _protected(current)
    if redirect is not None:
        return redirect
    return templates.TemplateResponse(request, "profile.html", _page_context(request, current))
