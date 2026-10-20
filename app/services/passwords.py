"""Argon2id password hashing and the password policy.

Argon2id parameters come from settings; tests use deliberately cheap costs
(see tests/conftest.py). Verification never raises: every failure — unknown
hash, malformed hash, wrong password — yields ``False``.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)

from app.core.config import Settings


def build_hasher(settings: Settings) -> PasswordHasher:
    return PasswordHasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost,
        parallelism=settings.argon2_parallelism,
        hash_len=32,
        salt_len=16,
    )


def hash_password(password: str, settings: Settings) -> str:
    """Hash a password. Never log or persist the return value anywhere but the
    ``users.password_hash`` column."""
    return build_hasher(settings).hash(password)


def verify_password(password: str, password_hash: str, settings: Settings) -> bool:
    hasher = build_hasher(settings)
    try:
        return hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def validate_password_policy(
    password: str, *, username: str | None = None, settings: Settings
) -> None:
    """Validate the password policy, raising ValueError with a client-safe message."""
    if len(password) < settings.password_min_length:
        raise ValueError(
            f"password must be at least {settings.password_min_length} characters long"
        )
    if len(password) > settings.password_max_length:
        raise ValueError(f"password must be at most {settings.password_max_length} characters long")
    if not password.strip():
        raise ValueError("password must not be only whitespace")
    if username and password.lower() == username.lower():
        raise ValueError("password must not be identical to the username")
