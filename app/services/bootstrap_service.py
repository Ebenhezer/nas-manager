"""First-run administrator bootstrap used by ``python -m app.cli bootstrap-admin``.

The CLI reads the password from a prompt or from stdin; it is never taken from
the command line, an environment variable or a file that the web process can
read afterwards. The web application itself has no bootstrap endpoint.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError
from app.models import Role, User
from app.services import audit
from app.services.passwords import hash_password, validate_password_policy


def bootstrap_admin(
    session: Session,
    *,
    username: str,
    password: str,
    settings: Settings,
    reset_existing: bool = False,
) -> User:
    """Create the initial administrator, or reset the password of an existing
    one when ``reset_existing`` is set. Idempotent for repeat ``--reset`` runs."""
    name = username.strip().lower()
    admin_role = session.execute(select(Role).where(Role.name == "admin")).scalar_one()
    if admin_role is None:
        raise RuntimeError("admin role is missing; run migrations (alembic upgrade head)")

    validate_password_policy(password, username=name, settings=settings)
    existing: User | None = session.execute(
        select(User).where(User.username == name)
    ).scalar_one_or_none()

    if existing is not None and not reset_existing:
        raise ConflictError(
            f"user '{name}' already exists; pass --reset-password to reset its password"
        )

    if existing is None:
        user = User(
            username=name,
            display_name="Administrator",
            password_hash=hash_password(password, settings),
            is_active=True,
        )
        user.roles = [admin_role]
        session.add(user)
        session.flush()
        audit.record_audit_event(
            session,
            event=audit.USER_BOOTSTRAPPED,
            category=audit.CATEGORY_USER,
            actor=user,
            target_type="user",
            target_id=user.id,
            details={"username": name, "roles": ["admin"]},
            commit=False,
        )
        session.commit()
        return user

    if reset_existing:
        existing.password_hash = hash_password(password, settings)
        existing.is_active = True
        existing.locked_until = None
        existing.failed_login_count = 0
        if "admin" not in {role.name for role in existing.roles}:
            existing.roles.append(admin_role)
        audit.record_audit_event(
            session,
            event=audit.USER_PASSWORD_RESET,
            category=audit.CATEGORY_USER,
            actor=existing,
            target_type="user",
            target_id=existing.id,
            details={"username": name, "source": "bootstrap"},
            commit=False,
        )
        session.commit()
        return existing
    return existing
