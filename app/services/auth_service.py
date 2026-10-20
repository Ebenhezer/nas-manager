"""Authentication: sessions, login/logout, lockout and helper primitives.

Sessions are server-side rows in ``auth_sessions``. The browser keeps only an
opaque random token; the database stores its SHA-256. Nothing password-like is
ever written to logs — the audit module records usernames, outcomes and
metadata, and covers passwords through the logging layer's redaction.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import TooManyRequestsError
from app.db.base import utcnow_naive
from app.models import AuthSession, User
from app.services import audit
from app.services.passwords import verify_password
from app.services.throttle import IpLoginThrottle, assert_login_allowed

INVALID_CREDENTIALS = "invalid_credentials"
ACCOUNT_LOCKED = "account_locked"
ACCOUNT_INACTIVE = "account_inactive"
ACCOUNT_MISSING = "account_missing"

_IDLE_TOUCH_SECONDS = 60


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class LoginResult:
    user: User | None
    raw_session_token: str | None = None
    csrf_token: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    locked_until: object | None = None

    @property
    def ok(self) -> bool:
        return self.error_code is None


_DUMMY_HASH_CACHE: dict[tuple[int, int, int], str] = {}


def _dummy_hash(settings: Settings) -> str:
    """A valid Argon2id hash used to equalise timing when the username is unknown."""
    from app.services.passwords import build_hasher

    key = (settings.argon2_time_cost, settings.argon2_memory_cost, settings.argon2_parallelism)
    cached = _DUMMY_HASH_CACHE.get(key)
    if cached is None:
        cached = build_hasher(settings).hash("timing-equalisation-dummy")
        _DUMMY_HASH_CACHE[key] = cached
    return cached


def create_session(
    session: Session,
    user: User,
    *,
    settings: Settings,
    ip_address: str | None,
    user_agent: str | None,
) -> tuple[str, str, AuthSession]:
    """Create a session row and return ``(raw_token, csrf_token, row)``.

    Callers send ``raw_token`` to the browser in a cookie and return
    ``csrf_token`` in the response body; only the digests are stored.
    """
    raw_token = generate_token()
    csrf_token = generate_token()
    now = utcnow_naive()
    row = AuthSession(
        user_id=user.id,
        token_hash=token_digest(raw_token),
        csrf_token_hash=token_digest(csrf_token),
        ip_address=ip_address,
        user_agent=(user_agent or "")[:255] or None,
        created_at=now,
        expires_at=now + timedelta(seconds=settings.session_ttl_seconds),
        last_seen_at=now,
    )
    session.add(row)
    return raw_token, csrf_token, row


def _touch_session(session: Session, row: AuthSession, settings: Settings) -> None:
    """Slide the expiry window on activity, throttled to one write per minute."""
    now = utcnow_naive()
    if (now - row.last_seen_at).total_seconds() < _IDLE_TOUCH_SECONDS:
        return
    row.last_seen_at = now
    # Extend the TTL from now, but never beyond the absolute idle ceiling.
    extended = now + timedelta(seconds=settings.session_ttl_seconds)
    ceiling = now + timedelta(seconds=settings.session_idle_max_seconds)
    row.expires_at = min(ceiling, max(row.expires_at, extended))
    session.commit()


def load_valid_session(
    session: Session, raw_token: str | None, settings: Settings
) -> tuple[AuthSession, User] | None:
    """Resolve a cookie token to a live session and its active user."""
    if not raw_token:
        return None
    row = session.execute(
        select(AuthSession).where(AuthSession.token_hash == token_digest(raw_token))
    ).scalar_one_or_none()
    if row is None or row.is_revoked or row.is_expired:
        return None
    user = row.user
    if user is None or not user.is_active:
        return None
    _touch_session(session, row, settings)
    return row, user


def revoke_session(session: Session, row: AuthSession) -> None:
    """Revoke one session (logout). The row is kept for the audit trail."""
    if row.revoked_at is None:
        row.revoked_at = utcnow_naive()
    session.commit()


def authenticate(
    session: Session,
    *,
    username: str,
    password: str,
    ip_address: str | None,
    user_agent: str | None,
    settings: Settings,
    throttle: IpLoginThrottle,
) -> LoginResult:
    """Attempt a login and record an audit event for every outcome.

    Raises ``TooManyRequestsError`` (429) when the per-IP window is exhausted.
    Returns a ``LoginResult``; ``ok`` is False for credential, lockout and
    deactivation failures (all answered with 401 by the caller).
    """
    ip = ip_address or "unknown"
    try:
        assert_login_allowed(throttle, ip)
    except TooManyRequestsError:
        audit.record_audit_event(
            session,
            event=audit.AUTH_LOGIN_RATE_LIMITED,
            category=audit.CATEGORY_AUTH,
            outcome=audit.OUTCOME_FAILURE,
            details={"username": username.strip()},
            ip_address=ip,
            user_agent=user_agent,
            commit=False,
        )
        session.commit()
        raise

    user = session.execute(
        select(User).where(User.username == username.strip().lower())
    ).scalar_one_or_none()

    if user is None:
        # Equalise timing with the real verify path; then audit and fail.
        verify_password(password, _dummy_hash(settings), settings)
        audit.record_audit_event(
            session,
            event=audit.AUTH_LOGIN_FAILURE,
            category=audit.CATEGORY_AUTH,
            outcome=audit.OUTCOME_FAILURE,
            details={"username": username.strip()},
            ip_address=ip,
            user_agent=user_agent,
            commit=False,
        )
        session.commit()
        return LoginResult(
            user=None, error_code=ACCOUNT_MISSING, error_message="Invalid username or password"
        )

    now = utcnow_naive()
    if user.is_locked:
        audit.record_audit_event(
            session,
            event=audit.AUTH_LOGIN_LOCKED,
            category=audit.CATEGORY_AUTH,
            outcome=audit.OUTCOME_FAILURE,
            actor=user,
            details={"locked_until": user.locked_until.isoformat() if user.locked_until else None},
            ip_address=ip,
            user_agent=user_agent,
            commit=False,
        )
        session.commit()
        return LoginResult(
            user=user,
            error_code=ACCOUNT_LOCKED,
            error_message="Account temporarily locked due to too many failed attempts",
            locked_until=user.locked_until,
        )

    if not verify_password(password, user.password_hash, settings):
        user.failed_login_count += 1
        if user.failed_login_count >= settings.login_lockout_threshold:
            user.locked_until = now + timedelta(minutes=settings.login_lockout_minutes)
            user.failed_login_count = 0
            event, message, code = (
                audit.AUTH_LOGIN_LOCKED,
                "Account temporarily locked",
                ACCOUNT_LOCKED,
            )
        else:
            event, message, code = (
                audit.AUTH_LOGIN_FAILURE,
                "Invalid username or password",
                ACCOUNT_MISSING,
            )
        audit.record_audit_event(
            session,
            event=event,
            category=audit.CATEGORY_AUTH,
            outcome=audit.OUTCOME_FAILURE,
            actor=user,
            details={
                "remaining_attempts": max(
                    0, settings.login_lockout_threshold - user.failed_login_count
                )
            },
            ip_address=ip,
            user_agent=user_agent,
            commit=False,
        )
        session.commit()
        return LoginResult(
            user=user, error_code=code, error_message=message, locked_until=user.locked_until
        )

    if not user.is_active:
        audit.record_audit_event(
            session,
            event=audit.AUTH_LOGIN_FAILURE,
            category=audit.CATEGORY_AUTH,
            outcome=audit.OUTCOME_FAILURE,
            actor=user,
            details={"reason": "deactivated"},
            ip_address=ip,
            user_agent=user_agent,
            commit=False,
        )
        session.commit()
        return LoginResult(
            user=user, error_code=ACCOUNT_INACTIVE, error_message="Account is disabled"
        )

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = utcnow_naive()
    raw_token, csrf_token, _row = create_session(
        session, user, settings=settings, ip_address=ip, user_agent=user_agent
    )
    audit.record_audit_event(
        session,
        event=audit.AUTH_LOGIN_SUCCESS,
        category=audit.CATEGORY_AUTH,
        actor=user,
        ip_address=ip,
        user_agent=user_agent,
        commit=False,
    )
    session.commit()
    return LoginResult(user=user, raw_session_token=raw_token, csrf_token=csrf_token)


def logout(session: Session, row: AuthSession, *, actor: User) -> None:
    revoke_session(session, row)
    audit.record_audit_event(
        session,
        event=audit.AUTH_LOGOUT,
        category=audit.CATEGORY_AUTH,
        actor=actor,
        commit=False,
    )
    session.commit()


def revoke_all_user_sessions(session: Session, user: User) -> int:
    rows = (
        session.execute(
            select(AuthSession).where(
                AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)
            )
        )
        .scalars()
        .all()
    )
    count = 0
    for row in rows:
        row.revoked_at = utcnow_naive()
        count += 1
    session.commit()
    return count
