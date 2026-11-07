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
    "uploads:read",
    "uploads:write",
    "disks:read",
    "disks:write",
    "api_tokens:read",
    "api_tokens:write",
    "remotes:read",
    "remotes:write",
    "media:read",
    "media:write",
    "media:admin",
    "backup:read",
    "backup:write",
    "backup:restore",
    "config:export",
    "config:import",
    "removable:read",
    "removable:write",
]

ALL_PERMISSIONS: frozenset[str] = frozenset(PERMISSIONS)

ROLE_DESCRIPTIONS: dict[str, str] = {
    "admin": "Full access to every managed resource, including user and group management.",
    "operator": (
        "Read access to users, groups, roles, audit log, system, settings, public upload usage"
        ", API token metadata, remote sync status (never remote credentials)"
        " and system monitoring and alerts (view only)"
        ", backup repositories, jobs, runs and snapshots (view only; never restore)"
        "; their own photo and video library; plugged-in USB drives (copy to and from)."
    ),
    "viewer": "Read-only access to the dashboard; their own photo and video library.",
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
        "uploads:read",
        "disks:read",
        "api_tokens:read",
        "remotes:read",
        "media:read",
        "media:write",
        "backup:read",
        "removable:read",
        "removable:write",
    ],
    "viewer": ["dashboard:read", "media:read", "media:write"],
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
