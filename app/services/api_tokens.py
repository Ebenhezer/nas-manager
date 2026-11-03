"""Personal API tokens (Phase 6).

A token is a long random bearer credential (``nas_<urlsafe>``) bound to one
user and a list of scopes. Only the SHA-256 of the raw value is stored, so a
database leak cannot be replayed; the raw value is returned once, when the
token is created.

Effective access of a token is ``scopes ∩ effective_permissions(owner)``,
evaluated on every request: taking a role away from the owner immediately
narrows every token they hold, and a token can never exceed its owner.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.core.permissions import validate_permissions
from app.db.base import utcnow_naive
from app.models import ApiToken, User
from app.services import audit
from app.services.auth_service import generate_token, token_digest
from app.services.security import effective_permissions_for_user

TOKEN_PREFIX = "nas_"  # noqa: S105 - a format marker, not a credential
_DISPLAY_PREFIX_LENGTH = 12
_LAST_USED_TOUCH_SECONDS = 60


def looks_like_token(raw: str) -> bool:
    return raw.startswith(TOKEN_PREFIX) and 16 <= len(raw) <= 256


def grantable_scopes(session: Session, user: User) -> list[str]:
    """The scopes ``user`` may put on a new token: their own permissions."""
    return sorted(effective_permissions_for_user(session, user))


def token_permissions(session: Session, token: ApiToken, user: User) -> frozenset[str]:
    """Effective permissions of a token request (scopes ∩ owner's permissions)."""
    return effective_permissions_for_user(session, user) & frozenset(token.scopes)


def create_token(
    session: Session,
    user: User,
    *,
    name: str,
    scopes: list[str],
    expires_in_days: int | None,
    settings: Settings,
    context: dict[str, str | None] | None = None,
) -> tuple[str, ApiToken]:
    """Create a token for ``user`` and return ``(raw_token, row)``.

    ``expires_in_days=None`` uses the configured default lifetime; ``0``
    requests a never-expiring token, allowed only when the configured
    maximum lifetime is 0.
    """
    clean_name = name.strip()
    if not clean_name or len(clean_name) > 64:
        raise BadRequestError("Token name must be 1-64 characters")
    if not scopes:
        raise BadRequestError("A token needs at least one scope")
    try:
        validate_permissions(scopes)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    own = effective_permissions_for_user(session, user)
    excess = sorted(set(scopes) - own)
    if excess:
        raise ForbiddenError(
            "A token cannot carry permissions you do not hold",
            details={"scopes": excess},
        )

    max_days = settings.api_token_max_lifetime_days
    days = settings.api_token_default_lifetime_days if expires_in_days is None else expires_in_days
    if days < 0:
        raise BadRequestError("expires_in_days must not be negative")
    if days == 0 and max_days != 0:
        raise BadRequestError("Tokens must expire", details={"max_lifetime_days": max_days})
    if max_days and days > max_days:
        raise BadRequestError(
            f"Token lifetime may not exceed {max_days} days",
            details={"max_lifetime_days": max_days},
        )

    active = list_tokens(session, user=user, include_inactive=False)
    if len(active) >= settings.api_token_max_per_user:
        raise ConflictError(
            "Too many active API tokens; revoke one first",
            details={"max_per_user": settings.api_token_max_per_user},
        )
    if any(token.name.lower() == clean_name.lower() for token in active):
        raise ConflictError("An active token with that name already exists")

    raw = f"{TOKEN_PREFIX}{generate_token()}"
    now = utcnow_naive()
    row = ApiToken(
        user_id=user.id,
        name=clean_name,
        token_prefix=raw[:_DISPLAY_PREFIX_LENGTH],
        token_hash=token_digest(raw),
        created_at=now,
        expires_at=now + timedelta(days=days) if days else None,
    )
    row.scopes = scopes
    session.add(row)
    session.flush()
    audit.record_audit_event(
        session,
        event=audit.API_TOKEN_CREATED,
        category=audit.CATEGORY_API_TOKEN,
        actor=user,
        target_type="api_token",
        target_id=row.id,
        details={
            "name": clean_name,
            "scopes": row.scopes,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        },
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return raw, row


def list_tokens(
    session: Session,
    *,
    user: User | None = None,
    user_id: int | None = None,
    include_inactive: bool = True,
) -> list[ApiToken]:
    """Tokens of one user (or of everyone when neither filter is set), newest first."""
    query = select(ApiToken).order_by(ApiToken.created_at.desc(), ApiToken.id.desc())
    owner_id = user.id if user is not None else user_id
    if owner_id is not None:
        query = query.where(ApiToken.user_id == owner_id)
    rows = list(session.execute(query).scalars().all())
    if include_inactive:
        return rows
    return [row for row in rows if not row.is_revoked and not row.is_expired]


def get_token_or_404(session: Session, token_id: int, *, owner: User | None = None) -> ApiToken:
    row = session.get(ApiToken, token_id)
    if row is None or (owner is not None and row.user_id != owner.id):
        raise NotFoundError("API token not found", details={"id": token_id})
    return row


def revoke_token(
    session: Session,
    row: ApiToken,
    *,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> ApiToken:
    """Revoke one token (idempotent). The row is kept for the audit trail."""
    if row.revoked_at is None:
        row.revoked_at = utcnow_naive()
    audit.record_audit_event(
        session,
        event=audit.API_TOKEN_REVOKED,
        category=audit.CATEGORY_API_TOKEN,
        actor=actor,
        target_type="api_token",
        target_id=row.id,
        details={"name": row.name, "owner_user_id": row.user_id},
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return row


def revoke_all_user_tokens(session: Session, user: User) -> int:
    """Revoke every live token of ``user`` (deactivation, admin password reset)."""
    rows = (
        session.execute(
            select(ApiToken).where(ApiToken.user_id == user.id, ApiToken.revoked_at.is_(None))
        )
        .scalars()
        .all()
    )
    now = utcnow_naive()
    for row in rows:
        row.revoked_at = now
    session.commit()
    return len(rows)


def load_valid_token(
    session: Session, raw_token: str, *, ip_address: str | None = None
) -> tuple[ApiToken, User] | None:
    """Resolve a raw bearer token to a live token and its active owner."""
    if not looks_like_token(raw_token):
        return None
    row = session.execute(
        select(ApiToken).where(ApiToken.token_hash == token_digest(raw_token))
    ).scalar_one_or_none()
    if row is None or row.is_revoked or row.is_expired:
        return None
    user = row.user
    if user is None or not user.is_active:
        return None
    now = utcnow_naive()
    if (
        row.last_used_at is None
        or (now - row.last_used_at).total_seconds() >= _LAST_USED_TOUCH_SECONDS
    ):
        row.last_used_at = now
        row.last_used_ip = ip_address
        session.commit()
    return row, user
