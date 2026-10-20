"""Permission tokens and the role defaults.

This module is the single source of truth for every permission string the
application can grant. Unknown tokens are rejected when permissions are
written, so a role can never carry permissions this code does not understand.
"""

from __future__ import annotations

PERMISSIONS: list[str] = [
    "dashboard:read",
    "users:read",
    "users:write",
    "groups:read",
    "groups:write",
    "roles:read",
    "roles:write",
    "audit:read",
    "system:read",
    "system:write",
    "settings:read",
    "settings:write",
    "files:read",
    "files:write",
    "samba:read",
    "samba:write",
]

ALL_PERMISSIONS: frozenset[str] = frozenset(PERMISSIONS)

ROLE_DESCRIPTIONS: dict[str, str] = {
    "admin": "Full access to every managed resource, including user and group management.",
    "operator": "Read access to users, groups, roles, audit log, system and settings.",
    "viewer": "Read-only access to the dashboard.",
}

ROLE_DEFAULT_PERMISSIONS: dict[str, list[str]] = {
    "admin": sorted(ALL_PERMISSIONS),
    "operator": [
        "dashboard:read",
        "users:read",
        "groups:read",
        "roles:read",
        "audit:read",
        "system:read",
        "settings:read",
    ],
    "viewer": ["dashboard:read"],
}

# Roles created by migrations that cannot be renamed or deleted by the API.
SYSTEM_ROLES: tuple[str, ...] = tuple(ROLE_DEFAULT_PERMISSIONS)

# Role assigned automatically when a new user is created without explicit roles.
DEFAULT_USER_ROLE = "viewer"


def is_valid_permission(token: str) -> bool:
    return token in ALL_PERMISSIONS


def validate_permissions(tokens: list[str]) -> None:
    """Raise ValueError on the first unknown permission token."""
    unknown = sorted({token for token in tokens if not is_valid_permission(token)})
    if unknown:
        raise ValueError(f"unknown permission token(s): {', '.join(unknown)}")
